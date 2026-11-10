#!/usr/bin/env python3
"""HTML library: scan/export or optional loopback runtime; no third-party packages."""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
import os
import sys
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from editor import Conflict, Editor
from library import allowed_path, discover, excluded_directory_name, extract

PROJECT = Path(__file__).resolve().parent
DEFAULT_STATE = Path.home() / '.local/share/html-manager'


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(6) + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_json(path, fallback):
    try:
        # Python accepts NaN/Infinity, but browser JSON parsers reject those tokens.
        value = json.loads(path.read_text(), parse_constant=lambda _: None)
        return value if isinstance(value, type(fallback)) else fallback
    except (OSError, ValueError):
        return fallback


def default_roots():
    home = Path.home()
    return [home / 'Desktop/03-内容创作/16-飞书HTML', home / 'Documents',
            home / 'Downloads', home / 'Desktop', home / 'Desktop/01-项目开发']


def validate_root(value):
    path = Path(value).expanduser().absolute()
    if not allowed_path(path) or not path.is_dir():
        raise ValueError('目录不存在，或含隐藏/敏感/符号链接路径')
    home = Path.home()
    volume = Path('/Volumes')
    if not (path == home or home in path.parents or (volume in path.parents and
            path.parts[2] not in {'Macintosh HD', 'TimeMachine', 'Recovery'})):
        raise ValueError('仅支持用户主目录和已挂载的个人资料盘')
    if any(part in {'Library', 'Applications'} for part in path.parts):
        raise ValueError('系统和应用目录不作为资料源')
    return path


