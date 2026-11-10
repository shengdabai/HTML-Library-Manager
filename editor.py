#!/usr/bin/env python3
"""Editor backend: script-free preview, versioned saves, annotations, uploads and Codex jobs."""
from __future__ import annotations

import base64
import glob
import hashlib
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote

from library import allowed_path

MAX_EDIT_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_BYTES = 12 * 1024 * 1024
MAX_PREVIEW_BYTES = 80 * 1024 * 1024
KEEP_VERSIONS = 60
JOB_TIMEOUT = 25 * 60
MAX_ACTIVE_JOBS = 2
PREVIEW_TYPES = {'.html', '.htm', '.css', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.avif', '.svg',
                 '.ico', '.bmp', '.woff', '.woff2', '.ttf', '.otf', '.mp4', '.webm', '.mp3',
                 '.wav', '.m4a', '.ogg'}
IMAGE_MAGIC = [(b'\x89PNG\r\n\x1a\n', '.png'), (b'\xff\xd8\xff', '.jpg'), (b'GIF87a', '.gif'),
               (b'GIF89a', '.gif')]
REASONS = {'before-save': '手动保存前', 'before-restore': '恢复版本前', 'before-codex': 'Codex 修改前'}
# Preview documents keep their own CSS/images but never run scripts, submit forms or call APIs.
PREVIEW_CSP = ("default-src 'self' data: blob: https: http:; script-src 'none'; object-src 'none'; "
               "connect-src 'none'; form-action 'none'; base-uri 'self'; frame-src https: http:; "
               "style-src 'self' 'unsafe-inline' https: http:; frame-ancestors 'self'")
ASSET_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; sandbox"


