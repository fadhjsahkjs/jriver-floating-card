"""Protocol and lyric safeguards; these tests never send playback/rating writes."""
import unittest
from unittest.mock import Mock, patch
import tempfile
from pathlib import Path

import floating_bridge as f


class LyricTests(unittest.TestCase):
    def test_multistamp_bilingual_offset_and_empty_break(self):
        lines = f.parse_lrc('[offset:-250]\n[00:01.5][00:04.00]hello\n[00:01.50]译文\n[00:03.123]\n[ar:artist]')
        self.assertEqual(lines, [(1250, 'hello\n译文'), (2873, ''), (3750, 'hello')])
        self.assertEqual(f.lyric_index(lines, 1249), -1)
        self.assertEqual(f.lyric_index(lines, 1250), 0)
        self.assertEqual(f.lyric_index(lines, 2873), 1)

    def test_manual_lyrics_bound_to_exact_recording_not_title(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(f, 'DATA', Path(temp)):
            source = Path(temp) / 'input.lrc'
            source.write_text('[00:02]line', encoding='utf-8')
            f.import_lyrics('C:/album/live.flac', source)
            self.assertEqual(f.lyrics_for_track({'Filename': 'C:/album/live.flac'})['lines'], [(2000, 'line')])
            self.assertEqual(f.lyrics_for_track({'Filename': 'C:/album/studio.flac'})['lines'], [])

    def test_embedded_plain_text_not_fabricated_as_synced(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(f, 'DATA', Path(temp)):
            result = f.lyrics_for_track({'Lyrics': 'plain words'})
            self.assertEqual(result['lines'], [])
            self.assertEqual(result['plain'], 'plain words')


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.b = f.Bridge()
        self.b.items = Mock(return_value={})
        self.b.status = Mock(return_value={'FileKey': '17', 'ZoneID': '0', 'DurationMS': '8000'})

    def test_seek_is_ms_bounded_and_bound_to_recording_and_zone(self):
        self.b.command('seek', key='17', value=9000, zone='0')
        self.b.items.assert_called_once_with('Playback/Position', Position=8000, Mode='ms', Zone='0')
        self.b.items.reset_mock()
        for key, zone in [('18', '0'), ('17', '1')]:
            with self.assertRaises(RuntimeError):
                self.b.command('seek', key=key, value=1000, zone=zone)
        self.b.items.assert_not_called()

    def test_controls_never_rebuild_queue_or_modify_audio_configuration(self):
        for command in ['Previous', 'Next', 'Play']:
            self.b.command(command, zone='0')
            self.b.items.assert_called_with('Playback/' + command, Zone='0')
        self.b.command('Pause', zone='0')
        self.b.items.assert_called_with('Playback/Pause', State=-1, Zone='0')
        with patch.object(self.b, 'set_rating', return_value={'rating':4}) as setter:
            self.b.command('rating', key='17', value=4)
            setter.assert_called_once_with('17', 4)
        with self.assertRaises(ValueError):
            self.b.command('File/Delete', key='17')

    def test_undo_does_not_overwrite_rating_changed_elsewhere(self):
        import json
        self.b.get = Mock(return_value=json.dumps([{'Key':17,'Filename':'C:/a.flac','Rating':5}]).encode())
        with patch.object(f.subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):
                self.b.set_rating('17', 2, expected=4)
        run.assert_not_called()

    def test_rating_requires_http_readback_and_uses_hidden_com_process(self):
        import json
        self.b.get = Mock(side_effect=[json.dumps([{'Key':17,'Filename':'C:/a.flac','Name':'A','Rating':3}]).encode(),
                                      json.dumps([{'Key':17,'Rating':4}]).encode()])
        with patch.object(f.subprocess, 'run', return_value=Mock(returncode=0,stdout='{"key":17,"before":3,"rating":4}')) as run:
            result = self.b.set_rating('17',4)
        self.assertEqual(result['rating'],4)
        self.assertIn('-ExpectedFilename',run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['creationflags'],f.subprocess.CREATE_NO_WINDOW)


if __name__ == '__main__':
    unittest.main()
