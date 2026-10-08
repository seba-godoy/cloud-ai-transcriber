import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from config import Config
from gemini_transcriber import (assert_ascii_upload_basename, gemini_working_basename,
                                transcribe_audio, transcribe_chunk)
from utils import make_source_key


class UnicodeWorkingPathTests(unittest.TestCase):
    NAMES = ("Reunión.m4a", "München.m4a", "中文课程.m4a", "clase_🎓.m4a", "cafe\u0301.m4a")

    def test_unicode_display_identity_is_separate_from_deterministic_ascii_paths(self):
        for index, name in enumerate(self.NAMES):
            with self.subTest(name=name):
                file_id = f"drive-id-{index}"
                normal = gemini_working_basename("drive", file_id, 0)
                enhanced = gemini_working_basename("drive", file_id, 0, enhanced=True)
                self.assertEqual(name, self.NAMES[index])
                self.assertEqual(make_source_key("drive", file_id),
                                 make_source_key("drive", file_id))
                self.assertTrue(normal.isascii()); self.assertTrue(enhanced.isascii())
                self.assertEqual(normal, gemini_working_basename("drive", file_id, 0))
                self.assertNotEqual(normal, gemini_working_basename("drive", file_id, 1))
                self.assertTrue(normal.endswith(".wav")); self.assertTrue(enhanced.endswith(".wav"))
        self.assertEqual(len({gemini_working_basename("drive", f"drive-id-{i}", 0)
                              for i in range(len(self.NAMES))}), len(self.NAMES))

    def test_upload_boundary_rejects_unicode_and_allows_ascii(self):
        assert_ascii_upload_basename("/tmp/gemini_deadbeef_chunk_0000.wav")
        with self.assertRaisesRegex(ValueError, "ASCII"):
            assert_ascii_upload_basename("/tmp/Reunión.wav")

    def test_every_uploaded_path_is_ascii_and_cleanup_survives_success(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source_name = self.NAMES[3]
            source = os.path.join(temp_dir, source_name)
            open(source, "wb").close()
            uploaded = []
            chunk = MagicMock(); chunk.__len__.return_value = 1000
            def extract(_src, dst, _start, _duration):
                open(dst, "wb").close()
            def transcribe(_client, path, *_args):
                uploaded.append(path)
                self.assertTrue(os.path.basename(path).isascii())
                return "hola", "model"
            with patch.object(Config, "TEMP_DIR", temp_dir), \
                 patch("gemini_transcriber.probe_audio_duration", return_value=1), \
                 patch("gemini_transcriber.extract_audio_chunk", side_effect=extract), \
                 patch("gemini_transcriber.AudioSegment.from_file", return_value=chunk), \
                 patch("gemini_transcriber.detect_silence", return_value=[]), \
                 patch("gemini_transcriber.subprocess.run"), \
                 patch("gemini_transcriber.transcribe_chunk", side_effect=transcribe), \
                 patch("gemini_transcriber.genai.Client"):
                transcribe_audio(source, file_id="unicode-drive-id", file_name=source_name)
            self.assertEqual(os.path.basename(source), source_name)
            self.assertEqual(len(uploaded), 1)
            self.assertFalse(os.path.exists(uploaded[0]))

    def test_failure_before_upload_does_not_call_api(self):
        client = MagicMock()
        with self.assertRaises(ValueError):
            transcribe_chunk(client, "/tmp/clase_🎓.wav", "m1", "m2", "prompt", 1000)
        client.files.upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
