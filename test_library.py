import tempfile
import stat
import unittest
from unittest.mock import patch
from pathlib import Path
from library import discover, extract, short_label


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='html-manager-test-')
        self.root = Path(self.temp.name).resolve()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, body):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path

    def test_extract_does_not_index_scripts_and_preserves_source(self):
        path = self.write('20261005-0900-需求.html', '<title>收入机会研究</title><style>CSS_SECRET</style>'
                          '<script>window.BAD=1; SCRIPT_SECRET</script><h1>传统行业的收入机会</h1>'
                          '<p>发现业务采用缺口</p>')
        before = path.read_bytes()
        record = extract(path)
        self.assertEqual(record['label'], '收入机会研究')
        self.assertIn('采用缺口', record['text'])
        self.assertNotIn('SCRIPT_SECRET', record['text'])
        self.assertNotIn('CSS_SECRET', record['text'])
        self.assertEqual(before, path.read_bytes())

    def test_discovery_exclusions_duplicates_and_stale_removal(self):
        self.write('a.html', '<h1>主文件</h1>')
        self.write('node_modules/x.html', '<p>dependency</p>')
        self.write('.ssh/x.html', '<p>private</p>')
        self.write('secrets.html', '<p>private</p>')
        self.write('.env.html', '<p>private</p>')
        self.write('credentials.html', '<p>private</p>')
        (self.root / 'linked.html').symlink_to(self.root / 'a.html')
        rows, report = discover([self.root, self.root])
        self.assertEqual([r['filename'] for r in rows], ['a.html'])
        self.assertFalse(report['limited'])
        (self.root / 'a.html').unlink()
        rows, _ = discover([self.root])
        self.assertEqual(rows, [])

    def test_label_is_traceable_and_bounded(self):
        label, basis = short_label('index', [], '20261005-0900-市场验证方法.html')
        self.assertEqual((label, basis), ('市场验证方法', '文件名'))
        self.assertLessEqual(len(short_label('研究' * 40, [], 'a.html')[0]), 20)

    def test_nosync_dependency_directories_are_not_scanned(self):
        self.write('report.html', '<title>用户资料</title>')
        self.write('node_modules.nosync/dependency.html', '<title>依赖文档</title>')
        self.write('venv.nosync/package.html', '<title>Python 包文档</title>')
        self.write('build.nosync/generated.html', '<title>构建产物</title>')
        self.write('reports.nosync/owner.html', '<title>另一份用户资料</title>')
        (self.root / 'node_modules').symlink_to('node_modules.nosync', target_is_directory=True)
        rows, report = discover([self.root])
        self.assertEqual({r['filename'] for r in rows}, {'report.html', 'owner.html'})
        self.assertEqual(report['ignored_dirs'], 4)
        self.assertIn(str(self.root / 'node_modules.nosync'), report['ignored_paths'])

    def test_missing_root_and_limits_are_disclosed(self):
        self.write('a.html', 'hello')
        rows, report = discover([self.root / 'missing', self.root], max_entries=1)
        self.assertEqual(rows, [])
        self.assertTrue(report['limited'])
        self.assertEqual(report['roots'][0]['status'], 'unavailable')
        self.assertGreater(report['issue_count'], 0)

    def test_encoding_and_oversize(self):
        path = self.root / 'gbk.html'
        path.write_bytes('<meta charset=gbk><title>市场机会</title>'.encode('gbk'))
        self.assertEqual(extract(path)['title'], '市场机会')
        path.write_bytes(b'x' * (4 * 1024 * 1024 + 1))
        record = extract(path)
        self.assertTrue(record['truncated'])
        self.assertLessEqual(len(record['text']), 100_000)
        self.assertTrue(record['digest'].startswith('partial:'))

    def test_heading_fallback_and_svg_title(self):
        path = self.write('文件名称.html', '<h1>真正的页面主题</h1>')
        self.assertEqual((extract(path)['label'], extract(path)['basis']), ('真正的页面主题', '页面小标题'))
        path.write_text('<title>收入研究</title><svg><title>下载图标</title></svg>')
        self.assertEqual(extract(path)['label'], '收入研究')

    def test_inline_markup_preserves_words_and_complete_headings(self):
        path = self.write('report.html', '<h1>如何<strong>找到</strong>需求</h1>'
                          '<h2>一个<span>完整</span>小标题</h2>'
                          '<p>人工<span>智能</span>用于<b>市场</b>研究</p>')
        original = path.read_bytes()
        record = extract(path)
        self.assertEqual((record['label'], record['basis']), ('如何找到需求', '页面小标题'))
        self.assertEqual(record['snippet'], '如何找到需求 · 一个完整小标题')
        self.assertIn('人工智能用于市场研究', record['text'])
        self.assertEqual(path.read_bytes(), original)

    def test_text_keeps_explicit_whitespace_and_separates_blocks(self):
        path = self.write('spacing.html', '<title>Spacing test</title>'
                          '<p>before <b>bold</b> after</p>'
                          '<div>first</div><div>second<br>third</div>'
                          '<table><tr><td>cell1</td><td>cell2</td></tr></table>'
                          '<p>co<em>operate</em></p>')
        self.assertEqual(extract(path)['text'],
                         'Spacing test before bold after first second third cell1 cell2 cooperate')

    def test_heading_line_break_stays_a_single_heading(self):
        path = self.write('heading.html', '<h1>First<br>second <em>part</em></h1>')
        record = extract(path)
        self.assertEqual(record['label'], 'First second part')
        self.assertEqual(record['snippet'], 'First second part')

    def test_unclosed_final_text_and_entities_are_flushed(self):
        for source, expected in [('<title>Research &amp', 'Research &'),
                                 ('<h1>AT&T', 'AT&T'),
                                 ('<p>End &unknown', 'End &unknown')]:
            with self.subTest(source=source):
                record = extract(self.write('eof.html', source))
                self.assertEqual(record['text'], expected)
                if source.startswith('<title>'):
                    self.assertEqual(record['title'], expected)
                if source.startswith('<h1>'):
                    self.assertEqual(record['label'], expected)

    def test_many_inline_nodes_stay_within_text_budget(self):
        path = self.write('inline-limit.html', '<p>' + '<span>x</span>' * 100_001 + '</p>')
        record = extract(path)
        self.assertLessEqual(len(record['text']), 100_000)
        self.assertTrue(record['truncated'])

    def test_catalog_exports_skipped_and_each_root_has_budget(self):
        self.write('catalog.html', '<meta name="html-manager-catalog" content="true"><title>管理器</title>')
        one = self.root / 'one'
        two = self.root / 'two'
        one.mkdir();two.mkdir()
        for n in range(8):
            (one / f'{n}.html').write_text('<title>第一组</title>')
        (two / 'second.html').write_text('<title>第二组</title>')
        rows, report = discover([one, two], root_entry_limit=3)
        self.assertIn('second.html', [r['filename'] for r in rows])
        self.assertEqual(report['roots'][0]['status'], 'partial_limit')
        rows, report = discover([self.root])
        self.assertEqual(report['ignored_catalogs'], 1)
        self.assertNotIn('catalog.html', [r['filename'] for r in rows])

    def test_unreadable_html_remains_filename_searchable_and_error_visible(self):
        path = self.write('离线报告.html', 'original')
        with patch('library.extract', side_effect=OSError(11, 'Resource deadlock avoided')):
            rows, report = discover([self.root])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['basis'], '文件名')
        self.assertTrue(rows[0]['unreadable'])
        self.assertEqual(report['roots'][0]['status'], 'partial_error')
        self.assertEqual(report['issue_count'], 1)

    def test_utf8_cut_inside_multibyte_character_keeps_title_and_encoding(self):
        path = self.root / '大中文报告.html'
        prefix = '<meta charset="utf-8"><title>最新资讯与机会地图</title><p>'.encode()
        padding = b'a' * ((4 * 1024 * 1024) - len(prefix) - 1)
        path.write_bytes(prefix + padding + '中文字符'.encode() + b'</p>')
        record = extract(path)
        self.assertEqual(record['encoding'], 'utf-8')
        self.assertEqual(record['label'], '最新资讯与机会地图')
        self.assertTrue(record['truncated'])

    def test_dataless_source_is_not_opened(self):
        path = self.write('云端报告.html', 'original')
        fake = type('Flags', (), {'st_flags': stat.SF_DATALESS})()
        with patch('library.allowed_path', return_value=True), patch.object(Path, 'stat', return_value=fake), patch('library.os.open') as opened:
            with self.assertRaisesRegex(OSError, '未下载正文'):
                extract(path)
            opened.assert_not_called()

    def test_root_permission_error_marks_unavailable(self):
        def denied(root, **kwargs):
            kwargs['onerror'](PermissionError(13, 'denied', str(root)))
            return iter([])
        with patch('library.os.walk', side_effect=denied):
            rows, report = discover([self.root])
        self.assertEqual(rows, [])
        self.assertEqual(report['roots'][0]['status'], 'unavailable')
        self.assertEqual(report['issue_count'], 1)


if __name__ == '__main__':
    unittest.main()
