import base64
import hashlib
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from editor import Conflict, Editor
from manager import Catalog, create_server

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')
# Stand-in for `codex exec`: edits the copy in -C, adds an asset, writes the -o summary.
FAKE_CODEX = r'''#!/usr/bin/env python3
import os, sys, time, pathlib
args = sys.argv[1:]
work = pathlib.Path(args[args.index('-C') + 1])
last = pathlib.Path(args[args.index('-o') + 1])
prompt = sys.stdin.read()
mode = os.environ.get('FAKE_CODEX_MODE', 'ok')
pathlib.Path(os.environ['FAKE_CODEX_PROMPT']).write_text(prompt)
print('fake codex start', flush=True)
if mode == 'sleep':
    time.sleep(30)
if mode == 'fail':
    print('boom', flush=True); sys.exit(3)
page = next(p for p in work.iterdir() if p.suffix == '.html')
if mode != 'same':
    page.write_text(page.read_text().replace('旧标题', '新标题'))
    assets = work / (page.stem + '_assets')
    (assets / 'codex-generated.png').write_bytes(bytes.fromhex('89504e470d0a1a0a') + b'fake')
last.write_text('1. 标题改为新标题')
'''


class EditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='html-editor-')
        self.directory = Path(self.temp.name).resolve()
        self.sources = self.directory / 'sources'
        (self.sources / 'img').mkdir(parents=True)
        self.page = self.sources / '报告.html'
        self.page.write_text('<!doctype html><html><head><meta charset="utf-8"><title>旧标题</title>'
                             '<link rel="stylesheet" href="style.css"></head><body><h1>旧标题</h1>'
                             '<img src="img/a.png"><script>window.X=1</script></body></html>')
        (self.sources / 'style.css').write_text('h1{color:red}')
        (self.sources / 'img/a.png').write_bytes(PNG)
        (self.sources / '.secret.css').write_text('x')
        self.gbk = self.sources / '旧编码.html'
        self.gbk.write_bytes('<meta charset="gbk"><title>旧</title><p>中文</p>'.encode('gbk'))
        self.fake = self.directory / 'fake-codex'
        self.fake.write_text(FAKE_CODEX)
        self.fake.chmod(0o755)
        self.prompt_log = self.directory / 'prompt.txt'
        os.environ['FAKE_CODEX_PROMPT'] = str(self.prompt_log)
        os.environ['FAKE_CODEX_MODE'] = 'ok'
        self.catalog = Catalog(self.directory / 'state', [self.sources])
        self.catalog.refresh(asynchronous=False)
        self.editor = Editor(self.catalog, codex=str(self.fake))
        self.id = next(k for k, r in self.catalog.records.items() if r['filename'] == '报告.html')
        self.gbk_id = next(k for k, r in self.catalog.records.items() if r['filename'] == '旧编码.html')

    def tearDown(self):
        for job in list(self.editor.processes.values()):
            self.editor.kill(job)
        self.temp.cleanup()

    def digest(self, path=None):
        return hashlib.sha256((path or self.page).read_bytes()).hexdigest()

    def wait(self, job_id, seconds=15):
        deadline = time.time() + seconds
        while time.time() < deadline:
            job = self.editor.job(job_id)
            if job['status'] not in {'queued', 'running'}:
                return job
            time.sleep(0.05)
        self.fail('job did not finish')

    def test_preview_is_confined_and_script_free(self):
        info = self.editor.info(self.id)
        self.assertTrue(info['editable'])
        relative = info['preview'].split('/', 3)[3]
        body, kind, csp = self.editor.preview(self.id, relative)
        self.assertEqual(kind, 'text/html')
        self.assertIn("script-src 'none'", csp)
        self.assertIn("connect-src 'none'", csp)
        self.assertEqual(self.editor.preview(self.id, 'style.css')[1], 'text/css')
        for bad in ['../../etc/passwd', '%2e%2e/%2e%2e/etc/hosts', '.secret.css', 'missing.png', 'state/catalog.json']:
            with self.assertRaises(FileNotFoundError, msg=bad):
                self.editor.preview(self.id, bad)
        with self.assertRaises(FileNotFoundError):
            self.editor.preview('nope', 'style.css')

    def test_save_versions_conflict_restore_and_reindex(self):
        original = self.page.read_bytes()
        with self.assertRaises(Conflict):
            self.editor.save(self.id, '<p>x</p>', 'stale')
        result = self.editor.save(self.id, original.decode().replace('旧标题', '改过的标题'), self.digest())
        self.assertIn('改过的标题', self.page.read_text())
        self.assertEqual(result['digest'], self.digest())
        self.assertEqual(self.catalog.records[self.id]['title'], '改过的标题')
        self.assertEqual(self.catalog.view({'q': ['改过的标题']})['total'], 1)
        versions = self.editor.versions(self.id)
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0]['reason'], 'before-save')
        self.assertEqual(self.editor.save(self.id, self.page.read_text(), self.digest())['unchanged'], True)
        self.editor.restore(self.id, versions[0]['name'], self.digest())
        self.assertEqual(self.page.read_bytes(), original)
        self.assertEqual(len(self.editor.versions(self.id)), 2)
        with self.assertRaises(ValueError):
            self.editor.restore(self.id, '../../x.html', self.digest())
        with self.assertRaises(ValueError):
            self.editor.save(self.id, '   ', self.digest())

    def test_non_utf8_page_is_read_only(self):
        info = self.editor.info(self.gbk_id)
        self.assertFalse(info['editable'])
        with self.assertRaises(ValueError):
            self.editor.save(self.gbk_id, '<p>x</p>', self.digest(self.gbk))

    def test_annotations_are_sanitised(self):
        rows = [{'id': 'a1', 'selector': 'h1', 'kind': 'evil', 'status': 'weird', 'instruction': 'x' * 9000},
                {'id': 'a2', 'selector': 'img', 'kind': 'image', 'status': 'open'}]
        saved = self.editor.set_annotations(self.id, rows)['annotations']
        self.assertEqual(saved[0]['kind'], 'other')
        self.assertEqual(saved[0]['status'], 'open')
        self.assertEqual(len(saved[0]['instruction']), 4000)
        self.assertEqual(self.editor.annotations(self.id)[1]['kind'], 'image')
        with self.assertRaises(ValueError):
            self.editor.set_annotations(self.id, [{'id': str(i)} for i in range(201)])
        with self.assertRaises(ValueError):
            self.editor.set_annotations(self.id, [{'selector': 'no id'}])

    def test_upload_accepts_real_images_only(self):
        result = self.editor.upload(self.id, '截图 1.png', 'data:image/png;base64,' + base64.b64encode(PNG).decode())
        self.assertTrue(result['src'].startswith('%E6%8A%A5%E5%91%8A_assets/'))
        files = list((self.sources / '报告_assets').iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), PNG)
        for bad in [base64.b64encode(b'<svg onload=alert(1)>').decode(), 'not base64!!', '']:
            with self.assertRaises(ValueError):
                self.editor.upload(self.id, 'x.svg', bad)

    def test_codex_job_applies_with_backup_assets_and_undo(self):
        original = self.page.read_bytes()
        self.editor.set_annotations(self.id, [{'id': 'n1', 'selector': 'h1', 'text': '旧标题', 'kind': 'text',
                                               'instruction': '改成新标题'}])
        with self.assertRaises(ValueError):
            self.editor.start_job(self.id, '', [], self.digest())
        with self.assertRaises(Conflict):
            self.editor.start_job(self.id, '改', [], 'stale')
        job = self.editor.start_job(self.id, '整体更清楚', ['n1'], self.digest())['job']
        done = self.wait(job['id'])
        self.assertEqual(done['status'], 'applied', done)
        self.assertIn('新标题', self.page.read_text())
        self.assertEqual((self.sources / '报告_assets/codex-generated.png').read_bytes()[:8], PNG[:8])
        self.assertEqual(done['assets'], ['codex-generated.png'])
        self.assertIn('标题改为新标题', done['summary'])
        self.assertEqual(self.editor.annotations(self.id)[0]['status'], 'done')
        prompt = self.prompt_log.read_text()
        self.assertIn('整体更清楚', prompt)
        self.assertIn('改成新标题', prompt)
        self.assertIn('报告_assets', prompt)
        self.assertEqual(self.editor.versions(self.id)[0]['reason'], 'before-codex')
        self.editor.undo_job(job['id'], self.digest())
        self.assertEqual(self.page.read_bytes(), original)
        self.assertEqual(self.editor.job(job['id'])['status'], 'undone')

    def test_codex_failure_nochange_and_cancel_leave_source(self):
        original = self.page.read_bytes()
        self.editor.set_annotations(self.id, [{'id': 'n1', 'selector': 'h1', 'instruction': 'x'}])
        os.environ['FAKE_CODEX_MODE'] = 'fail'
        done = self.wait(self.editor.start_job(self.id, '', ['n1'], self.digest())['job']['id'])
        self.assertEqual(done['status'], 'failed')
        self.assertIn('boom', done['log'])
        self.assertEqual(self.editor.annotations(self.id)[0]['status'], 'open')
        os.environ['FAKE_CODEX_MODE'] = 'same'
        done = self.wait(self.editor.start_job(self.id, '改', [], self.digest())['job']['id'])
        self.assertEqual(done['status'], 'nochange')
        os.environ['FAKE_CODEX_MODE'] = 'sleep'
        job = self.editor.start_job(self.id, '改', ['n1'], self.digest())['job']
        time.sleep(0.4)
        self.assertEqual(self.editor.annotations(self.id)[0]['status'], 'sent')
        with self.assertRaises(Conflict):
            self.editor.save(self.id, '<p>during job</p>', self.digest())
        with self.assertRaises(Conflict):
            self.editor.start_job(self.id, '再改', [], self.digest())
        self.editor.cancel(job['id'])
        self.assertEqual(self.wait(job['id'])['status'], 'cancelled')
        self.assertEqual(self.page.read_bytes(), original)
        self.assertEqual(self.editor.annotations(self.id)[0]['status'], 'open')

    def test_source_changed_during_job_is_not_overwritten(self):
        os.environ['FAKE_CODEX_MODE'] = 'sleep'
        job = self.editor.start_job(self.id, '改', [], self.digest())['job']
        time.sleep(0.3)
        self.page.write_text('<p>外部程序改过</p>')
        process = self.editor.processes[job['id']]
        process.terminate()  # Ends the fake early with a non-zero code; simulate success instead below.
        self.wait(job['id'])
        work = self.editor.base / 'jobs' / job['id'] / 'work'
        record = dict(self.editor.jobs[job['id']], status='running')
        with self.editor.lock:
            self.editor.apply(record, work)
        self.assertEqual(record['status'], 'conflict')
        self.assertEqual(self.page.read_text(), '<p>外部程序改过</p>')

    def test_restart_marks_running_jobs_interrupted(self):
        os.environ['FAKE_CODEX_MODE'] = 'sleep'
        job = self.editor.start_job(self.id, '改', [], self.digest())['job']
        time.sleep(0.3)
        reborn = Editor(self.catalog, codex=str(self.fake))
        self.assertEqual(reborn.job(job['id'])['status'], 'interrupted')
        self.editor.cancel(job['id'])

    def test_restart_skips_malformed_job_metadata_and_keeps_valid_job(self):
        with patch('editor.threading.Thread.start'):
            queued = self.editor.start_job(self.id, 'fixture only', [], self.digest())['job']
        bad_rows = [None, [], 7, {}, {'id': 'missing-fields'},
                    {**queued, 'id': '../outside'}, {**queued, 'status': []},
                    {**queued, 'annotations': [None]}, {**queued, 'created': 'not a timestamp'},
                    {**queued, 'created': float('nan')}]
        for number, row in enumerate(bad_rows):
            if isinstance(row, dict) and row.get('id') == queued['id']:
                row = {**row, 'id': f'bad-{number}'}
            directory = self.editor.base / 'jobs' / f'bad-{number}'
            directory.mkdir()
            (directory / 'meta.json').write_text(json.dumps(row))
        restored = Editor(self.catalog, codex='')
        self.assertEqual(list(restored.jobs), [queued['id']])
        self.assertEqual(restored.job(queued['id'])['status'], 'interrupted')
        self.assertEqual(len(restored.job_list(self.id)), 1)
        self.assertEqual(len(list((self.editor.base / 'jobs').glob('bad-*/meta.json'))), len(bad_rows))

    def test_malformed_version_index_does_not_prevent_save(self):
        folder = self.editor.version_dir(self.id)
        folder.mkdir(parents=True)
        for bad in [None, 7, 'invalid', {'name': 'not a list'},
                    [{'name': '../outside.html'}], [{'name': '/outside.html'}]]:
            with self.subTest(index=bad):
                (folder / 'index.json').write_text(json.dumps(bad))
                self.assertEqual(self.editor.versions(self.id), [])
                before = self.page.read_bytes()
                result = self.editor.save(self.id, '<p>next ' + str(bad) + '</p>', self.digest())
                self.assertEqual((folder / result['version']).read_bytes(), before)
                self.assertEqual(len(self.editor.versions(self.id)), 1)

    def test_version_pruning_ignores_paths_outside_version_directory(self):
        folder = self.editor.version_dir(self.id)
        folder.mkdir(parents=True)
        sentinel = self.editor.base / 'outside.html'
        sentinel.write_text('fixture must survive pruning')
        rows = [{'name': '20261029T010101-000001-before-save.html'}] * 60
        rows.append({'name': '../../outside.html'})
        (folder / 'index.json').write_text(json.dumps(rows))
        self.editor.save(self.id, '<p>saved safely</p>', self.digest())
        self.assertTrue(sentinel.exists())
        self.assertEqual(sentinel.read_text(), 'fixture must survive pruning')

    def test_restart_reopens_notes_left_sent(self):
        self.editor.set_annotations(self.id, [{'id': 'n1', 'selector': 'h1', 'kind': 'text'}])
        os.environ['FAKE_CODEX_MODE'] = 'sleep'
        job = self.editor.start_job(self.id, '', ['n1'], self.digest())['job']
        time.sleep(0.3)
        reborn = Editor(self.catalog, codex=str(self.fake))
        self.assertEqual(reborn.annotations(self.id)[0]['status'], 'open')
        self.editor.cancel(job['id'])

    def test_preview_rejects_root_swapped_for_symlink(self):
        url_path = self.editor.preview_url(self.catalog.records[self.id]).split('/', 3)[3]
        self.editor.preview(self.id, url_path)
        other = self.directory / 'other'
        other.mkdir()
        (other / '报告.html').write_text('<p>别处</p>')
        self.sources.rename(self.directory / 'moved')
        self.sources.symlink_to(other)
        with self.assertRaises(FileNotFoundError):
            self.editor.preview(self.id, url_path)

    def test_codex_assets_never_written_through_symlinks(self):
        outside = self.directory / 'outside'
        outside.mkdir()
        (self.sources / '报告_assets').symlink_to(outside)
        done = self.wait(self.editor.start_job(self.id, '改', [], self.digest())['job']['id'])
        self.assertEqual(done['status'], 'failed')
        self.assertEqual(list(outside.iterdir()), [])
        self.assertIn('旧标题', self.page.read_text())
        (self.sources / '报告_assets').unlink()
        (self.sources / '报告_assets').mkdir()
        (self.sources / '报告_assets' / 'codex-generated.png').symlink_to(outside / 'target.png')
        done = self.wait(self.editor.start_job(self.id, '改', [], self.digest())['job']['id'])
        self.assertEqual(done['status'], 'applied')
        self.assertEqual(done['assets'], [])
        self.assertFalse((outside / 'target.png').exists())

    def test_change_after_backup_is_not_overwritten(self):
        original = self.page.read_bytes()
        real = self.editor.snapshot

        def racing_snapshot(*args, **kwargs):
            name = real(*args, **kwargs)
            self.page.write_text('<p>备份之后外部写入</p>')
            return name
        self.editor.snapshot = racing_snapshot
        with self.assertRaises(Conflict):
            self.editor.save(self.id, '<p>我的版本</p>', hashlib.sha256(original).hexdigest())
        self.assertEqual(self.page.read_text(), '<p>备份之后外部写入</p>')

    def test_change_during_temp_write_is_not_overwritten(self):
        original = self.page.read_bytes()
        real_fsync, page = os.fsync, self.page

        def racing_fsync(fd):
            real_fsync(fd)
            if page.read_bytes() == original:
                page.write_text('<p>写临时文件期间外部写入</p>')
        os.fsync = racing_fsync
        try:
            with self.assertRaises(Conflict):
                self.editor.save(self.id, '<p>我的版本</p>', hashlib.sha256(original).hexdigest())
        finally:
            os.fsync = real_fsync
        self.assertEqual(self.page.read_text(), '<p>写临时文件期间外部写入</p>')
        self.assertEqual([p.name for p in self.sources.iterdir() if p.name.endswith('.tmp')], [])

    def test_atomic_temporary_files_are_private_before_fsync(self):
        from editor import atomic_bytes
        seen = []
        real_fsync = os.fsync

        def inspect(fd):
            seen.append(os.fstat(fd).st_mode & 0o777)
            real_fsync(fd)

        previous = os.umask(0)
        try:
            for mode in (None, 0o600, 0o644):
                target = self.directory / ('atomic-' + str(mode))
                with patch('editor.os.fsync', side_effect=inspect):
                    atomic_bytes(target, b'private content', mode)
                self.assertEqual(target.stat().st_mode & 0o777, mode if mode is not None else 0o600)
        finally:
            os.umask(previous)
        self.assertEqual(seen, [0o600] * 3)

    def test_codex_job_directory_is_private_before_prompt_is_written(self):
        real_write_text = Path.write_text
        seen = []

        def inspect(path, *args, **kwargs):
            if path.name == 'prompt.md':
                seen.append(path.parent.stat().st_mode & 0o777)
            return real_write_text(path, *args, **kwargs)

        previous = os.umask(0)
        try:
            with patch.object(Path, 'write_text', inspect), patch('editor.threading.Thread'):
                self.editor.start_job(self.id, 'change text', [], self.digest())
        finally:
            os.umask(previous)
        self.assertEqual(seen, [0o700])

    def test_restart_closes_access_to_existing_job_artifacts(self):
        jobs = self.editor.base / 'jobs'
        legacy = jobs / 'legacy'
        legacy.mkdir(parents=True)
        (legacy / 'prompt.md').write_text('private historical instruction')
        jobs.chmod(0o755)
        legacy.chmod(0o755)
        Editor(self.catalog, codex='')
        self.assertEqual(jobs.stat().st_mode & 0o777, 0o700)
        self.assertEqual((legacy / 'prompt.md').read_text(), 'private historical instruction')

    def test_undo_refused_after_later_save(self):
        done = self.wait(self.editor.start_job(self.id, '改', [], self.digest())['job']['id'])
        self.assertEqual(done['status'], 'applied')
        self.editor.save(self.id, '<p>之后手动改过</p>', self.digest())
        with self.assertRaises(Conflict):
            self.editor.undo_job(done['id'], self.digest())
        self.assertEqual(self.page.read_text(), '<p>之后手动改过</p>')

    def test_client_cannot_override_job_owned_note_status(self):
        self.editor.set_annotations(self.id, [{'id': 'n1', 'selector': 'h1', 'instruction': '原要求'}])
        os.environ['FAKE_CODEX_MODE'] = 'sleep'
        job = self.editor.start_job(self.id, '', ['n1'], self.digest())['job']
        time.sleep(0.3)
        # During the job: edits and deletion of the sent note are ignored.
        self.editor.set_annotations(self.id, [])
        row = self.editor.annotations(self.id)[0]
        self.assertEqual((row['status'], row['instruction']), ('sent', '原要求'))
        self.editor.set_annotations(self.id, [{'id': 'n1', 'instruction': '偷改', 'status': 'done'}])
        self.assertEqual(self.editor.annotations(self.id)[0]['instruction'], '原要求')
        self.editor.cancel(job['id'])
        self.wait(job['id'])
        # After the job: a stale autosave still carrying "sent" cannot resurrect it.
        self.editor.set_annotations(self.id, [{'id': 'n1', 'instruction': '新要求', 'status': 'sent'}])
        row = self.editor.annotations(self.id)[0]
        self.assertEqual((row['status'], row['instruction']), ('open', '新要求'))


class EditorHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='html-editor-http-')
        self.directory = Path(self.temp.name).resolve()
        self.sources = self.directory / 'sources'
        self.sources.mkdir()
        self.page = self.sources / 'page.html'
        self.page.write_text('<title>页面</title><h1>原文</h1>')
        self.catalog = Catalog(self.directory / 'state', [self.sources])
        self.catalog.refresh(asynchronous=False)
        self.server = create_server(self.catalog, 0, Editor(self.catalog, codex=''))
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.id = next(iter(self.catalog.records))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, method, url, value=None, headers=None, raw=False):
        base = {'Host': f'127.0.0.1:{self.port}'}
        if method == 'POST':
            base.update({'Origin': f'http://127.0.0.1:{self.port}', 'X-HTML-Token': self.catalog.token,
                         'Content-Type': 'application/json'})
        base.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        connection.request(method, url, body=json.dumps(value).encode() if value is not None else None, headers=base)
        response = connection.getresponse()
        status, body, csp = response.status, response.read(), response.getheader('Content-Security-Policy')
        connection.close()
        return (status, body, csp) if raw else (status, json.loads(body))

    def test_editor_page_preview_and_save_over_http(self):
        status, body, csp = self.request('GET', '/edit?id=' + self.id, raw=True)
        self.assertEqual(status, 200)
        self.assertIn(self.catalog.token.encode(), body)
        self.assertIn("frame-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        status, info = self.request('GET', '/api/editor/info?id=' + self.id)
        self.assertEqual(status, 200)
        self.assertFalse(info['codex'])
        status, body, csp = self.request('GET', info['preview'], raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b'<h1>', body)
        self.assertIn("script-src 'none'", csp)
        self.assertEqual(self.request('GET', f'/p/{self.id}/../state/catalog.json', raw=True)[0], 404)
        self.assertEqual(self.request('GET', '/p/' + self.id + '/page.html',
                                      headers={'Host': 'evil.example'}, raw=True)[0], 403)
        payload = {'id': self.id, 'html': '<title>页面</title><h1>新文</h1>', 'digest': 'stale'}
        self.assertEqual(self.request('POST', '/api/editor/save', payload)[0], 409)
        self.assertEqual(self.request('POST', '/api/editor/save', payload, {'X-HTML-Token': 'bad'})[0], 403)
        payload['digest'] = info['digest']
        status, result = self.request('POST', '/api/editor/save', payload)
        self.assertEqual(status, 200, result)
        self.assertIn('新文', self.page.read_text())
        big = {'id': self.id, 'html': 'x' * 70_000, 'digest': result['digest']}
        self.assertEqual(self.request('POST', '/api/editor/save', big)[0], 200)
        self.assertEqual(self.request('POST', '/api/editor/cancel', {'job': 'x', 'pad': 'x' * 70_000})[0], 400)
        status, result = self.request('POST', '/api/editor/codex', {'id': self.id, 'instruction': 'x', 'digest': 'x'})
        self.assertEqual(status, 400)
        self.assertIn('codex', result['error'])
        self.assertEqual(self.request('GET', '/api/editor/info?id=missing')[0], 400)

    def test_invalid_annotation_enums_return_json_error_and_preserve_notes(self):
        endpoint = '/api/editor/annotations'
        note = {'id': 'fixture-note', 'instruction': 'keep this note', 'kind': 'text', 'status': 'open'}
        self.assertEqual(self.request('POST', endpoint, {'id': self.id, 'annotations': [note]})[0], 200)
        for field in ('kind', 'status'):
            for value in ([], {}, 7):
                with self.subTest(field=field, value=value):
                    status, payload = self.request('POST', endpoint, {
                        'id': self.id, 'annotations': [{**note, field: value}]})
                    self.assertEqual(status, 400)
                    self.assertIn('error', payload)
        status, payload = self.request('GET', endpoint + '?id=' + self.id)
        self.assertEqual(status, 200)
        self.assertEqual(payload['annotations'][0]['instruction'], 'keep this note')
        self.assertEqual(payload['annotations'][0]['kind'], 'text')

    def test_nonfinite_annotation_timestamp_is_replaced_with_current_time(self):
        status, payload = self.request('POST', '/api/editor/annotations', {
            'id': self.id, 'annotations': [{'id': 'fixture-note', 'created': float('inf')}]})
        self.assertEqual(status, 200)
        created = payload['annotations'][0]['created']
        self.assertLess(abs(created - time.time()), 5)
        json.dumps(payload, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
