import os
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from gemini_transcriber import transcribe_audio
from config import Config

class TestGeminiProgressAtomic(unittest.TestCase):
    def setUp(self):
        self.orig_token = Config.TELEGRAM_BOT_TOKEN
        self.orig_chat = Config.TELEGRAM_CHAT_ID
        Config.TELEGRAM_BOT_TOKEN = "test-token"
        Config.TELEGRAM_CHAT_ID = "123"

    def tearDown(self):
        Config.TELEGRAM_BOT_TOKEN = self.orig_token
        Config.TELEGRAM_CHAT_ID = self.orig_chat

    @patch("gemini_transcriber.transcribe_chunk", return_value=("Texto transcrito", "gemini-3.5-flash-lite"))
    @patch("gemini_transcriber.genai.Client")
    @patch("gemini_transcriber.AudioSegment.from_file")
    def test_valid_progress_recovery_resumes_from_saved_state(
        self, mock_audio_from_file, mock_genai_client, mock_transcribe_chunk
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = os.path.join(tmpdir, "test_audio.mp3")
            with open(audio_path, "wb") as f:
                f.write(b"fake audio data")

            # Mock 120s audio segment (2 chunks of 60s)
            mock_audio = MagicMock()
            mock_audio.__len__.return_value = 120000
            mock_audio.__getitem__.return_value = mock_audio
            mock_audio_from_file.return_value = mock_audio

            # Pre-create valid progress file indicating chunk 0 (idx 0) is already done
            progress_path = os.path.join(tmpdir, "test_audio_progress.json")
            valid_progress = {
                "last_idx": 0,
                "final_output": ["[00:00-01:00]", "Chunk 1 texto", ""],
                "used_models": ["gemini-3.5-flash-lite"],
                "total_words": 3,
                "prev_transcript": "Chunk 1 texto"
            }
            with open(progress_path, "w", encoding="utf-8") as f:
                json.dump(valid_progress, f)

            with patch("config.Config.TEMP_DIR", tmpdir), \
                 patch("gemini_transcriber.probe_audio_duration", return_value=120), \
                 patch("gemini_transcriber.extract_audio_chunk", side_effect=lambda s, d, a, b: open(d, "wb").write(b"audio")), \
                 patch("gemini_transcriber.detect_silence", return_value=[]):
                transcript, model = transcribe_audio(audio_path)

            # Assert transcribe_chunk was called ONLY ONCE for chunk index 1
            self.assertEqual(mock_transcribe_chunk.call_count, 1)

    @patch("gemini_transcriber.transcribe_chunk", return_value=("Chunk transcrito", "gemini-3.5-flash-lite"))
    @patch("gemini_transcriber.genai.Client")
    @patch("gemini_transcriber.AudioSegment.from_file")
    def test_corrupt_truncated_progress_file_renamed_and_starts_fresh(
        self, mock_audio_from_file, mock_genai_client, mock_transcribe_chunk
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = os.path.join(tmpdir, "test_corrupt.mp3")
            with open(audio_path, "wb") as f:
                f.write(b"fake audio data")

            mock_audio = MagicMock()
            mock_audio.__len__.return_value = 60000
            mock_audio.__getitem__.return_value = mock_audio
            mock_audio_from_file.return_value = mock_audio

            progress_path = os.path.join(tmpdir, "test_corrupt_progress.json")
            with open(progress_path, "w", encoding="utf-8") as f:
                f.write("{ INVALID TRUNCATED JSON ...")

            with patch("config.Config.TEMP_DIR", tmpdir), \
                 patch("gemini_transcriber.probe_audio_duration", return_value=60), \
                 patch("gemini_transcriber.extract_audio_chunk", side_effect=lambda s, d, a, b: open(d, "wb").write(b"audio")), \
                 patch("gemini_transcriber.detect_silence", return_value=[]):
                transcript, model = transcribe_audio(audio_path)

            # Assert corrupt progress file was renamed to *.corrupt_*.json
            corrupt_files = [f for f in os.listdir(tmpdir) if f.startswith("test_corrupt_progress.corrupt_") and f.endswith(".json")]
            self.assertEqual(len(corrupt_files), 1)

            # Assert transcription started fresh (chunk index 0 processed)
            self.assertEqual(mock_transcribe_chunk.call_count, 1)

    @patch("gemini_transcriber.transcribe_chunk", return_value=("Texto OK", "gemini-3.5-flash-lite"))
    @patch("gemini_transcriber.genai.Client")
    @patch("gemini_transcriber.AudioSegment.from_file")
    def test_atomic_progress_save_uses_temp_file_and_replace(
        self, mock_audio_from_file, mock_genai_client, mock_transcribe_chunk
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = os.path.join(tmpdir, "test_atomic.mp3")
            with open(audio_path, "wb") as f:
                f.write(b"fake audio data")

            mock_audio = MagicMock()
            mock_audio.__len__.return_value = 60000
            mock_audio.__getitem__.return_value = mock_audio
            mock_audio_from_file.return_value = mock_audio

            with patch("config.Config.TEMP_DIR", tmpdir), \
                 patch("gemini_transcriber.probe_audio_duration", return_value=60), \
                 patch("gemini_transcriber.extract_audio_chunk", side_effect=lambda s, d, a, b: open(d, "wb").write(b"audio")), \
                 patch("gemini_transcriber.detect_silence", return_value=[]), \
                 patch("os.replace", wraps=os.replace) as mock_os_replace:
                transcribe_audio(audio_path)

            # Assert os.replace was called to atomically finalize progress file
            self.assertTrue(mock_os_replace.called)
            replaced_targets = [call[0][1] for call in mock_os_replace.call_args_list]
            progress_target = os.path.join(tmpdir, "test_atomic_progress.json")
            self.assertIn(progress_target, replaced_targets)

if __name__ == "__main__":
    unittest.main()
