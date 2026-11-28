"""Video text extraction and cache migration in a temporary library."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from engine import library

PROJECT = Path(__file__).resolve().parents[2]


class LibraryExtraction(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ('text', 'videos', 'transcripts', 'manifests'):
            (self.root / name).mkdir()
        (self.root / 'videos/example.mp4').write_bytes(b'video fixture')
        self.record = {'id': 'video-example', 'path': 'videos/example.mp4', 'kind': 'video',
                       'title': 'Container labels', 'notes': 'An example recording.',
                       'sha256': 'a' * 64, 'collection': 'Examples', 'category': 'reference',
                       'captions': ['transcripts/example.vtt']}
        self.textpath = self.root / 'text/video-example.txt'
        self.metapath = self.textpath.with_suffix('.json')
        for name, value in (('ROOT', self.root), ('DB', self.root / 'search.sqlite')):
            setting = patch.object(library, name, value)
            setting.start()
            self.addCleanup(setting.stop)

    def caption(self, text, path='transcripts/example.vtt'):
        dest = self.root / path
        dest.write_text('WEBVTT\n\n00:01.000 --> 00:05.000\n' + text + '\n', encoding='utf-8')
        return dest

    def extract(self, **changes):
        record, pages, error = library.extract({**self.record, **changes})
        self.assertIsNone(error)
        self.assertEqual(record['text_path'], 'text/video-example.txt')
        self.assertEqual(self.textpath.read_text(encoding='utf-8'), '\n\f\n'.join(pages))
        return pages

    def legacy_cache(self, text, **details):
        self.textpath.write_text(text, encoding='utf-8')
        self.metapath.write_text(json.dumps({'sha256': self.record['sha256'], 'details': details}), encoding='utf-8')

    def build(self):
        (self.root / 'manifests/examples.json').write_text(json.dumps([self.record]), encoding='utf-8')
        with redirect_stdout(io.StringIO()):
            library.build()
        catalog = json.loads((self.root / 'catalog.json').read_text(encoding='utf-8'))
        summary = json.loads((self.root / 'summary.json').read_text(encoding='utf-8'))
        return catalog[0], summary

    def test_video_formats_keep_numeric_payload_and_skip_metadata(self):
        data = (b'\xef\xbb\xbfWEBVTT\r\nKind: captions\r\n\r\n'
                b'17\r\n00:01.000 --> 00:05.000\r\nThe label reads\r\n12\r\nlitres.\r\n\r\n'
                b'NOTE editor notes\r\nDo not include this comment.\r\n\r\n'
                b'chapter-two\r\n00:05.000 --> 00:08.000\r\n'
                b'<b>NOTE</b> this &amp; that are spoken.\r\n')
        (self.root / self.record['captions'][0]).write_bytes(data)
        for kind in ('video', 'mp4', 'webm', 'mkv'):
            with self.subTest(kind=kind):
                self.textpath.unlink(missing_ok=True)
                self.metapath.unlink(missing_ok=True)
                self.assertEqual(self.extract(kind=kind), ['The label reads\n12\nlitres.\nNOTE this & that are spoken.'])

    def test_legacy_cache_is_rebuilt_with_the_same_video_checksum(self):
        self.caption('The label reads\n12\nlitres.')
        self.legacy_cache('The label reads\nlitres.')
        self.assertEqual(self.extract(), ['The label reads\n12\nlitres.'])
        self.assertEqual(json.loads(self.metapath.read_text())['sha256'], self.record['sha256'])

    def test_reviewed_cue_boundaries_keep_payload_and_rolling_lines(self):
        for whitespace in (' ', '\t', ' \t ', '\u00a0'):
            with self.subTest(whitespace=repr(whitespace)):
                data = ('WEBVTT\n\n00:01.000 --> 00:05.000\nThe label reads\n'
                        + whitespace + '\n12\nlitres.\n' + whitespace
                        + '\n\nWEBVTT\n00:05.000 --> 00:08.000\nlitres.\nA later cue.\n')
                (self.root / self.record['captions'][0]).write_text(data, encoding='utf-8')
                self.assertEqual(self.extract(), ['The label reads\n12\nlitres.\nA later cue.'])

    def test_version_one_cache_is_rebuilt_with_unchanged_caption_inputs(self):
        self.caption('A corrected caption.\n12')
        expected = self.extract()
        metadata = json.loads(self.metapath.read_text(encoding='utf-8'))
        metadata['caption_inputs']['version'] = 1
        self.metapath.write_text(json.dumps(metadata), encoding='utf-8')
        self.textpath.write_text('Text from the earlier caption reader.', encoding='utf-8')
        self.assertEqual(self.extract(), expected)
        current = json.loads(self.metapath.read_text(encoding='utf-8'))
        self.assertEqual(current['caption_inputs']['version'], 2)
        self.assertEqual(current['caption_inputs']['files'], metadata['caption_inputs']['files'])

    def test_unchanged_inputs_reuse_the_cached_text(self):
        self.caption('An unchanged caption.')
        expected = self.extract()
        with patch.object(Path, 'write_text', side_effect=AssertionError('rewrote unchanged extract')):
            self.assertEqual(self.extract(), expected)

    def test_caption_edit_invalidates_cache_even_with_same_size_and_mtime(self):
        path = self.caption('First caption.')
        self.assertEqual(self.extract(), ['First caption.'])
        stat = path.stat()
        self.caption('Other caption.')
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(path.stat().st_size, stat.st_size)
        self.assertEqual(self.extract(), ['Other caption.'])

    def test_added_reordered_and_removed_tracks_refresh_the_cache(self):
        first = self.record['captions'][0]
        second = 'transcripts/second.vtt'
        self.caption('First track.')
        self.caption('Deuxième piste.', second)
        self.assertEqual(self.extract(), ['First track.'])
        self.assertEqual(self.extract(captions=[first, second]), ['First track.\nDeuxième piste.'])
        self.assertEqual(self.extract(captions=[second, first]), ['Deuxième piste.\nFirst track.'])
        self.assertEqual(self.extract(captions=[second]), ['Deuxième piste.'])
        self.assertEqual(self.extract(captions=[]), ['Container labels\nAn example recording.'])

    def test_fallback_title_and_notes_are_not_stuck_in_cache(self):
        self.assertEqual(self.extract(captions=[]), ['Container labels\nAn example recording.'])
        self.assertEqual(self.extract(captions=[], title='Updated title', notes='Updated notes.'),
                         ['Updated title\nUpdated notes.'])

    def test_metadata_only_captions_use_the_record_fallback(self):
        (self.root / self.record['captions'][0]).write_text('WEBVTT\n\nNOTE\nEditor only.\n', encoding='utf-8')
        self.assertEqual(self.extract(), ['Container labels\nAn example recording.'])

    def test_missing_caption_reports_an_error_without_replacing_cache(self):
        path = self.caption('Previously available caption.')
        self.extract()
        before = (self.textpath.read_bytes(), self.metapath.read_bytes())
        path.unlink()
        record, pages, error = library.extract(dict(self.record))
        self.assertEqual(pages, ['Previously available caption.'])
        self.assertEqual(record['text_path'], 'text/video-example.txt')
        self.assertIsNotNone(error)
        self.assertEqual((self.textpath.read_bytes(), self.metapath.read_bytes()), before)

    def test_rebuild_keeps_cached_video_searchable_until_captions_return(self):
        for version in ('legacy', 1, 2):
            with self.subTest(cache_version=version):
                path = self.caption('The label reads\n12\nlitres.')
                self.build()
                self.assertEqual([r['doc_id'] for r in library.search('12')], ['video-example'])
                metadata = json.loads(self.metapath.read_text(encoding='utf-8'))
                if version == 'legacy':
                    metadata.pop('caption_inputs')
                else:
                    metadata['caption_inputs']['version'] = version
                self.metapath.write_text(json.dumps(metadata), encoding='utf-8')
                before = (self.textpath.read_bytes(), self.metapath.read_bytes())
                path.unlink()

                for _ in range(2):
                    record, summary = self.build()
                    self.assertEqual([r['doc_id'] for r in library.search('12')], ['video-example'])
                    self.assertEqual(record['text_path'], 'text/video-example.txt')
                    self.assertEqual(summary['indexed_units'], 1)
                    self.assertEqual(len(summary['extraction_errors']), 1)
                    self.assertEqual(summary['extraction_errors'][0]['path'], self.record['path'])
                    self.assertIn(path.name, summary['extraction_errors'][0]['error'])
                    self.assertEqual((self.textpath.read_bytes(), self.metapath.read_bytes()), before)

                self.caption('The label reads\n34\nlitres.')
                _, summary = self.build()
                self.assertEqual(library.search('12'), [])
                self.assertEqual([r['doc_id'] for r in library.search('34')], ['video-example'])
                self.assertEqual(summary['extraction_errors'], [])
                self.assertEqual(json.loads(self.metapath.read_text())['caption_inputs']['version'], 2)

    def test_rebuild_reports_missing_captions_without_using_an_invalid_cache(self):
        cases = ('no cache', 'missing text', 'missing metadata', 'invalid metadata',
                 'non-object metadata', 'different video', 'empty text', 'invalid text encoding')
        for case in cases:
            with self.subTest(cache=case):
                self.textpath.unlink(missing_ok=True)
                self.metapath.unlink(missing_ok=True)
                path = self.caption('Cached caption 12.')
                self.build()
                path.unlink()
                if case in ('no cache', 'missing text'):
                    self.textpath.unlink()
                if case in ('no cache', 'missing metadata'):
                    self.metapath.unlink()
                if case == 'invalid metadata':
                    self.metapath.write_text('{', encoding='utf-8')
                elif case == 'non-object metadata':
                    self.metapath.write_text('[]', encoding='utf-8')
                elif case == 'different video':
                    metadata = json.loads(self.metapath.read_text(encoding='utf-8'))
                    metadata['sha256'] = 'b' * 64
                    self.metapath.write_text(json.dumps(metadata), encoding='utf-8')
                elif case == 'empty text':
                    self.textpath.write_text(' \n\f\n\t', encoding='utf-8')
                elif case == 'invalid text encoding':
                    self.textpath.write_bytes(b'\xff')
                record, summary = self.build()
                self.assertEqual(library.search('12'), [])
                self.assertNotIn('text_path', record)
                self.assertEqual(summary['indexed_units'], 0)
                self.assertEqual(len(summary['extraction_errors']), 1)
                self.assertIn(path.name, summary['extraction_errors'][0]['error'])

    def test_rebuild_does_not_replace_cached_tracks_with_partial_captions(self):
        second = 'transcripts/second.vtt'
        self.record['captions'].append(second)
        self.caption('First track 12.')
        path = self.caption('Second track 34.', second)
        self.build()
        before = (self.textpath.read_bytes(), self.metapath.read_bytes())
        self.caption('Changed first track 56.')
        path.unlink()
        _, summary = self.build()
        for query in ('12', '34'):
            self.assertEqual([r['doc_id'] for r in library.search(query)], ['video-example'])
        self.assertEqual(library.search('56'), [])
        self.assertEqual(len(summary['extraction_errors']), 1)
        self.assertEqual((self.textpath.read_bytes(), self.metapath.read_bytes()), before)
        self.caption('Changed second track 78.', second)
        _, summary = self.build()
        for query in ('12', '34'):
            self.assertEqual(library.search(query), [])
        for query in ('56', '78'):
            self.assertEqual([r['doc_id'] for r in library.search(query)], ['video-example'])
        self.assertEqual(summary['extraction_errors'], [])

    def test_missing_caption_rejects_non_object_details_without_changing_record(self):
        cases = (None, 12, 'invalid details', [],
                 [['title', 'Stale title'], ['notes', 'Stale notes.']],
                 [['title', 'Stale title'], ['notes', 'Stale notes.'], ['invalid']])
        for details in cases:
            with self.subTest(details=details):
                self.textpath.unlink(missing_ok=True)
                self.metapath.unlink(missing_ok=True)
                path = self.caption('Previously available caption 12.')
                original, _ = self.build()
                metadata = json.loads(self.metapath.read_text(encoding='utf-8'))
                metadata['details'] = details
                self.metapath.write_text(json.dumps(metadata), encoding='utf-8')
                before = (self.textpath.read_bytes(), self.metapath.read_bytes())
                path.unlink()

                record, pages, error = library.extract(dict(self.record))
                self.assertEqual(record, self.record)
                self.assertEqual(pages, [])
                self.assertIsNotNone(error)
                self.assertIn(path.name, error)
                record, summary = self.build()
                self.assertEqual(record, {k: v for k, v in original.items() if k != 'text_path'})
                self.assertEqual(library.search('12'), [])
                self.assertEqual(summary['indexed_units'], 0)
                self.assertEqual(len(summary['extraction_errors']), 1)
                self.assertIn(path.name, summary['extraction_errors'][0]['error'])
                self.assertEqual((self.textpath.read_bytes(), self.metapath.read_bytes()), before)

                self.caption('Previously available caption 12.')
                record, summary = self.build()
                self.assertEqual(record, original)
                self.assertEqual([r['title'] for r in library.search('12')], [original['title']])
                self.assertEqual(summary['extraction_errors'], [])
                self.assertEqual(self.textpath.read_bytes(), before[0])
                metadata['details'] = {}
                self.assertEqual(json.loads(self.metapath.read_text(encoding='utf-8')), metadata)

    def test_caption_permission_error_keeps_the_cached_extract(self):
        self.caption('Previously available caption.')
        self.extract()
        before = (self.textpath.read_bytes(), self.metapath.read_bytes())
        with patch.object(Path, 'read_bytes', side_effect=PermissionError('caption access denied')):
            record, pages, error = library.extract(dict(self.record))
        self.assertEqual(pages, ['Previously available caption.'])
        self.assertEqual(record['text_path'], 'text/video-example.txt')
        self.assertEqual(error, 'caption access denied')
        self.assertEqual((self.textpath.read_bytes(), self.metapath.read_bytes()), before)

    def test_missing_text_file_is_rebuilt(self):
        self.caption('A caption to restore.')
        self.extract()
        self.textpath.unlink()
        self.assertEqual(self.extract(), ['A caption to restore.'])

    def test_rebuild_recovers_blank_video_text_with_unchanged_captions(self):
        for text in ('', ' \n\t', '\n\f\n', '\u00a0'):
            with self.subTest(cached_text=repr(text)):
                self.textpath.unlink(missing_ok=True)
                self.metapath.unlink(missing_ok=True)
                source = self.caption('Café label 12.')
                original, _ = self.build()
                before = (self.textpath.read_bytes(), self.metapath.read_bytes(), source.read_bytes())
                self.textpath.write_text(text, encoding='utf-8')

                record, summary = self.build()
                self.assertEqual(record, original)
                self.assertEqual(summary['indexed_units'], 1)
                self.assertEqual(summary['extraction_errors'], [])
                self.assertEqual([r['doc_id'] for r in library.search('12')], ['video-example'])
                self.assertEqual((self.textpath.read_bytes(), self.metapath.read_bytes(), source.read_bytes()), before)
                with patch.object(Path, 'write_text', side_effect=AssertionError('rewrote repaired extract')):
                    self.assertEqual(self.extract(), ['Café label 12.'])

    def test_rebuild_recovers_blank_video_text_without_caption_tracks(self):
        self.record['captions'] = []
        for text in ('', ' \n\t', '\n\f\n', '\u00a0'):
            with self.subTest(cached_text=repr(text)):
                self.textpath.unlink(missing_ok=True)
                self.metapath.unlink(missing_ok=True)
                original, _ = self.build()
                before = (self.textpath.read_bytes(), self.metapath.read_bytes())
                self.textpath.write_text(text, encoding='utf-8')

                record, summary = self.build()
                self.assertEqual(record, original)
                self.assertEqual(summary['indexed_units'], 1)
                self.assertEqual(summary['extraction_errors'], [])
                for query in ('labels', 'recording'):
                    self.assertEqual([r['doc_id'] for r in library.search(query)], ['video-example'])
                self.assertEqual((self.textpath.read_bytes(), self.metapath.read_bytes()), before)
                with patch.object(Path, 'write_text', side_effect=AssertionError('rewrote repaired fallback')):
                    self.assertEqual(self.extract(), ['Container labels\nAn example recording.'])

    def test_rebuild_reports_missing_caption_with_blank_video_text(self):
        for text in ('', ' \n\t', '\n\f\n', '\u00a0'):
            with self.subTest(cached_text=repr(text)):
                self.textpath.unlink(missing_ok=True)
                self.metapath.unlink(missing_ok=True)
                source = self.caption('Café label 12.')
                original, _ = self.build()
                self.textpath.write_text(text, encoding='utf-8')
                before = (self.textpath.read_bytes(), self.metapath.read_bytes())
                source.unlink()

                for _ in range(2):
                    record, summary = self.build()
                    self.assertEqual(record, {k: v for k, v in original.items() if k != 'text_path'})
                    for query in ('12', 'labels', 'recording'):
                        self.assertEqual(library.search(query), [])
                    self.assertEqual(summary['indexed_units'], 0)
                    self.assertEqual(len(summary['extraction_errors']), 1)
                    self.assertEqual(summary['extraction_errors'][0]['path'], self.record['path'])
                    self.assertIn(source.name, summary['extraction_errors'][0]['error'])
                    self.assertEqual((self.textpath.read_bytes(), self.metapath.read_bytes()), before)

                self.caption('Café label 12.')
                record, summary = self.build()
                self.assertEqual(record, original)
                self.assertEqual(summary['indexed_units'], 1)
                self.assertEqual(summary['extraction_errors'], [])
                self.assertEqual([r['doc_id'] for r in library.search('12')], ['video-example'])

    def test_rebuild_recovers_unreadable_extraction_caches(self):
        cases = ('invalid metadata', 'non-object metadata', 'invalid metadata encoding',
                 'invalid text encoding', 'invalid details')
        for kind in ('video', 'txt'):
            for case in cases:
                with self.subTest(kind=kind, cache=case):
                    self.textpath.unlink(missing_ok=True)
                    self.metapath.unlink(missing_ok=True)
                    if kind == 'video':
                        source = self.caption('Café label 12.')
                        self.record.update(kind=kind, path='videos/example.mp4')
                    else:
                        source = self.root / 'example.txt'
                        source.write_text('Café label 12.', encoding='utf-8')
                        self.record.update(kind=kind, path='example.txt')
                    self.build()
                    before = (self.textpath.read_bytes(), self.metapath.read_bytes(), source.read_bytes())
                    if case == 'invalid metadata':
                        self.metapath.write_text('{', encoding='utf-8')
                    elif case == 'non-object metadata':
                        self.metapath.write_text('[]', encoding='utf-8')
                    elif case == 'invalid metadata encoding':
                        self.metapath.write_bytes(b'\xff')
                    elif case == 'invalid text encoding':
                        self.textpath.write_bytes(b'\xff')
                    else:
                        metadata = json.loads(self.metapath.read_text(encoding='utf-8'))
                        metadata['details'] = 12
                        self.metapath.write_text(json.dumps(metadata), encoding='utf-8')
                    record, summary = self.build()
                    self.assertEqual(record['text_path'], 'text/video-example.txt')
                    self.assertEqual(summary['indexed_units'], 1)
                    self.assertEqual(summary['extraction_errors'], [])
                    self.assertEqual([r['doc_id'] for r in library.search('12')], ['video-example'])
                    self.assertEqual((self.textpath.read_bytes(), self.metapath.read_bytes(), source.read_bytes()), before)

    def test_rebuild_rejects_non_object_details_without_changing_record(self):
        cases = (None, 12, 'invalid details', [],
                 [['title', 'Stale title'], ['notes', 'Stale notes.']],
                 [['title', 'Stale title'], ['notes', 'Stale notes.'], ['invalid']])
        for kind in ('video', 'txt'):
            for details in cases:
                with self.subTest(kind=kind, details=details):
                    self.textpath.unlink(missing_ok=True)
                    self.metapath.unlink(missing_ok=True)
                    if kind == 'video':
                        source = self.caption('Café label 12.')
                        self.record.update(kind=kind, path='videos/example.mp4')
                    else:
                        source = self.root / 'example.txt'
                        source.write_text('Café label 12.', encoding='utf-8')
                        self.record.update(kind=kind, path='example.txt')
                    original, _ = self.build()
                    before = (self.textpath.read_bytes(), self.metapath.read_bytes(), source.read_bytes())
                    metadata = json.loads(self.metapath.read_text(encoding='utf-8'))
                    metadata['details'] = details
                    self.metapath.write_text(json.dumps(metadata), encoding='utf-8')

                    record, summary = self.build()
                    self.assertEqual(record, original)
                    self.assertEqual(summary['indexed_units'], 1)
                    self.assertEqual(summary['extraction_errors'], [])
                    self.assertEqual([r['title'] for r in library.search('12')], [original['title']])
                    self.assertEqual((self.textpath.read_bytes(), self.metapath.read_bytes(), source.read_bytes()), before)

    def test_changed_video_checksum_still_invalidates_cache(self):
        self.caption('The current caption.')
        self.extract()
        self.textpath.write_text('A cached sentinel.', encoding='utf-8')
        self.assertEqual(self.extract(sha256='b' * 64), ['The current caption.'])

    def test_cached_pdf_text_and_details_are_unchanged(self):
        self.legacy_cache('First page\n\f\nSecond page', pages=2, searchability='text')
        with patch.object(Path, 'write_text', side_effect=AssertionError('rewrote cached PDF')):
            record, pages, error = library.extract({**self.record, 'kind': 'pdf', 'path': 'documents/absent.pdf'})
        self.assertIsNone(error)
        self.assertEqual(pages, ['First page', 'Second page'])
        self.assertEqual(record['pages'], 2)
        self.assertEqual(record['searchability'], 'text')

    def test_plain_text_extraction_is_unchanged(self):
        path = self.root / 'example.txt'
        path.write_text('Text with 12 and café.\nA second line.', encoding='utf-8')
        self.assertEqual(self.extract(kind='txt', path='example.txt'), ['Text with 12 and café.\nA second line.'])

    def test_direct_cli_build_refreshes_legacy_text_and_search_rows(self):
        self.caption('The label reads\n12\nlitres.')
        self.legacy_cache('The label reads\nlitres.')
        (self.root / 'manifests/examples.json').write_text(json.dumps([self.record]), encoding='utf-8')
        # A library's vendor directory must not supply the parser imported by the program.
        vendor = self.root / 'tools' / ('vendor-py312' if sys.version_info[:2] == (3, 12) else 'vendor')
        vendor.mkdir(parents=True)
        (vendor / 'saved_pages.py').write_text("raise AssertionError('loaded a helper from the data folder')\n", encoding='utf-8')
        env = {**os.environ, 'LASTBOOK_LIBRARY': str(self.root)}

        def run(*args):
            result = subprocess.run([sys.executable, '-B', str(PROJECT / 'engine/library.py'), *args],
                                    cwd=self.root, env=env, capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout

        run('build')
        self.assertEqual([r['doc_id'] for r in json.loads(run('search', '12'))], ['video-example'])
        self.caption('The label reads\n34\nlitres.')
        run('build')
        self.assertEqual(json.loads(run('search', '12')), [])
        self.assertEqual([r['doc_id'] for r in json.loads(run('search', '34'))], ['video-example'])
        self.assertEqual(json.loads((self.root / 'summary.json').read_text())['extraction_errors'], [])

    def test_module_cli_remains_available(self):
        result = subprocess.run([sys.executable, '-B', '-m', 'engine.library', '--help'], cwd=PROJECT,
                                env={**os.environ, 'LASTBOOK_LIBRARY': str(self.root)}, capture_output=True,
                                text=True, encoding='utf-8', errors='replace', timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('build', result.stdout)


class PdfOcrCache(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ('text', 'ocr', 'documents', 'manifests'):
            (self.root / name).mkdir()
        self.record = {'id': 'pdf-example', 'path': 'documents/example.pdf', 'kind': 'pdf',
                       'title': 'Container labels', 'sha256': 'a' * 64,
                       'collection': 'Examples', 'category': 'reference'}
        self.ocrpath = self.root / 'ocr/pdf-example.json'
        self.textpath = self.root / 'text/pdf-example.txt'
        self.original = 'Original label 12. ' * 8
        stream = ('BT /F1 12 Tf 20 200 Td (' + self.original + ') Tj ET').encode('ascii')
        objects = [b'<</Type/Catalog/Pages 2 0 R>>', b'<</Type/Pages/Kids[3 0 R]/Count 1>>',
                   b'<</Type/Page/Parent 2 0 R/MediaBox[0 0 600 300]/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>',
                   b'<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>',
                   b'<</Length ' + str(len(stream)).encode('ascii') + b'>>\nstream\n' + stream + b'\nendstream']
        data, offsets = bytearray(b'%PDF-1.4\n'), []
        for number, obj in enumerate(objects, 1):
            offsets.append(len(data))
            data += b'%d 0 obj\n' % number + obj + b'\nendobj\n'
        xref = len(data)
        data += b'xref\n0 6\n0000000000 65535 f \n'
        data += b''.join(b'%010d 00000 n \n' % offset for offset in offsets)
        data += b'trailer\n<</Size 6/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n' % xref
        self.source = self.root / self.record['path']
        self.source.write_bytes(data)
        (self.root / 'manifests/examples.json').write_text(json.dumps([self.record]), encoding='utf-8')
        for name, value in (('ROOT', self.root), ('DB', self.root / 'search.sqlite')):
            setting = patch.object(library, name, value)
            setting.start()
            self.addCleanup(setting.stop)

    def build(self):
        with redirect_stdout(io.StringIO()):
            library.build()
        catalog = json.loads((self.root / 'catalog.json').read_text(encoding='utf-8'))
        summary = json.loads((self.root / 'summary.json').read_text(encoding='utf-8'))
        return catalog[0], summary

    def test_unreadable_ocr_cache_does_not_stop_a_library_rebuild(self):
        cases = (b'{', b'\xff', b'[]', b'null',
                 json.dumps({'sha256': self.record['sha256'], 'pages': None}).encode('utf-8'),
                 json.dumps({'sha256': self.record['sha256'], 'pages': [12]}).encode('utf-8'),
                 json.dumps({'sha256': self.record['sha256'], 'pages': [{'text': None}]}).encode('utf-8'),
                 json.dumps({'sha256': self.record['sha256'], 'pages': [{'text': ['bad'] * 101}]}).encode('utf-8'))
        source = self.source.read_bytes()
        for cache in cases:
            with self.subTest(cache=cache):
                self.textpath.unlink(missing_ok=True)
                self.textpath.with_suffix('.json').unlink(missing_ok=True)
                self.ocrpath.write_bytes(cache)
                record, summary = self.build()
                self.assertEqual(record['text_path'], 'text/pdf-example.txt')
                self.assertEqual(record['pages'], 1)
                self.assertEqual(record['searchability'], 'text')
                self.assertNotIn('ocr_path', record)
                self.assertEqual(summary['indexed_units'], 1)
                self.assertEqual(summary['extraction_errors'], [])
                self.assertEqual([r['doc_id'] for r in library.search('12')], ['pdf-example'])
                self.assertEqual(self.ocrpath.read_bytes(), cache)
                self.assertEqual(self.source.read_bytes(), source)

    def test_valid_ocr_pages_still_take_precedence_over_original_pdf_text(self):
        pages = ['OCR label 34. ' * 8, 'Deuxième page 56. ' * 8]
        self.ocrpath.write_text(json.dumps({'sha256': self.record['sha256'],
                                           'pages': [{'text': page} for page in pages]}), encoding='utf-8')
        before = (self.ocrpath.read_bytes(), self.source.read_bytes())
        record, summary = self.build()
        self.assertEqual(record['pages'], 2)
        self.assertEqual(record['searchability'], 'OCR text: verify against original page')
        self.assertEqual(record['ocr_path'], 'ocr/pdf-example.json')
        self.assertEqual(self.textpath.read_text(encoding='utf-8'), '\n\f\n'.join(pages))
        self.assertEqual(summary['indexed_units'], 2)
        self.assertEqual(summary['extraction_errors'], [])
        self.assertEqual(library.search('12'), [])
        for query in ('34', '56'):
            self.assertEqual([r['doc_id'] for r in library.search(query)], ['pdf-example'])
        self.assertEqual((self.ocrpath.read_bytes(), self.source.read_bytes()), before)


if __name__ == '__main__':
    unittest.main()
