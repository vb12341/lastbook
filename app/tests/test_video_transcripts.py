"""Caption payloads reach the video reader, Chat retrieval and saved course pages intact, and video search finds
videos when a broad word is on more manual pages than the search keeps."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import assistant
import retrieval
import server
from engine import saved_pages


class VideoTranscripts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'captions.vtt'

    def texts(self, data):
        self.path.write_bytes(data)
        with patch.object(server, 'LIB', self.root):
            reader = ' '.join(cue['text'] for cue in server.vtt_cues('captions.vtt'))
        return {
            'reader': reader,
            'retrieval': ' '.join(text for _, text in retrieval.vtt_windows(self.path)),
            'saved page': saved_pages.transcript_text(data),
        }

    def test_numeric_payload_is_not_a_cue_identifier(self):
        data = (b'WEBVTT\n\n17\n00:01.000 --> 00:05.000\n'
                b'The label on this storage container reads\n12\n'
                b'litres, and the number belongs with the words.\n')
        expected = ('The label on this storage container reads 12 '
                    'litres, and the number belongs with the words.')
        for route, text in self.texts(data).items():
            with self.subTest(route=route):
                self.assertEqual(text, expected)

    def test_named_identifiers_do_not_become_spoken_text(self):
        first = 'Keep this first instruction with its own timestamp and wording.'
        second = 'Keep this next instruction with its own timestamp and wording.'
        data = ('WEBVTT\n\nchapter-one\n00:01.000 --> 00:05.000\n' + first +
                '\n\nchapter-two\n00:20.000 --> 00:24.000\n' + second).encode()
        for route, text in self.texts(data).items():
            with self.subTest(route=route):
                self.assertEqual(text, first + ' ' + second)

    def test_comments_and_settings_are_not_spoken_text(self):
        spoken = 'Only this sentence was spoken and should be used as a video source.'
        data = ('WEBVTT\nKind: captions\nLanguage: en\n\n'
                'STYLE\n::cue { color: lime; }\n\n'
                'REGION\nid:region-one\nwidth:40%\n\n'
                'NOTE editor notes\nDo not index this comment.\n'
                '00:00.000 --> 00:01.000\nThis is still inside the comment.\n\n'
                '00:02.000 --> 00:08.000 align:start position:10%\n' + spoken +
                '\n\nNOTE\nNor this trailing comment.\n').encode()
        for route, text in self.texts(data).items():
            with self.subTest(route=route):
                self.assertEqual(text, spoken)

    def test_header_words_inside_a_payload_are_kept(self):
        spoken = ('NOTE this sentence is spoken, not an editor comment. '
                  'Language: these words are also part of the caption.')
        data = ('WEBVTT\n\n00:01.000 --> 00:05.000\n'
                'NOTE this sentence is spoken, not an editor comment.\n'
                'Language: these words are also part of the caption.\n').encode()
        for route, text in self.texts(data).items():
            with self.subTest(route=route):
                self.assertEqual(text, spoken)

    def test_webvtt_identifier_after_header_keeps_its_payload(self):
        first = 'This first caption describes the label on the example container.'
        second = 'This later caption belongs to the cue whose identifier starts with WEBVTT.'
        data = ('WEBVTT\n\n00:01.000 --> 00:05.000\n' + first +
                '\n\nWEBVTT\n00:20.000 --> 00:25.000\n' + second + '\n').encode()
        for route, text in self.texts(data).items():
            with self.subTest(route=route):
                self.assertEqual(text, first + ' ' + second)
        with patch.object(server, 'LIB', self.root):
            self.assertEqual(server.vtt_cues('captions.vtt'),
                             [{'t': 1, 'text': first}, {'t': 20, 'text': second}])

    def test_whitespace_payload_lines_keep_words_and_rolling_cue_start(self):
        first = 'This first caption describes the label on the example container.'
        second = 'The next words explain how this label belongs with the rest of the example.'
        for whitespace in (' ', '\t', ' \t '):
            fixtures = {
                'inside cue': '00:20.000 --> 00:25.000\n' + first + '\n' + whitespace + '\n' + second + '\n',
                'rolling cue': ('00:02.000 --> 00:04.000 align:start position:0%\n' + whitespace + '\n' + first +
                                '\n\n00:04.000 --> 00:06.000 align:start position:0%\n' + first + '\n' + second + '\n'),
            }
            for fixture, payload in fixtures.items():
                data = ('WEBVTT\n\n' + payload).encode()
                for route, text in self.texts(data).items():
                    with self.subTest(whitespace=repr(whitespace), fixture=fixture, route=route):
                        # Consumers format whitespace differently; all spoken words must survive in order.
                        self.assertEqual(text.split(), (first + ' ' + second).split())
                expected_start = 2 if fixture == 'rolling cue' else 20
                with patch.object(server, 'LIB', self.root):
                    starts = {'reader': server.vtt_cues('captions.vtt')[0]['t'],
                              'retrieval': retrieval.vtt_windows(self.path)[0][0]}
                for route, start in starts.items():
                    with self.subTest(whitespace=repr(whitespace), fixture=fixture, route=route + ' timestamp'):
                        self.assertEqual(start, expected_start)

    def test_line_endings_bom_and_final_cue_without_blank_line(self):
        spoken = 'This final caption is complete even without a trailing blank line.'
        for newline in ('\n', '\r\n', '\r'):
            data = ('\ufeffWEBVTT\n\ncue-one\n01:02:03.000 --> 01:02:06.000\n' +
                    spoken).replace('\n', newline).encode()
            for route, text in self.texts(data).items():
                with self.subTest(newline=repr(newline), route=route):
                    self.assertEqual(text, spoken)
            with patch.object(server, 'LIB', self.root):
                self.assertEqual(server.vtt_cues('captions.vtt')[0]['t'], 3723)
            self.assertEqual(retrieval.vtt_windows(self.path)[0][0], 3723)

    def test_comma_timestamps_remain_readable(self):
        spoken = 'This caption uses the comma timestamps already accepted by Lastbook.'
        data = ('1\n00:01:02,000 --> 00:01:06,000\n' + spoken).encode()
        for route, text in self.texts(data).items():
            with self.subTest(route=route):
                self.assertEqual(text, spoken)
        with patch.object(server, 'LIB', self.root):
            self.assertEqual(server.vtt_cues('captions.vtt')[0]['t'], 62)

    def test_cue_markup_is_removed(self):
        spoken = 'This caption has spoken words inside tags and keeps the rest of the sentence.'
        data = (b'WEBVTT\n\n00:01.000 --> 00:05.000\n'
                b'<v Speaker>This caption has <b>spoken words</b> inside tags '
                b'and keeps the rest of the sentence.</v>\n')
        for route, text in self.texts(data).items():
            with self.subTest(route=route):
                self.assertEqual(text, spoken)

    def test_duplicate_cues_keep_existing_transcript_behavior(self):
        spoken = 'This complete caption appears twice in the original subtitle track.'
        data = ('WEBVTT\n\n00:01.000 --> 00:03.000\n' + spoken +
                '\n\n00:03.000 --> 00:05.000\n' + spoken + '\n').encode()
        for route, text in self.texts(data).items():
            with self.subTest(route=route):
                self.assertEqual(text, spoken)

    def test_short_cue_is_not_cut_from_the_next_cues_first_word(self):
        # "So" then "Some people…" is two cues, not a rolling repeat: only a repeat that ends at a word is dropped.
        for short, following in (('So', 'Some people boil the water before they pour it through the filter.'),
                                 ('the', 'then you tighten the bandage above the wound and note the time')):
            data = ('WEBVTT\n\n00:01.000 --> 00:02.000\n' + short +
                    '\n\n00:02.000 --> 00:06.000\n' + following + '\n').encode()
            for route, text in self.texts(data).items():
                with self.subTest(short=short, route=route):
                    self.assertEqual(text, short + ' ' + following)

    def test_payload_line_is_not_dropped_as_another_words_suffix(self):
        for first, second in (('The first label says 112', '12 is on the second label.'),
                              ('The first label says something', 'thing is on the second label.')):
            # Put the short suffix on its own line, where the reader used to mistake it for a repeat.
            short, rest = second.split(' ', 1)
            data = ('WEBVTT\n\n00:20.000 --> 00:25.000\n' + first + '\n' + short + '\n' + rest + '\n').encode()
            expected = first + ' ' + second
            for route, text in self.texts(data).items():
                with self.subTest(first=first, route=route):
                    self.assertEqual(text, expected)
            with self.subTest(first=first, route='reader timestamp'), patch.object(server, 'LIB', self.root):
                self.assertEqual(server.vtt_cues('captions.vtt'), [{'t': 20, 'text': expected}])
            with self.subTest(first=first, route='retrieval timestamp'):
                self.assertEqual(retrieval.vtt_windows(self.path), [(20, expected)])

    def test_repeated_payload_lines_still_merge_at_word_boundaries(self):
        data = (b'WEBVTT\n\n00:20.000 --> 00:25.000\nThe label reads\n'
                b'12 litres.\n12 litres.\nThe next label differs.\n')
        expected = 'The label reads 12 litres. The next label differs.'
        for route, text in self.texts(data).items():
            with self.subTest(route=route):
                self.assertEqual(text, expected)
        with patch.object(server, 'LIB', self.root):
            self.assertEqual(server.vtt_cues('captions.vtt'), [{'t': 20, 'text': expected}])

        for boundary, decoded in ((' ', ' '), ('&nbsp;', '\u00a0'), ('\t', '\t')):
            first = 'The label clearly says' + boundary + '14 litres.'
            data = ('WEBVTT\n\n00:20.000 --> 00:25.000\n' + first +
                    '\n14 litres.\nThe next label differs.\n').encode()
            expected = 'The label clearly says' + decoded + '14 litres. The next label differs.'
            for route, text in self.texts(data).items():
                if route != 'saved page':  # Saved pages only merge identical whole lines.
                    with self.subTest(boundary=boundary, route=route):
                        self.assertEqual(text.split(), expected.split())
            with self.subTest(boundary=boundary, route='reader timestamp'), patch.object(server, 'LIB', self.root):
                self.assertEqual(server.vtt_cues('captions.vtt'), [{'t': 20, 'text': expected}])
            with self.subTest(boundary=boundary, route='retrieval timestamp'):
                self.assertEqual(retrieval.vtt_windows(self.path), [(20, ' '.join(expected.split()))])

    def test_cached_video_windows_are_refreshed_with_the_unchanged_track(self):
        expected = 'The first label says 112 12 is on the second label.'
        self.path.write_text('WEBVTT\n\n00:20.000 --> 00:25.000\n'
                             'The first label says 112\n12\nis on the second label.\n', encoding='utf-8')
        before = self.path.read_bytes(), self.path.stat().st_mtime_ns
        video = {'id': 'video-test', 'kind': 'video', 'path': 'videos/test.mp4', 'captions': ['captions.vtt']}
        cat = {'by_id': {video['id']: video}, 'video_tx': {}}

        class CaptionEmbedder:
            def embed(self, texts):
                return retrieval.np.zeros((len(texts), retrieval.DIM), dtype='float32')

        with patch.object(retrieval, 'LIB', self.root), patch.object(retrieval, 'IDX', self.root / 'index'), \
                patch.object(retrieval, 'AVAILABLE', True), patch.object(retrieval, 'Embedder', CaptionEmbedder):
            index = retrieval.Index(lambda: cat, lambda record: ('', 1), self.root / 'search.sqlite')
            try:
                # Seed the signature and text an older reader kept, using no real model.
                with patch.object(index, 'sources', return_value=[(video['id'], 'vtt:captions.vtt', -1,
                                                                  ('vtt', 'captions.vtt'))]):
                    self.assertEqual(index.update(), 1)
                db = index._db()
                try:
                    db.execute('UPDATE chunk SET text=?', ('The first label says 112 is on the second label.',))
                finally:
                    db.close()

                self.assertEqual(index.update(), 1)
                db = index._db()
                try:
                    self.assertEqual(db.execute('SELECT t,text FROM chunk WHERE active=1').fetchall(), [(20, expected)])
                    self.assertEqual(db.execute('SELECT count(*) FROM chunk WHERE active=0').fetchone(), (1,))
                finally:
                    db.close()
                self.assertEqual(index.update(), 0)
                self.assertEqual((self.path.read_bytes(), self.path.stat().st_mtime_ns), before)
            finally:
                reader = getattr(index._tls, 'ro', None)
                if reader is not None:
                    reader.close()

    def test_empty_or_metadata_only_track_has_no_transcript(self):
        for data in (b'', b'WEBVTT\n\n', b'WEBVTT\n\nNOTE\nEditor notes only.\n'):
            for route, text in self.texts(data).items():
                with self.subTest(data=data, route=route):
                    self.assertEqual(text, '')

    def test_video_and_chat_entry_points_use_the_caption_payload(self):
        text = 'The storage container is marked 12 litres and this is its caption.'
        data = ('WEBVTT\n\ncaption-id\n00:20.000 --> 00:25.000\n'
                'The storage container is marked\n12\n'
                'litres and this is its caption.\n').encode()
        self.path.write_bytes(data)
        cat = {'by_id': {'video-test': {'path': 'videos/test.mp4', 'captions': ['captions.vtt']}},
               'video_tx': {}}
        with patch.object(server, 'LIB', self.root), patch.object(server, 'catalog', return_value=cat):
            self.assertEqual(server.transcript('video-test')['cues'], [{'t': 20, 'text': text}])
        with patch.object(retrieval, 'LIB', self.root):
            index = retrieval.Index.__new__(retrieval.Index)  # _texts needs neither an embedder nor an index database
            self.assertEqual(index._texts('video-test', ('vtt', 'captions.vtt')), [(None, 20, text)])

    def test_saved_course_transcript_keeps_numbers(self):
        page = b'<html><body><video><track src="captions.vtt"></video></body></html>'
        vtt = b'WEBVTT\n\n17\n00:01.000 --> 00:05.000\nThe label reads\n12\nlitres.\n'
        result = saved_pages.rewrite(page, lambda src: vtt if src == 'captions.vtt' else None)
        self.assertIn(b'<p>The label reads 12 litres.</p>', result)

    def test_trailing_whitespace_does_not_repeat_saved_course_caption(self):
        page = b'<html><body><video><track src="captions.vtt"></video></body></html>'
        expected = 'hello everyone today we compare water filters'
        for whitespace in (' ', '\t', ' \t ', '\u00a0'):
            with self.subTest(whitespace=repr(whitespace)):
                vtt = ('WEBVTT\n\n00:02.000 --> 00:04.000\nhello everyone\n' + whitespace +
                       '\n\n00:04.000 --> 00:06.000\nhello everyone\ntoday we compare water filters\n').encode()
                self.assertEqual(saved_pages.transcript_text(vtt), expected)
                result = saved_pages.rewrite(page, lambda src: vtt if src == 'captions.vtt' else None)
                self.assertIn(('<p>' + expected + '</p>').encode(), result)

    def test_empty_cue_does_not_consume_the_next_identifier(self):
        spoken = 'The next nonempty caption still belongs to its own timestamp.'
        data = ('WEBVTT\n\n00:00.000 --> 00:01.000\n\n'
                'next-cue\n00:20.000 --> 00:25.000\n' + spoken).encode()
        for route, text in self.texts(data).items():
            with self.subTest(route=route):
                self.assertEqual(text, spoken)
        with patch.object(server, 'LIB', self.root):
            self.assertEqual(server.vtt_cues('captions.vtt'), [{'t': 20, 'text': spoken}])


class VideoSearch(unittest.TestCase):
    """A broad word must still find videos when it is on more manual pages than the search keeps (600 in
    server.find, 120 in assistant.hybrid): Videos, the Videos row under Everything and the AI's video search."""
    MANUAL_PAGES = 700

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        recs = [{'id': f'manual-{i}', 'kind': 'pdf', 'path': f'documents/manual-{i}.pdf', 'title': f'Manual {i}'}
                for i in range(self.MANUAL_PAGES)]
        recs += [{'id': 'video-pump', 'kind': 'video', 'path': 'videos/pump.mp4', 'title': 'Hand pump repair'},
                 {'id': 'tx-pump', 'kind': 'transcript', 'path': 'transcripts/pump.txt', 'video_path': 'videos/pump.mp4',
                  'title': 'Hand pump repair transcript'},
                 {'id': 'tx-alone', 'kind': 'transcript', 'path': 'transcripts/alone.txt', 'title': 'Rain barrel talk'}]
        for r in recs:
            r.update(collection='Test', category='test', publisher='Test')
        self.cat = {'by_id': {r['id']: r for r in recs}, 'by_path': {r['path']: r for r in recs},
                    'video_tx': {'videos/pump.mp4': recs[-2]}, 'thumbs': {}}
        db = self.root / 'search.sqlite'
        con = sqlite3.connect(db)
        con.execute('CREATE VIRTUAL TABLE pages USING fts5(doc_id UNINDEXED,title,collection,category,path UNINDEXED,page UNINDEXED,body,tokenize="unicode61")')
        con.executemany('INSERT INTO pages VALUES (?,?,?,?,?,?,?)',
                        [(f'manual-{i}', f'Manual {i}', 'Test', 'test', f'documents/manual-{i}.pdf', 1,
                          'water water clean water storage') for i in range(self.MANUAL_PAGES)])
        con.execute('INSERT INTO pages VALUES (?,?,?,?,?,?,?)', ('tx-pump', 'Hand pump repair transcript', 'Test', 'test',
                    'transcripts/pump.txt', None, 'today we fix a hand pump that lifts water from a well'))
        con.execute('INSERT INTO pages VALUES (?,?,?,?,?,?,?)', ('tx-alone', 'Rain barrel talk', 'Test', 'test',
                    'transcripts/alone.txt', None, 'a rain barrel keeps water for the garden'))
        con.commit()
        con.close()
        for p in (patch.object(server, 'LIB', self.root), patch.object(server.library, 'DB', db),
                  patch.object(server, 'catalog', return_value=self.cat), patch.object(assistant, 'S', server),
                  patch.object(assistant, 'INDEX', None)):
            p.start()
            self.addCleanup(p.stop)
        server.find.cache_clear()
        self.addCleanup(server.find.cache_clear)

    def test_videos_tab_finds_a_video_past_the_cap(self):
        self.assertIn('video-pump', [r['id'] for r in server.find('water', 'video')['results']])

    def test_chat_video_search_finds_a_video_past_the_cap(self):
        self.assertIn('video-pump', [h['doc_id'] for h in assistant.hybrid('water', 6, 'video')])

    def test_other_kinds_keep_the_plain_query(self):
        # Manual pages are most of the index and are never crowded out, so these searches keep the cheaper query.
        with patch.object(server, 'search_ids', side_effect=AssertionError('filtered in the query'), create=True):
            for kind in ('read', 'all'):
                with self.subTest(find=kind):
                    self.assertTrue(server.find('water', kind)['results'])
            for kind in ('manual', 'any'):
                with self.subTest(hybrid=kind):
                    self.assertTrue(assistant.hybrid('water', 6, kind))

    def test_a_lone_dash_or_underscore_is_not_a_word(self):
        # "_" or "-" alone became an empty all-words term, so the search fell back to any word.
        self.assertEqual(server.query_groups('fever - child _ cough')[0], ['fever', 'child', 'cough'])
        self.assertEqual(server.query_groups('first-aid kit')[0], ['first-aid', 'kit'])
        plain = server.find('water storage', 'read')
        for q in ('water _ storage', 'water - storage'):
            with self.subTest(q=q):
                r = server.find(q, 'read')
                self.assertEqual(r['mode'], 'all words')          # was 'some words': no page has the empty term
                self.assertEqual(r['results'], plain['results'])

    def test_transcript_without_a_video(self):
        # Today Videos lists a transcript that has no video of its own, and the AI's search leaves it out.
        self.assertIn('tx-alone', [r['id'] for r in server.find('rain barrel', 'video')['results']])
        self.assertNotIn('tx-alone', [h['doc_id'] for h in assistant.hybrid('rain barrel', 6, 'video')])


if __name__ == '__main__':
    unittest.main()