class Conflict(ValueError):
    """The file or job state changed since the client loaded it."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stamp() -> str:
    return datetime.now().strftime('%Y%m%dT%H%M%S-%f')


def atomic_bytes(path: Path, data: bytes, mode: int | None = None, expect: bytes | None = None):
    """Write via a synced temp file; with expect, refuse (Conflict) if path no longer holds those bytes."""
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(6) + '.tmp')
    try:
        # Backups and annotations can contain private source text. Create the file
        # privately before writing any bytes, regardless of the process umask.
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        if expect is not None and path.read_bytes() != expect:  # Last check, after the slow write/fsync.
            raise Conflict('原文件刚刚被其他程序修改，未写入。请重新载入后再试')
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def editable_reason(data: bytes) -> str:
    if len(data) > MAX_EDIT_BYTES:
        return '文件超过 20 MB，暂不支持在线编辑'
    try:
        data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return '仅支持 UTF-8 编码的页面在线编辑'
    match = re.search(br'charset\s*=\s*["\x27]?([a-zA-Z0-9_-]+)', data[:4096], re.I)
    if match and match[1].decode('ascii').lower() not in {'utf-8', 'utf8'}:
        return '页面声明了非 UTF-8 编码，暂不支持在线编辑'
    return ''


def find_codex() -> str:
    candidates = [os.environ.get('HTML_MANAGER_CODEX', ''), shutil.which('codex') or '']
    # Finder-launched .command shells often miss nvm's PATH; newest installed node wins.
    candidates += sorted(glob.glob(str(Path.home() / '.nvm/versions/node/*/bin/codex')), reverse=True)
    candidates += ['/opt/homebrew/bin/codex', '/usr/local/bin/codex']
    return next((c for c in candidates if c and os.access(c, os.X_OK)), '')


def clip(value, limit):
    return value[:limit] if isinstance(value, str) else ''


class Editor:
    def __init__(self, catalog, codex=None):
        self.catalog = catalog
        self.base = Path(catalog.state_dir) / 'editor'
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.processes: dict[str, subprocess.Popen] = {}
        self.codex = codex if codex is not None else find_codex()
        # Also protect artifacts created by older versions of the manager.
        jobs_root = self.base / 'jobs'
        jobs_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        jobs_root.chmod(0o700)
        for meta in sorted(jobs_root.glob('*/meta.json')):
            try:
                job = json.loads(meta.read_text(), parse_constant=lambda _: None)
            except (OSError, ValueError):
                continue
            strings = {'id', 'record', 'filename', 'status', 'instruction', 'base_digest',
                       'model', 'summary', 'error', 'version', 'digest'}
            if (not isinstance(job, dict)
                    or not all(isinstance(job.get(key), str) for key in strings)
                    or job['id'] != meta.parent.name
                    or not re.fullmatch(r'[\w-]{1,80}', job['id'])
                    or job['status'] not in {'queued', 'running', 'interrupted', 'applied',
                                             'failed', 'conflict', 'cancelled', 'nochange', 'undone'}
                    or not isinstance(job.get('created'), (int, float))
                    or not all(isinstance(job.get(key), list) and
                               all(isinstance(value, str) for value in job[key])
                               for key in ('annotations', 'assets'))):
                continue  # A damaged job must not prevent opening the entire manager.
            if job.get('status') in {'queued', 'running'}:
                job.update(status='interrupted', error='管理器重启，任务中断；原文件未被这次任务修改')
            self.jobs[job['id']] = job

    # ---------- records and files ----------
    def record(self, identifier):
        with self.catalog.lock:
            record = self.catalog.records.get(identifier) if isinstance(identifier, str) else None
        if not record:
            raise ValueError('文件不在当前索引中，请回到资料库刷新')
        path = Path(record['path'])
        if (path.suffix.lower() not in {'.html', '.htm'} or not allowed_path(path)
                or not path.is_file() or path.is_symlink()):
            raise ValueError('原文件已移动、删除或变为符号链接，请刷新资料库')
        return record, path

    def read(self, identifier):
        record, path = self.record(identifier)
        data = path.read_bytes()
        return record, path, data

    def preview_url(self, record):
        root, path = Path(record['root']), Path(record['path'])
        relative = path.relative_to(root).as_posix()
        return '/p/' + record['id'] + '/' + quote(relative)

    def info(self, identifier):
        record, path, data = self.read(identifier)
        reason = editable_reason(data)
        public = {k: v for k, v in record.items() if k not in {'text', 'uri'}}
        return {'record': public, 'digest': sha256(data), 'size': len(data), 'editable': not reason,
                'reason': reason, 'preview': self.preview_url(record),
                'assets_dir': path.stem + '_assets', 'job': self.active_job(identifier),
                'codex': bool(self.codex)}

    def preview(self, identifier, relative):
        record = self.catalog.records.get(identifier)
        if not record:
            raise FileNotFoundError('preview record missing')
        raw_root = Path(record['root'])
        raw = Path(os.path.normpath(raw_root / unquote(relative)))
        # Check symlink components on the lexical path first; resolve() would erase them.
        if raw_root not in raw.parents or not allowed_path(raw):
            raise FileNotFoundError('preview asset unavailable')
        root = raw_root.resolve()
        target = raw.resolve()
        if (root not in target.parents or target.suffix.lower() not in PREVIEW_TYPES
                or not allowed_path(target) or not target.is_file()):
            raise FileNotFoundError('preview asset unavailable')
        if target.stat().st_size > MAX_PREVIEW_BYTES:
            raise FileNotFoundError('preview asset too large')
        kind = 'text/html' if target.suffix.lower() in {'.html', '.htm'} else (
            mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
        csp = PREVIEW_CSP if kind == 'text/html' else ASSET_CSP
        return target.read_bytes(), kind, csp

    # ---------- versions ----------
    def version_dir(self, identifier):
        return self.base / 'versions' / identifier

    def snapshot(self, identifier, data, reason, note=''):
        folder = self.version_dir(identifier)
        folder.mkdir(parents=True, exist_ok=True)
        name = stamp() + '-' + reason + '.html'
        atomic_bytes(folder / name, data, 0o600)
        index = self.versions(identifier)
        index.insert(0, {'name': name, 'time': time.time(), 'reason': reason,
                         'label': REASONS.get(reason, reason), 'note': clip(note, 200),
                         'digest': sha256(data), 'size': len(data)})
        for old in index[KEEP_VERSIONS:]:
            (folder / old['name']).unlink(missing_ok=True)
        atomic_bytes(folder / 'index.json', json.dumps(index[:KEEP_VERSIONS], ensure_ascii=False).encode(), 0o600)
        return name

    def versions(self, identifier):
        try:
            rows = json.loads((self.version_dir(identifier) / 'index.json').read_text(), parse_constant=lambda _: None)
            if not isinstance(rows, list):
                return []
            return [r for r in rows if isinstance(r, dict) and isinstance(r.get('name'), str)
                    and re.fullmatch(r'[0-9T-]+-[a-z-]+\.html', r['name'])]
        except (OSError, ValueError):
            return []

    def write(self, identifier, path, current, data, reason, note=''):
        """Back up the verified bytes, re-check, then atomically replace the source; caller holds self.lock.

        A writer outside the manager can still change the file in the instant between the final check and
        the rename; closing that gap would need every writer to share a lock."""
        version = self.snapshot(identifier, current, reason, note)
        atomic_bytes(path, data, path.stat().st_mode & 0o777, expect=current)
        self.catalog.reindex(identifier)
        return version

    def save(self, identifier, html, base_digest, note=''):
        if not isinstance(html, str) or not html.strip():
            raise ValueError('页面内容为空，未保存')
        data = html.encode('utf-8')
        if len(data) > MAX_EDIT_BYTES:
            raise ValueError('页面超过 20 MB，未保存')
        with self.lock:
            record, path, current = self.read(identifier)
            reason = editable_reason(current)
            if reason:
                raise ValueError(reason)
            if self.active_job(identifier):
                raise Conflict('Codex 正在修改这个页面，完成后再保存')
            if sha256(current) != base_digest:
                raise Conflict('原文件在你编辑期间被修改过。请先复制你的改动，再重新载入页面')
            if data == current:
                return {'ok': True, 'digest': base_digest, 'unchanged': True}
            version = self.write(identifier, path, current, data, 'before-save', note)
            return {'ok': True, 'digest': sha256(data), 'version': version}

    def restore(self, identifier, name, base_digest):
        if not isinstance(name, str) or not re.fullmatch(r'[0-9T-]+-[a-z-]+\.html', name):
            raise ValueError('版本名称无效')
        with self.lock:
            record, path, current = self.read(identifier)
            if self.active_job(identifier):
                raise Conflict('Codex 正在修改这个页面，完成后再恢复')
            if sha256(current) != base_digest:
                raise Conflict('原文件已变化，请重新载入后再恢复')
            source = self.version_dir(identifier) / name
            if not source.is_file():
                raise ValueError('这个版本已不存在')
            data = source.read_bytes()
            self.write(identifier, path, current, data, 'before-restore', '恢复到 ' + name[:15])
            return {'ok': True, 'digest': sha256(data)}

    # ---------- annotations ----------
    def annotation_path(self, identifier):
        return self.base / 'annotations' / (identifier + '.json')

    def annotations(self, identifier):
        self.record(identifier)
        with self.lock:  # File contents and job state must come from the same moment.
            try:
                rows = json.loads(self.annotation_path(identifier).read_text(), parse_constant=lambda _: None)
                rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
            except (OSError, ValueError):
                return []
            busy = {i for j in self.jobs.values() if j['record'] == identifier
                    and j['status'] in {'queued', 'running'} for i in j['annotations']}
        for row in rows:  # "sent" without a running job (e.g. after a restart) means open again.
            if row.get('status') == 'sent' and row.get('id') not in busy:
                row['status'] = 'open'
        return rows

    def set_annotations(self, identifier, rows, trusted=False):
        """Store notes. Client writes (trusted=False) cannot set or clear "sent" nor edit notes in a running job."""
        self.record(identifier)
        if not isinstance(rows, list) or len(rows) > 200:
            raise ValueError('标注最多 200 条')
        with self.lock:
            return self._set_annotations(identifier, rows, trusted)

    def _set_annotations(self, identifier, rows, trusted):
        existing = {} if trusted else {r.get('id'): r for r in self.annotations(identifier)}
        frozen = {k: v for k, v in existing.items() if v.get('status') == 'sent'}
        clean = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('id'), str):
                raise ValueError('标注格式错误')
            if not trusted:
                if row['id'] in frozen:
                    row = frozen.pop(row['id'])
                elif row.get('status') == 'sent':
                    row = {**row, 'status': existing.get(row['id'], {}).get('status', 'open')}
            if any(row.get(key) is not None and not isinstance(row[key], str) for key in ('kind', 'status')):
                raise ValueError('标注类型或状态格式错误')
            created = row.get('created')
            if (not isinstance(created, (int, float))
                    or (isinstance(created, float) and not math.isfinite(created))):
                created = time.time()
            clean.append({'id': clip(row['id'], 40), 'selector': clip(row.get('selector'), 1000),
                          'tag': clip(row.get('tag'), 40), 'text': clip(row.get('text'), 1000),
                          'html': clip(row.get('html'), 6000), 'src': clip(row.get('src'), 2000),
                          'instruction': clip(row.get('instruction'), 4000),
                          'kind': row.get('kind') if row.get('kind') in {'text', 'layout', 'image', 'other'} else 'other',
                          'status': row.get('status') if row.get('status') in {'open', 'sent', 'done'} else 'open',
                          'created': created})
        for row in frozen.values():  # A note in a running job cannot be deleted by the client.
            clean.append(row)
        path = self.annotation_path(identifier)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_bytes(path, json.dumps(clean, ensure_ascii=False).encode(), 0o600)
        return {'ok': True, 'annotations': clean}

    def mark_annotations(self, identifier, ids, status):
        rows = self.annotations(identifier)
        for row in rows:
            if row.get('id') in ids:
                row['status'] = status
        self.set_annotations(identifier, rows, trusted=True)

    # ---------- uploads ----------
    def upload(self, identifier, name, data64):
        record, path = self.record(identifier)
        if not isinstance(data64, str):
            raise ValueError('图片数据缺失')
        try:
            data = base64.b64decode(data64.split(',', 1)[-1], validate=True)
        except ValueError:
            raise ValueError('图片数据无法解码') from None
        if not data or len(data) > MAX_UPLOAD_BYTES:
            raise ValueError('图片需小于 12 MB')
        suffix = next((ext for magic, ext in IMAGE_MAGIC if data.startswith(magic)), '')
        if not suffix and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            suffix = '.webp'
        if not suffix:
            raise ValueError('仅支持 PNG、JPEG、GIF、WebP 图片')
        stem = re.sub(r'[^\w一-鿿-]+', '-', Path(clip(name, 80) or 'image').stem).strip('-')[:40] or 'image'
        folder = path.parent / (path.stem + '_assets')
        if not allowed_path(folder):
            raise ValueError('资源目录路径不允许写入')
        folder.mkdir(exist_ok=True)
        target = folder / f'{stem}-{secrets.token_hex(3)}{suffix}'
        with target.open('xb') as stream:
            stream.write(data)
        return {'ok': True, 'src': quote(path.stem + '_assets') + '/' + quote(target.name)}

    # ---------- Codex jobs ----------
    def active_job(self, identifier):
        with self.lock:
            for job in self.jobs.values():
                if job['record'] == identifier and job['status'] in {'queued', 'running'}:
                    return self.public_job(job)
        return None

    def public_job(self, job):
        output = dict(job)
        log = self.base / 'jobs' / job['id'] / 'log.txt'
        try:
            with log.open('rb') as stream:
                stream.seek(max(0, log.stat().st_size - 6000))
                output['log'] = stream.read().decode('utf-8', errors='replace')
        except OSError:
            output['log'] = ''
        return output

    def job(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id) if isinstance(job_id, str) else None
            if not job:
                raise ValueError('任务不存在')
            return self.public_job(job)

    def job_list(self, identifier):
        self.record(identifier)
        with self.lock:
            rows = [self.public_job(j) for j in self.jobs.values() if j['record'] == identifier]
        rows.sort(key=lambda j: j['created'], reverse=True)
        for row in rows[1:]:
            row.pop('log', None)
        return rows[:20]

    def persist(self, job):
        folder = self.base / 'jobs' / job['id']
        folder.mkdir(parents=True, exist_ok=True)
        atomic_bytes(folder / 'meta.json', json.dumps(job, ensure_ascii=False).encode(), 0o600)

    def prompt(self, job, path, instruction, notes):
        assets = path.stem + '_assets'
        lines = [
            '你在修改一个本地 HTML 页面。当前工作目录是你唯一可写的位置。',
            f'- 目标文件：./{path.name}（它是原文件的副本；你完成后由 HTML 管理器备份原文件再写回）。',
            f'- 原页面目录（只读，可查看图片、CSS 等相对资源）：{path.parent}',
            f'- 新增或修改的图片保存到 ./{assets}/，文件名以 codex-{job["id"]} 开头；'
            f'HTML 中用相对路径 {assets}/文件名 引用。不要覆盖原目录里的文件。',
            '- 需要生成或改图时，使用你内置的图像生成工具；改现有图片时先读取原图作参考，生成后把图片文件复制进上面的目录。',
            '- 只改下面要求涉及的部分，其余 HTML 保持原样（结构、脚本、样式、UTF-8 编码），不要重新格式化整个文件。',
            '- 不要访问网络，不要运行与本任务无关的命令，不要修改工作目录以外的文件。',
            '- 完成后用中文列出改了什么（不超过 5 条）。', '']
        if instruction:
            lines += ['## 整体要求', instruction, '']
        if notes:
            lines.append(f'## 页面上的标注（{len(notes)} 条，按顺序处理）')
            kinds = {'text': '文字内容', 'layout': '排版样式', 'image': '图片', 'other': '其他'}
            for number, note in enumerate(notes, 1):
                lines += [f'### {number}. {kinds.get(note["kind"], "其他")}',
                          f'- 定位（CSS 选择器）：`{note["selector"]}`']
                if note.get('text'):
                    lines.append('- 当前文字：' + json.dumps(note['text'][:800], ensure_ascii=False))
                if note.get('src'):
                    lines.append('- 图片地址（相对原页面目录）：' + note['src'][:500])
                if note.get('html'):
                    lines += ['- HTML 片段：', '```html', note['html'][:3000], '```']
                lines += ['- 修改要求：' + (note.get('instruction') or '按整体要求优化'), '']
        return '\n'.join(lines)

    def start_job(self, identifier, instruction, annotation_ids, base_digest, model=''):
        instruction = clip(instruction, 8000).strip()
        if not isinstance(annotation_ids, list) or not all(isinstance(x, str) for x in annotation_ids):
            raise ValueError('标注列表格式错误')
        if model and (not isinstance(model, str) or not re.fullmatch(r'[\w.:-]{1,60}', model)):
            raise ValueError('模型名称无效')
        if not self.codex:
            raise ValueError('本机没有找到 codex 命令。安装 Codex CLI 或设置 HTML_MANAGER_CODEX')
        with self.lock:
            record, path, data = self.read(identifier)
            reason = editable_reason(data)
            if reason:
                raise ValueError(reason)
            if sha256(data) != base_digest:
                raise Conflict('原文件已变化，请先保存或重新载入')
            if self.active_job(identifier):
                raise Conflict('这个页面已有 Codex 任务在运行')
            if sum(j['status'] in {'queued', 'running'} for j in self.jobs.values()) >= MAX_ACTIVE_JOBS:
                raise Conflict(f'最多同时运行 {MAX_ACTIVE_JOBS} 个 Codex 任务，请稍后')
            notes = [n for n in self.annotations(identifier) if n['id'] in set(annotation_ids)]
            if not instruction and not notes:
                raise ValueError('请写下修改要求，或选择至少一条标注')
            job_id = datetime.now().strftime('%m%d%H%M%S') + secrets.token_hex(3)
            folder = self.base / 'jobs' / job_id
            work = folder / 'work'
            # Protect the prompt, source copy and CLI logs before creating them.
            folder.mkdir(parents=True, mode=0o700)
            (work / (path.stem + '_assets')).mkdir(parents=True)
            (work / path.name).write_bytes(data)
            job = {'id': job_id, 'record': identifier, 'filename': path.name, 'status': 'queued',
                   'created': time.time(), 'started': None, 'finished': None, 'instruction': instruction,
                   'annotations': [n['id'] for n in notes], 'base_digest': base_digest, 'model': model or '',
                   'summary': '', 'error': '', 'version': '', 'assets': [], 'digest': ''}
            (folder / 'prompt.md').write_text(self.prompt(job, path, instruction, notes), encoding='utf-8')
            self.jobs[job_id] = job
            self.persist(job)
            if notes:
                self.mark_annotations(identifier, job['annotations'], 'sent')
        threading.Thread(target=self.run_job, args=(job_id,), daemon=True).start()
        return {'ok': True, 'job': self.public_job(job)}

    def run_job(self, job_id):
        job = self.jobs[job_id]
        folder = self.base / 'jobs' / job_id
        work = folder / 'work'
        command = [self.codex, 'exec', '--skip-git-repo-check', '-s', 'workspace-write',
                   '-c', 'approval_policy="never"', '-C', str(work), '--color', 'never',
                   '-o', str(folder / 'last.txt')]
        if job['model']:
            command += ['-m', job['model']]
        command.append('-')
        environment = dict(os.environ)
        environment['PATH'] = str(Path(self.codex).parent) + os.pathsep + environment.get('PATH', '')
        try:
            with (folder / 'prompt.md').open('rb') as prompt, (folder / 'log.txt').open('wb') as log:
                with self.lock:
                    if job['status'] == 'cancelled':
                        return
                    process = subprocess.Popen(command, stdin=prompt, stdout=log, stderr=subprocess.STDOUT,
                                               cwd=work, env=environment, start_new_session=True)
                    self.processes[job_id] = process
                    job.update(status='running', started=time.time())
                    self.persist(job)
                try:
                    code = process.wait(timeout=JOB_TIMEOUT)
                except subprocess.TimeoutExpired:
                    self.kill(process)
                    code = None
            with self.lock:
                self.processes.pop(job_id, None)
                if job['status'] == 'cancelled':
                    return
                try:
                    job['summary'] = (folder / 'last.txt').read_text(errors='replace')[:6000]
                except OSError:
                    pass
                if code is None:
                    self.finish(job, 'failed', error=f'Codex 超过 {JOB_TIMEOUT // 60} 分钟未完成，已停止；原文件未修改')
                elif code != 0:
                    self.finish(job, 'failed', error=f'Codex 退出码 {code}；原文件未修改，详见日志')
                else:
                    self.apply(job, work)
        except Exception as error:  # Never leave a job "running" after an unexpected failure.
            with self.lock:
                self.processes.pop(job_id, None)
                if job['status'] not in {'cancelled', 'applied'}:
                    self.finish(job, 'failed', error='任务异常：' + str(error)[:200])

    def apply(self, job, work):
        """Caller holds self.lock. Copy Codex output back only if the source is unchanged since start."""
        identifier = job['record']
        try:
            record, path, current = self.read(identifier)
        except ValueError as error:
            return self.finish(job, 'failed', error=str(error))
        result = work / path.name
        if not result.is_file() or result.is_symlink():
            return self.finish(job, 'failed', error='Codex 没有留下目标 HTML，原文件未修改')
        data = result.read_bytes()
        if not data.strip() or editable_reason(data):
            return self.finish(job, 'failed', error='Codex 输出不是有效的 UTF-8 HTML，原文件未修改')
        if sha256(current) != job['base_digest']:
            return self.finish(job, 'conflict', error='任务期间原文件被其他程序修改，Codex 结果未写回（保留在任务目录）')
        assets = []
        source_assets = work / (path.stem + '_assets')
        target_assets = path.parent / (path.stem + '_assets')
        items = sorted(source_assets.iterdir()) if source_assets.is_dir() and not source_assets.is_symlink() else []
        items = [i for i in items if i.is_file() and not i.is_symlink() and i.suffix.lower() in PREVIEW_TYPES]
        if items and not allowed_path(target_assets):
            return self.finish(job, 'failed', error='资源目录是符号链接或不允许写入，原文件未修改')
        for item in items:
            target_assets.mkdir(exist_ok=True)
            target = target_assets / item.name
            if os.path.lexists(target):
                continue  # Never overwrite an existing asset or follow a dangling link.
            try:
                with target.open('xb') as stream:  # O_EXCL: refuses any pre-existing entry, symlinks included.
                    stream.write(item.read_bytes())
            except FileExistsError:
                continue
            assets.append(item.name)
        if data == current and not assets:
            self.mark_annotations(identifier, job['annotations'], 'open')
            return self.finish(job, 'nochange', error='Codex 没有修改页面')
        try:
            version = self.write(identifier, path, current, data, 'before-codex', job['instruction'][:120]) if data != current else ''
        except Conflict as error:
            return self.finish(job, 'conflict', error=str(error) + '（Codex 结果保留在任务目录）')
        job.update(version=version, assets=assets, digest=sha256(data))
        self.mark_annotations(identifier, job['annotations'], 'done')
        return self.finish(job, 'applied')

    def finish(self, job, status, error=''):
        job.update(status=status, error=error, finished=time.time())
        if status in {'failed', 'conflict', 'cancelled', 'interrupted'} and job['annotations']:
            try:
                self.mark_annotations(job['record'], job['annotations'], 'open')
            except ValueError:
                pass
        self.persist(job)

    @staticmethod
    def kill(process):
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id) if isinstance(job_id, str) else None
            if not job or job['status'] not in {'queued', 'running'}:
                raise ValueError('任务不在运行中')
            process = self.processes.get(job_id)
            self.finish(job, 'cancelled', error='已取消；原文件未修改')
        if process:
            self.kill(process)
        return {'ok': True}

    def undo_job(self, job_id, base_digest):
        with self.lock:
            job = self.jobs.get(job_id) if isinstance(job_id, str) else None
            if not job or job['status'] != 'applied' or not job.get('version'):
                raise ValueError('这个任务没有可撤销的页面修改')
            _, _, current = self.read(job['record'])
            if sha256(current) != job.get('digest'):
                raise Conflict('Codex 改完后页面又被保存或修改过，不能直接撤销这次任务。请到「版本」页选择要恢复的备份')
            result = self.restore(job['record'], job['version'], base_digest)
            job['status'] = 'undone'
            self.persist(job)
            return result

    # ---------- HTTP dispatch ----------
    def get(self, action, params):
        identifier = params.get('id', [''])[0]
        if action == 'info':
            return self.info(identifier)
        if action == 'annotations':
            return {'annotations': self.annotations(identifier)}
        if action == 'versions':
            self.record(identifier)
            return {'versions': self.versions(identifier)}
        if action == 'jobs':
            return {'jobs': self.job_list(identifier)}
        if action == 'job':
            return {'job': self.job(params.get('job', [''])[0])}
        raise ValueError('未知操作')

    def post(self, action, payload):
        identifier = payload.get('id')
        if action == 'save':
            return self.save(identifier, payload.get('html'), payload.get('digest'), payload.get('note', ''))
        if action == 'restore':
            return self.restore(identifier, payload.get('version'), payload.get('digest'))
        if action == 'annotations':
            return self.set_annotations(identifier, payload.get('annotations'))
        if action == 'upload':
            return self.upload(identifier, payload.get('name', ''), payload.get('data'))
        if action == 'codex':
            return self.start_job(identifier, payload.get('instruction', ''), payload.get('annotations', []),
                                  payload.get('digest'), payload.get('model', ''))
        if action == 'cancel':
            return self.cancel(payload.get('job'))
        if action == 'undo':
            return self.undo_job(payload.get('job'), payload.get('digest'))
        raise ValueError('未知操作')
