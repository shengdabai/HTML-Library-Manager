import json
import re
import tempfile
import unittest
from pathlib import Path
from manager import Catalog, render_export

class PortableTests(unittest.TestCase):
    def test_inline_words_are_searchable_in_service_and_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / 'source'; root.mkdir()
            (root / 'a.html').write_text('<h1>研究<strong>方法</strong></h1>'
                                       '<p>人工<span>智能</span>的实际用途</p>')
            catalog = Catalog(Path(directory) / 'state', [root])
            catalog.refresh(asynchronous=False)
            self.assertEqual(catalog.view({'q': ['人工智能']})['total'], 1)
            self.assertEqual(catalog.view({'q': ['研究方法'], 'mode': ['meta']})['total'], 1)
            data = {'records': list(catalog.records.values()), 'report': catalog.report}
            page = render_export(data)
            payload = re.search(r'<script id="snapshot" type="application/json">(.*?)</script>', page, re.S).group(1)
            record = json.loads(payload)['records'][0]
            self.assertIn('人工智能', record['text'])
            self.assertEqual(record['label'], '研究方法')

    def test_marker_text_is_never_substituted_inside_data_or_fallback(self):
        markers = '__FALLBACK__ __SNAPSHOT__ </script><script>alert(1)</script> & <b>'
        data = {'records': [{'label': markers, 'title': markers, 'filename': markers, 'text': markers, 'snippet': markers}], 'report': {}}
        page = render_export(data)
        payload = re.search(r'<script id="snapshot" type="application/json">(.*?)</script>', page, re.S).group(1)
        self.assertEqual(json.loads(payload), data)
        self.assertNotIn('<script>alert', payload)
        self.assertIn('&lt;/script&gt;', page)

    def test_fullwidth_metadata_and_body_mode_and_all_terms(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / 'source'; root.mkdir()
            original = root / 'a.html'
            original.write_text('<title>ＦＴＳ 标题</title><p>' + ' '.join('word' + str(i) for i in range(14)) + '</p>')
            catalog = Catalog(Path(directory) / 'state', [root]); catalog.refresh(asynchronous=False)
            self.assertEqual(catalog.view({'q':['fts']})['total'], 1)
            self.assertEqual(catalog.view({'q':['word3'], 'mode':['meta']})['total'], 0)
            self.assertEqual(catalog.view({'q':['word3'], 'mode':['full']})['total'], 1)
            self.assertEqual(catalog.view({'q':[' '.join('word' + str(i) for i in range(13)) + ' absent']})['total'], 0)
            original.write_text('<title>替换后的索引</title>')
            catalog.refresh(asynchronous=False)
            self.assertEqual(catalog.view({'q':['fts']})['total'], 0)
            self.assertEqual(catalog.view({'q':['替换后的索引']})['total'], 1)

if __name__ == '__main__': unittest.main()