class Catalog:
    def __init__(self, state_dir=DEFAULT_STATE, roots=None):
        self.state_dir = Path(state_dir)
        self.lock = threading.RLock()
        config = read_json(self.state_dir / 'config.json', {})
        configured = config.get('roots')
        if (not isinstance(configured, list) or not configured
                or not all(isinstance(x, str) and x and '\x00' not in x and Path(x).is_absolute()
                           for x in configured)):
            configured = default_roots()
        self.roots = list(roots) if roots is not None else [Path(x) for x in configured]
        cached = read_json(self.state_dir / 'catalog.json', {})
        required = {'id', 'path', 'uri', 'filename', 'title', 'label', 'basis', 'snippet', 'text', 'modified', 'size', 'digest', 'root', 'folder'}
        rows = cached.get('records', [])
        self.records = {r['id']: r for r in rows if isinstance(r, dict) and required <= r.keys()
                        and all(isinstance(r[k], str) for k in required - {'modified', 'size'})
                        and isinstance(r['modified'], (int, float)) and isinstance(r['size'], int)
                        and self.in_scope(Path(r['path']))} if isinstance(rows, list) else {}
        self.report = cached.get('report', {'count': 0, 'roots': [], 'issues': [], 'issue_count': 0, 'limited': False})
        self.state = read_json(self.state_dir / 'state.json', {'favorites': [], 'recent': {}})
        if not isinstance(self.state.get('favorites'), list):
            self.state['favorites'] = []
        if not isinstance(self.state.get('recent'), dict):
            self.state['recent'] = {}
        self.state['favorites'] = [v for v in self.state['favorites'] if isinstance(v, str)]
        self.state['recent'] = {k: v for k, v in self.state['recent'].items()
                                if isinstance(v, (int, float))}
        self.scanning = False
        self.scan_error = ''
        self.token = secrets.token_urlsafe(32)

    def in_scope(self, path):
        if (not path.is_absolute() or path.suffix.lower() not in {'.html', '.htm'}
                or not allowed_path(path)):
            return False
        return any(root in path.parents and not any(excluded_directory_name(part)
                   for part in path.relative_to(root).parts[:-1]) for root in self.roots)

    def refresh(self, asynchronous=True):
        with self.lock:
            if self.scanning:
                return False
            self.scanning = True
            self.scan_error = ''
        if asynchronous:
            threading.Thread(target=self._scan, daemon=True).start()
        else:
            self._scan()
        return True

    def _scan(self):
        try:
            development = Path.home() / 'Desktop/01-项目开发'
            excludes = [PROJECT]
            if development in self.roots and Path.home() / 'Desktop' in self.roots:
                excludes.append(development)  # Its own final root gets a separate allowance.
            rows, report = discover(self.roots, exclude=excludes, max_seconds=60)
            with self.lock:
                fresh = {r['id']: r for r in rows}
                # Preserve unknown coverage on partial/error scans; don't advertise stale as verified.
                uncertain = report['limited'] or report['issue_count'] > 0
                if uncertain:
                    for key, old in self.records.items():
                        if (key not in fresh and self.in_scope(Path(old['path']))
                                and Path(old['path']).exists() and allowed_path(Path(old['path']))):
                            path = Path(old['path'])
                            root = max((r for r in self.roots if r in path.parents), key=lambda r: len(r.parts))
                            fresh[key] = dict(old, stale=True, root=str(root), folder=str(path.parent.relative_to(root)) or '.')
                self.records = fresh
                self.report = report
                self.report['cached_unverified'] = sum(bool(r.get('stale')) for r in fresh.values())
                atomic_json(self.state_dir / 'catalog.json', {'records': list(fresh.values()), 'report': report})
        except Exception as error:
            with self.lock:
                self.scan_error = str(error)[:180]
        finally:
            with self.lock:
                self.scanning = False

    def reindex(self, identifier):
        """Refresh one record after an in-place edit; publish a new dict so search caches rebuild."""
        with self.lock:
            old = self.records.get(identifier)
            if not old:
                return
            try:
                fresh = dict(extract(Path(old['path'])), root=old['root'], folder=old['folder'])
            except (OSError, ValueError):
                return
            records = dict(self.records)
            records[identifier] = fresh
            self.records = records
            atomic_json(self.state_dir / 'catalog.json', {'records': list(records.values()), 'report': self.report})

    def view(self, params):
        with self.lock:
            normalize = lambda value: unicodedata.normalize('NFKC', value).lower()
            terms = normalize(params.get('q', [''])[0]).split()
            scope = params.get('scope', ['all'])[0]
            folder = params.get('folder', [''])[0]
            theme = params.get('theme', [''])[0]
            theme_root = params.get('theme_root', [''])[0]
            sort = params.get('sort', ['modified'])[0]
            favorites = set(self.state.get('favorites', []))
            recent = self.state.get('recent', {})
            # Scans publish a new dictionary atomically; reuse normalized indexes until then.
            if getattr(self, '_search_source', None) is not self.records:
                rows = list(self.records.values())
                self._search_strings = {r['id']: (normalize(r['filename'] + ' ' + r['title'] + ' ' + r['label']), normalize(r['text'])) for r in rows}
                self._search_orders = {
                    'modified': sorted(rows, key=lambda r: r['modified'], reverse=True),
                    'name': sorted(rows, key=lambda r: normalize(r['title']).encode('utf-16-be', errors='surrogatepass'))}
                self._search_hashes = {}
                for r in rows:
                    self._search_hashes[r['digest']] = self._search_hashes.get(r['digest'], 0) + 1
                counts = {}
                for r in rows:
                    name = theme_of(r)
                    if name:
                        counts[(r['root'], name)] = counts.get((r['root'], name), 0) + 1
                order = {str(p): i for i, p in enumerate(self.roots)}
                self._search_themes = [{'root': k[0], 'name': k[1], 'count': counts[k]} for k in sorted(counts, key=lambda k: (order.get(k[0], len(order)), k[0], k[1]))]
                self._search_source = self.records
            records = self._search_orders.get(sort, self._search_orders['modified'])
            fulltext = params.get('mode', ['full'])[0] != 'meta'
            selected = [r for r in records if
                        (not folder or r['root'] == folder) and
                        (not theme or (theme_of(r) == theme and (not theme_root or r['root'] == theme_root))) and
                        (scope != 'favorites' or r['id'] in favorites) and
                        (scope != 'recent' or r['id'] in recent) and
                        all(t in self._search_strings[r['id']][0] or (fulltext and t in self._search_strings[r['id']][1]) for t in terms)]
            if scope == 'recent' and sort != 'name':
                selected.sort(key=lambda r: recent.get(r['id'], 0), reverse=True)
            offset = max(0, int(params.get('offset', ['0'])[0]))
            limit = min(120, max(1, int(params.get('limit', ['60'])[0])))
            hashes = self._search_hashes
            output = []
            for record in selected[offset:offset + limit]:
                public = {k: v for k, v in record.items() if k not in {'text', 'uri'}}
                public.update(favorite=record['id'] in favorites, opened=recent.get(record['id']),
                              duplicates=hashes[record['digest']])
                output.append(public)
            return {'records': output, 'total': len(selected), 'count': len(records),
                    'favorite_count': sum(r['id'] in favorites for r in records),
                    'recent_count': sum(r['id'] in recent for r in records),
                    'report': self.report, 'roots': [str(p) for p in self.roots], 'themes': self._search_themes,
                    'scanning': self.scanning, 'error': self.scan_error}

    def mutate(self, action, payload):
        with self.lock:
            if action == 'refresh':
                self.refresh()
                return {'ok': True}
            if action == 'roots':
                values = payload.get('roots')
                if not isinstance(values, list) or not 1 <= len(values) <= 12 or not all(isinstance(x, str) for x in values):
                    raise ValueError('请选择 1–12 个资料目录')
                if self.scanning:
                    raise ValueError('扫描中，请稍后再修改目录')
                roots = list(dict.fromkeys(validate_root(v) for v in values))
                atomic_json(self.state_dir / 'config.json', {'roots': [str(p) for p in roots]})
                self.roots = roots
                self.records = {}  # Removed sources must stop being searchable/openable immediately.
                self.report = {'roots': [{'path': str(p), 'status': 'pending', 'count': 0} for p in roots],
                               'count': 0, 'issues': [], 'issue_count': 0, 'limited': False}
                atomic_json(self.state_dir / 'catalog.json', {'records': [], 'report': self.report})
                self.refresh()
                return {'ok': True}
            identifier = payload.get('id')
            if not isinstance(identifier, str) or identifier not in self.records:
                raise ValueError('文件不在当前索引中，请刷新资料库')
            record = self.records[identifier]
            if action == 'favorite':
                favorites = set(self.state.get('favorites', []))
                if identifier in favorites:
                    favorites.remove(identifier)
                else:
                    favorites.add(identifier)
                self.state['favorites'] = sorted(favorites)
            elif action == 'open':
                path = Path(record['path'])
                if path.suffix.lower() not in {'.html', '.htm'} or not allowed_path(path) or not path.is_file():
                    raise ValueError('原文件已移动、删除或变为符号链接，请刷新')
                subprocess.run(['/usr/bin/open', str(path)], check=True, timeout=10, capture_output=True)
                self.state.setdefault('recent', {})[identifier] = time.time()
            else:
                raise ValueError('未知操作')
            atomic_json(self.state_dir / 'state.json', self.state)
            return {'ok': True}


