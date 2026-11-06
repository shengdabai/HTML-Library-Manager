import http.client
import urllib.parse
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from manager import Catalog, create_server


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='html-manager-runtime-')
        self.directory = Path(self.temp.name).resolve()
        self.sources = self.directory / 'sources'
        self.sources.mkdir()
        self.html = self.sources / '报告 #1.html'
        self.html.write_text('<title>核心需求研究</title><h1>重点内容</h1><p>全文独有词 searchable</p>')
        self.catalog = Catalog(self.directory / 'state', [self.sources])
        self.catalog.refresh(asynchronous=False)
        self.server = create_server(self.catalog, 0)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, method, url, value=None, headers=None):
        base = {'Host': f'127.0.0.1:{self.port}'}
        if method == 'POST':
            base.update({'Origin': f'http://127.0.0.1:{self.port}',
                         'X-HTML-Token': self.catalog.token, 'Content-Type': 'application/json'})
        base.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        connection.request(method, url, body=json.dumps(value).encode() if value is not None else None, headers=base)
        response = connection.getresponse()
        status, body = response.status, response.read()
        connection.close()
        return status, json.loads(body)

    def identifier(self):
        return next(iter(self.catalog.records))

    def test_search_fulltext_unicode_and_no_source_serving(self):
        code, payload = self.request('GET', '/api/library?q=searchable')
        self.assertEqual(code, 200)
        self.assertEqual(payload['total'], 1)
        self.assertNotIn('text', payload['records'][0])
        self.assertEqual(self.request('GET', '/api/library?q=absent')[1]['total'], 0)
        self.assertEqual(self.request('GET', '/etc/passwd')[0], 404)
        self.assertEqual(self.request('GET', '/api/library?offset=oops')[0], 400)

    def test_host_origin_token_and_cross_site_enforcement(self):
        self.assertEqual(self.request('GET', '/api/library', headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.request('GET', '/api/library', headers={'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.request('GET', '/api/library', headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        for headers in [{'X-HTML-Token': ''}, {'Origin': 'null'}, {'Content-Type': 'text/plain'}]:
            self.assertEqual(self.request('POST', '/api/open', {'id': self.identifier()}, headers)[0], 403)

    def test_favorites_survive_restart(self):
        code, _ = self.request('POST', '/api/favorite', {'id': self.identifier()})
        self.assertEqual(code, 200)
        payload = self.request('GET', '/api/library?scope=favorites')[1]
        self.assertEqual(payload['total'], 1)
        again = Catalog(self.directory / 'state', [self.sources])
        self.assertEqual(again.view({'scope': ['favorites']})['total'], 1)

    def test_open_is_indexed_argument_array_and_tracks_history(self):
        with patch('manager.subprocess.run') as run:
            self.assertEqual(self.request('POST', '/api/open', {'id': self.identifier()})[0], 200)
            self.assertEqual(run.call_args.args[0], ['/usr/bin/open', str(self.html)])
        self.assertEqual(self.request('GET', '/api/library?scope=recent')[1]['total'], 1)
        self.assertEqual(self.request('POST', '/api/open', {'id': str(self.html)})[0], 400)

    def test_missing_and_replaced_symlink_refused(self):
        identifier = self.identifier()
        self.html.unlink()
        with patch('manager.subprocess.run') as run:
            self.assertEqual(self.request('POST', '/api/open', {'id': identifier})[0], 400)
            run.assert_not_called()
        target = self.directory / 'somewhere.html'
        target.write_text('<title>存在的符号链接目标</title>')
        self.html.symlink_to(target)
        with patch('manager.subprocess.run') as run:
            self.assertEqual(self.request('POST', '/api/open', {'id': identifier})[0], 400)
            run.assert_not_called()

    def test_refresh_added_changed_removed_files(self):
        self.html.write_text('<title>修改后的主题</title>')
        other = self.sources / 'new.htm'
        other.write_text('<title>新增页面</title>')
        self.catalog.refresh(asynchronous=False)
        self.assertEqual(len(self.catalog.records), 2)
        self.assertIn('修改后的主题', [r['title'] for r in self.catalog.records.values()])
        self.html.unlink()
        self.catalog.refresh(asynchronous=False)
        self.assertEqual(len(self.catalog.records), 1)

    def test_bad_request_and_sources_cannot_target_system_or_private_dirs(self):
        self.assertEqual(self.request('POST', '/api/roots', {'roots': ['/etc']})[0], 400)
        self.assertEqual(self.request('POST', '/api/roots', {'roots': []})[0], 400)
        self.assertEqual(self.request('POST', '/api/open', [self.identifier()])[0], 400)
        self.assertEqual(self.request('POST', '/api/open', {'id': {}})[0], 400)

    def test_removed_source_never_retained_by_uncertain_scan_or_restart(self):
        other = self.directory / 'other'
        other.mkdir()
        new = Catalog(self.directory / 'state', [other])
        self.assertEqual(len(new.records), 0)
        with patch('manager.discover', return_value=([], {'limited': True, 'issue_count': 1})):
            new.refresh(asynchronous=False)
        self.assertEqual(len(new.records), 0)
        self.assertEqual(len(Catalog(self.directory / 'state', [other]).records), 0)

    def test_theme_filter_uses_first_subfolder_and_lists_counts(self):
        themed = self.sources / '01-主题A' / '子目录'
        themed.mkdir(parents=True)
        (themed / 'a.html').write_text('<title>主题A页面</title>')
        (self.sources / '02-主题B').mkdir()
        (self.sources / '02-主题B' / 'b.html').write_text('<title>主题B页面</title>')
        self.catalog.refresh(asynchronous=False)
        payload = self.request('GET', '/api/library')[1]
        root = str(self.sources)
        self.assertEqual(payload['themes'], [{'root': root, 'name': '01-主题A', 'count': 1}, {'root': root, 'name': '02-主题B', 'count': 1}])
        code, payload = self.request('GET', '/api/library?theme=01-%E4%B8%BB%E9%A2%98A')
        self.assertEqual([r['title'] for r in payload['records']], ['主题A页面'])
        self.assertEqual(self.request('GET', '/api/library?theme=01-%E4%B8%BB%E9%A2%98A&theme_root=/nowhere')[1]['total'], 0)
        code, payload = self.request('GET', '/api/library?theme=01-%E4%B8%BB%E9%A2%98A&theme_root=' + urllib.parse.quote(root))
        self.assertEqual(code, 200)
        self.assertEqual([r['title'] for r in payload['records']], ['主题A页面'])
        self.assertEqual(self.request('GET', '/api/library?theme=missing')[1]['total'], 0)
        self.assertEqual(self.request('GET', '/api/library')[1]['total'], 3)

    def test_export_refuses_existing_unless_force_then_replaces_atomically(self):
        import manager
        out = self.directory / 'catalog.html'
        argv = ['manager.py', 'export', '--root', str(self.sources), '--out', str(out), '--state-dir', str(self.directory / 'export-state')]
        def run(*extra):
            with patch('manager.sys.argv', argv + list(extra)), patch('manager.validate_root', lambda value: Path(value)):
                manager.main()
        run()
        first = out.read_text()
        with self.assertRaises(FileExistsError):
            run()
        self.assertEqual(out.read_text(), first)
        (self.sources / 'later.html').write_text('<title>强制覆盖后新增</title>')
        run('--force')
        self.assertIn('强制覆盖后新增', out.read_text())
        self.assertEqual(oct(out.stat().st_mode & 0o777), '0o600')
        self.assertEqual([p.name for p in self.directory.iterdir() if p.name.startswith('.catalog.html')], [])

    def test_force_export_keeps_old_file_when_replace_or_write_fails(self):
        import manager
        out = self.directory / 'catalog.html'
        argv = ['manager.py', 'export', '--root', str(self.sources), '--out', str(out), '--state-dir', str(self.directory / 'export-state'), '--force']
        real_replace, real_fsync = os.replace, os.fsync
        def failing_replace(src, dst):  # Only the final swap into --out fails; scan-side atomic_json keeps working.
            if Path(dst) == out:
                raise OSError('replace blocked')
            return real_replace(src, dst)
        def failing_fsync(fd):  # Only the export temp file (sibling of --out, named .catalog.html.*.tmp) fails.
            import fcntl
            name = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b'\0').decode()
            if Path(name).parent == out.parent and Path(name).name.startswith('.' + out.name + '.'):
                raise OSError('disk full')
            return real_fsync(fd)
        def run(**patches):
            with patch('manager.sys.argv', argv), patch('manager.validate_root', lambda value: Path(value)):
                if patches:
                    with patch.multiple('manager.os', **patches):
                        manager.main()
                else:
                    manager.main()
        run()
        first = out.read_text()
        (self.sources / 'later.html').write_text('<title>替换失败不应出现</title>')
        with self.assertRaises(OSError):
            run(replace=failing_replace)
        self.assertEqual(out.read_text(), first)
        self.assertEqual([p.name for p in self.directory.iterdir() if p.name.startswith('.catalog.html')], [])
        with self.assertRaises(OSError):
            run(fsync=failing_fsync)
        self.assertEqual(out.read_text(), first)
        self.assertEqual([p.name for p in self.directory.iterdir() if p.name.startswith('.catalog.html')], [])

    def test_same_theme_name_under_two_roots_counted_and_filtered_per_root(self):
        other = self.directory / 'other'
        (other / '01-共同主题').mkdir(parents=True)
        (other / '01-共同主题' / 'o.html').write_text('<title>另一根目录页面</title>')
        (self.sources / '01-共同主题').mkdir()
        (self.sources / '01-共同主题' / 's.html').write_text('<title>主根目录页面</title>')
        catalog = Catalog(self.directory / 'state-two', [self.sources, other])
        catalog.refresh(asynchronous=False)
        payload = catalog.view({})
        self.assertEqual(payload['themes'], [{'root': str(self.sources), 'name': '01-共同主题', 'count': 1}, {'root': str(other), 'name': '01-共同主题', 'count': 1}])
        self.assertEqual([r['title'] for r in catalog.view({'theme': ['01-共同主题']})['records']], ['主根目录页面', '另一根目录页面'])
        self.assertEqual([r['title'] for r in catalog.view({'theme': ['01-共同主题'], 'theme_root': [str(other)]})['records']], ['另一根目录页面'])
        (other / '01-共同主题' / 'o.html').unlink()
        catalog.refresh(asynchronous=False)
        self.assertEqual(catalog.view({})['themes'], [{'root': str(self.sources), 'name': '01-共同主题', 'count': 1}])
        self.assertEqual(catalog.view({'theme': ['01-共同主题'], 'theme_root': [str(other)]})['total'], 0)

    def test_malformed_cache_and_state_recovers(self):
        state = self.directory / 'state'
        (state / 'catalog.json').write_text('{"records":[{"id":"bad"}]}')
        (state / 'state.json').write_text('{"favorites":{},"recent":[]}')
        new = Catalog(state, [self.sources])
        self.assertEqual(new.view({})['count'], 0)
        new.refresh(asynchronous=False)
        self.assertEqual(new.view({})['count'], 1)

    def test_nonfinite_cached_numbers_do_not_break_json_responses(self):
        sample = next(iter(self.catalog.records.values()))
        rows = [sample, dict(sample, id='bad-number', modified=float('nan'))]
        state = self.catalog.state_dir
        (state / 'catalog.json').write_text(json.dumps({'records': rows, 'report': {'elapsed': float('inf')}}))
        (state / 'state.json').write_text(json.dumps({'favorites': [], 'recent': {sample['id']: float('-inf')}}))
        restored = Catalog(state, [self.sources])
        payload = restored.view({})
        self.assertEqual(payload['total'], 1)
        self.assertEqual(payload['recent_count'], 0)
        json.dumps(payload, allow_nan=False)

    def test_excluded_cached_records_are_removed_on_restart_and_partial_scan(self):
        from library import extract
        excluded = []
        for directory in ['node_modules.nosync', 'build', 'Example.app']:
            folder = self.sources / directory
            folder.mkdir()
            page = folder / 'excluded.html'
            page.write_text('<title>不应被搜索的缓存</title>')
            excluded.append(dict(extract(page), root=str(self.sources), folder=directory))
        state = self.catalog.state_dir
        (state / 'catalog.json').write_text(json.dumps({'records': excluded, 'report': {}}))
        restarted = Catalog(state, [self.sources])
        self.assertEqual(restarted.view({})['total'], 0)
        self.catalog.records.update({r['id']: r for r in excluded})
        with patch('manager.discover', return_value=([], {'limited': True, 'issue_count': 1})):
            self.catalog.refresh(asynchronous=False)
        self.assertEqual([r['filename'] for r in self.catalog.view({})['records']], [self.html.name])

    def test_invalid_config_root_shapes_fall_back_to_defaults(self):
        from manager import default_roots
        for roots in [None, 'sources', [None], [7], [True], {}, [], [''], ['\x00'], ['relative'], ['~unknown-user']]:
            with self.subTest(roots=roots):
                (self.catalog.state_dir / 'config.json').write_text(json.dumps({'roots': roots}))
                restored = Catalog(self.catalog.state_dir)
                self.assertEqual(restored.roots, default_roots())

    def test_cached_sensitive_hidden_and_symlink_paths_are_not_searchable(self):
        sample = next(iter(self.catalog.records.values()))
        hidden = self.sources / '.hidden.html'
        sensitive = self.sources / 'credentials.fixture.html'
        linked = self.sources / 'linked.html'
        hidden.write_text('synthetic hidden fixture')
        sensitive.write_text('synthetic sensitive fixture')
        linked.symlink_to(self.html)
        rows = [dict(sample, id=str(number), path=str(path))
                for number, path in enumerate([hidden, sensitive, linked])]
        state = self.catalog.state_dir
        (state / 'catalog.json').write_text(json.dumps({'records': rows, 'report': {}}))
        restored = Catalog(state, [self.sources])
        self.assertEqual(restored.view({})['total'], 0)
        self.assertEqual(restored.records, {})

    def test_explicit_root_inside_normally_skipped_directory_remains_supported(self):
        from library import extract
        root = self.sources / 'build'
        root.mkdir()
        page = root / 'owner.html'
        page.write_text('<title>显式选择的资料目录</title>')
        row = dict(extract(page), root=str(root), folder='.')
        (self.catalog.state_dir / 'catalog.json').write_text(json.dumps({'records': [row], 'report': {}}))
        restored = Catalog(self.catalog.state_dir, [root])
        self.assertEqual(restored.view({})['total'], 1)
        restored.refresh(asynchronous=False)
        self.assertEqual(restored.view({})['total'], 1)

    def test_desktop_personal_and_development_have_separate_root_allowances(self):
        home = self.directory / 'home'
        personal = home / 'Desktop/02-学习资料'
        development = home / 'Desktop/01-项目开发'
        personal.mkdir(parents=True);development.mkdir()
        (personal / 'personal.html').write_text('<title>个人学习报告</title>')
        (development / 'dev.html').write_text('<title>开发报告</title>')
        with patch('manager.Path.home', return_value=home):
            new = Catalog(self.directory / 'partition-state')
            self.assertEqual(new.roots[-1], development)
            new.refresh(asynchronous=False)
        self.assertEqual(len(new.records), 2)
        roots = {r['filename']: r['root'] for r in new.records.values()}
        self.assertEqual(roots['personal.html'], str(home / 'Desktop'))
        self.assertEqual(roots['dev.html'], str(development))


if __name__ == '__main__':
    unittest.main()