APP_CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; "
           "img-src data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
EDITOR_CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; "
              "img-src 'self' data: blob:; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
POST_LIMITS = {'/api/editor/save': 24 * 1024 * 1024, '/api/editor/upload': 18 * 1024 * 1024,
               '/api/editor/annotations': 2 * 1024 * 1024, '/api/editor/codex': 256 * 1024}


def create_server(catalog, port, editor=None):
    editor = editor or Editor(catalog)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *_):
            pass

        def send(self, code, data, content_type='application/json; charset=utf-8', csp=APP_CSP):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', csp)
            self.end_headers()
            self.wfile.write(body)

        def trusted(self, mutation=False):
            host = f'127.0.0.1:{self.server.server_port}'
            origin = self.headers.get('Origin')
            if self.headers.get('Host') != host or (origin and origin != f'http://{host}'):
                self.send(403, {'error': '来源不允许'})
                return False
            if self.headers.get('Sec-Fetch-Site') in {'cross-site'}:
                self.send(403, {'error': '跨站访问不允许'})
                return False
            if mutation and (origin != f'http://{host}' or
                             self.headers.get('X-HTML-Token') != catalog.token or
                             self.headers.get('Content-Type') != 'application/json'):
                self.send(403, {'error': '操作校验失败，请重新打开管理器'})
                return False
            return True

        def do_GET(self):
            if not self.trusted():
                return
            try:
                parts = urlsplit(self.path)
                if parts.path == '/':
                    page = (PROJECT / 'index.html').read_text().replace('__TOKEN__', catalog.token).replace('__FALLBACK__', '本机服务模式需执行 JavaScript；离线导出版本附有文字目录。')
                    self.send(200, page.encode(), 'text/html; charset=utf-8')
                elif parts.path == '/health':
                    self.send(200, {'ok': True, 'app': 'html-manager', 'scanning': catalog.scanning})
                elif parts.path == '/api/library':
                    self.send(200, catalog.view(parse_qs(parts.query)))
                elif parts.path == '/edit':
                    page = (PROJECT / 'editor.html').read_text().replace('__TOKEN__', catalog.token)
                    self.send(200, page.encode(), 'text/html; charset=utf-8', EDITOR_CSP)
                elif parts.path.startswith('/p/'):
                    identifier, _, relative = parts.path[3:].partition('/')
                    body, kind, csp = editor.preview(identifier, relative)
                    self.send(200, body, kind + ('; charset=utf-8' if kind == 'text/html' else ''), csp)
                elif parts.path.startswith('/api/editor/'):
                    self.send(200, editor.get(parts.path.removeprefix('/api/editor/'), parse_qs(parts.query)))
                else:
                    self.send(404, {'error': '页面不存在'})
            except FileNotFoundError:
                self.send(404, {'error': '文件不存在或不允许预览'})
            except (ValueError, OSError) as error:
                self.send(400, {'error': str(error)[:180]})

        def do_POST(self):
            if not self.trusted(mutation=True):
                return
            try:
                path = urlsplit(self.path).path
                size = int(self.headers.get('Content-Length', '0'))
                limit = POST_LIMITS.get(path, 65_536 if path.startswith('/api/editor/') else 16_384)
                if size < 2 or size > limit:
                    raise ValueError('请求大小超出限制')
                if size > 16_384:
                    self.connection.settimeout(120)  # Large saves/uploads need more than the 10 s idle timeout.
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict):
                    raise ValueError('请求格式错误')
                if path.startswith('/api/editor/'):
                    self.send(200, editor.post(path.removeprefix('/api/editor/'), payload))
                else:
                    self.send(200, catalog.mutate(path.removeprefix('/api/'), payload))
            except Conflict as error:
                self.send(409, {'error': str(error)[:300]})
            except (ValueError, OSError, subprocess.SubprocessError) as error:
                self.send(400, {'error': str(error)[:180]})

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def theme_of(record):
    """主题文件夹 = 资料目录下的第一层子目录名；根层文件没有主题。"""
    folder = record.get('folder') or '.'
    return '' if folder == '.' else folder.replace('\\', '/').split('/')[0]


def render_export(data):
    import html
    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    entries = []
    for record in data['records']:
        entries.append(record['label'] + '\n' + record['title'] + '\n' + record['filename'] + '\n' + record.get('snippet', '') + '\n')
    fallback = html.escape('\n'.join(entries))
    substitutions = {'__SNAPSHOT__': payload, '__FALLBACK__': fallback}
    return re.sub(r'__SNAPSHOT__|__FALLBACK__', lambda match: substitutions[match.group()], (PROJECT / 'index.html').read_text())


def main():
    parser = argparse.ArgumentParser(description='本机 HTML 资料库：扫描、导出，或运行本地管理器')
    parser.add_argument('mode', choices=['scan', 'export', 'serve'])
    parser.add_argument('--root', action='append', help='资料目录，可重复指定')
    parser.add_argument('--state-dir', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--port', type=int, default=18455)
    parser.add_argument('--out', type=Path, help='export 的目标 HTML（已有文件不会覆盖，除非加 --force）')
    parser.add_argument('--force', action='store_true', help='export 时原地替换已有文件：先写同目录临时文件，再原子替换')
    parser.add_argument('--open', action='store_true', help='启动后打开浏览器')
    args = parser.parse_args()
    roots = [validate_root(r) for r in args.root] if args.root else None
    catalog = Catalog(args.state_dir, roots)
    if args.mode == 'serve':
        server = create_server(catalog, args.port)
        catalog.refresh()
        url = f'http://127.0.0.1:{server.server_port}'
        print(url, flush=True)
        if args.open:
            subprocess.run(['/usr/bin/open', url], check=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    else:
        catalog.refresh(asynchronous=False)
        if catalog.scan_error:
            parser.exit(1, catalog.scan_error + '\n')
        if args.mode == 'scan':
            print(json.dumps(catalog.report, ensure_ascii=False, indent=2))
        else:
            if not args.out:
                parser.error('export 需要 --out')
            data = {'records': list(catalog.records.values()), 'report': catalog.report}
            page = render_export(data)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            if args.force:
                import tempfile
                handle, temp = tempfile.mkstemp(prefix='.' + args.out.name + '.', suffix='.tmp', dir=args.out.parent)
                try:
                    with os.fdopen(handle, 'w') as stream:
                        os.chmod(temp, 0o600)
                        stream.write(page)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temp, args.out)
                except BaseException:
                    Path(temp).unlink(missing_ok=True)
                    raise
            else:
                with args.out.open('x') as stream:
                    os.chmod(args.out, 0o600)
                    stream.write(page)
            print(str(args.out))


if __name__ == '__main__':
    main()
