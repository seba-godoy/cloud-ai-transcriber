import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from config import Config
from summary_generator import generate_summary
from gemini_transcriber import GenerationResult, transcribe_audio, transcribe_chunk

class TestModelMigration(unittest.TestCase):
    def test_config_model_names(self):
        self.assertEqual(Config.GEMINI_MODEL, "gemini-3.5-flash-lite")
        self.assertEqual(Config.GEMINI_FALLBACK_MODEL, "gemini-3.8-flash")

    @patch("gemini_transcriber.genai.Client")
    @patch("gemini_transcriber.AudioSegment.from_file")
    @patch("gemini_transcriber.detect_silence")
    @patch("gemini_transcriber.transcribe_chunk")
    def test_normal_transcription_uses_primary_model(
        self, mock_transcribe_chunk, mock_detect_silence, mock_audio, mock_client
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            dummy_path = os.path.join(tmpdir, "dummy.mp3")
            with open(dummy_path, "wb") as f:
                f.write(b"dummy")

            mock_chunk = MagicMock()
            mock_chunk.__len__.return_value = 30000
            mock_seg = MagicMock()
            mock_seg.__len__.return_value = 30000
            mock_seg.__getitem__.return_value = mock_chunk
            mock_audio.return_value = mock_seg
            mock_detect_silence.return_value = []
            mock_transcribe_chunk.return_value = ("Transcripción", Config.GEMINI_MODEL)

            with patch("config.Config.TEMP_DIR", tmpdir), \
                 patch("gemini_transcriber.probe_audio_duration", return_value=30), \
                 patch("gemini_transcriber.subprocess.run"), \
                 patch("time.sleep"):
                text, model = transcribe_audio(dummy_path, use_pro_model=False, file_id="f1", file_name="dummy.mp3")

            mock_transcribe_chunk.assert_called()
            default_model_arg = mock_transcribe_chunk.call_args[0][2]
            fallback_model_arg = mock_transcribe_chunk.call_args[0][3]
            self.assertEqual(default_model_arg, "gemini-3.5-flash-lite")
            self.assertEqual(fallback_model_arg, "gemini-3.8-flash")

    @patch("gemini_transcriber.genai.Client")
    @patch("gemini_transcriber.AudioSegment.from_file")
    @patch("gemini_transcriber.detect_silence")
    @patch("gemini_transcriber.transcribe_chunk")
    def test_short_telegram_audio_reverses_primary_and_fallback(
        self, mock_transcribe_chunk, mock_detect_silence, mock_audio, mock_client
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            dummy_path = os.path.join(tmpdir, "dummy.mp3")
            with open(dummy_path, "wb") as f:
                f.write(b"dummy")

            mock_chunk = MagicMock()
            mock_chunk.__len__.return_value = 30000
            mock_seg = MagicMock()
            mock_seg.__len__.return_value = 30000
            mock_seg.__getitem__.return_value = mock_chunk
            mock_audio.return_value = mock_seg
            mock_detect_silence.return_value = []
            mock_transcribe_chunk.return_value = ("Transcripción", Config.GEMINI_FALLBACK_MODEL)

            with patch("config.Config.TEMP_DIR", tmpdir), \
                 patch("gemini_transcriber.probe_audio_duration", return_value=30), \
                 patch("gemini_transcriber.subprocess.run"), \
                 patch("time.sleep"):
                text, model = transcribe_audio(dummy_path, use_pro_model=True, file_id="f1", file_name="dummy.mp3")

            mock_transcribe_chunk.assert_called()
            default_model_arg = mock_transcribe_chunk.call_args[0][2]
            fallback_model_arg = mock_transcribe_chunk.call_args[0][3]
            self.assertEqual(default_model_arg, "gemini-3.8-flash")
            self.assertEqual(fallback_model_arg, "gemini-3.5-flash-lite")

    @patch("gemini_transcriber._generate")
    def test_transcribe_chunk_fallback_uses_gemini_3_8_flash(self, mock_generate):
        mock_client = MagicMock()
        mock_file = MagicMock()
        mock_file.name = "files/123"
        mock_file.state = "ACTIVE"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        mock_generate.side_effect = [
            Exception("Default model failed"),
            GenerationResult("Transcripción fallback", "STOP", None, 1),
        ]

        text, model = transcribe_chunk(
            mock_client, "chunk.mp3", 
            default_model="gemini-3.5-flash-lite", 
            fallback_model="gemini-3.8-flash", 
            prompt="Prompt", 
            chunk_duration_ms=60000
        )

        self.assertEqual(text, "Transcripción fallback")
        self.assertEqual(model, "gemini-3.8-flash")
        self.assertEqual(mock_generate.call_count, 2)
        self.assertEqual(mock_generate.call_args_list[0][0][1], "gemini-3.5-flash-lite")
        self.assertEqual(mock_generate.call_args_list[1][0][1], "gemini-3.8-flash")

    @patch("summary_generator.genai.Client")
    def test_summary_generator_uses_fallback_model(self, mock_client_cls):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = "Resumen generado"
        mock_client.models.generate_content.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        res = generate_summary("Transcript de prueba", "Titulo")

        self.assertEqual(res, "Resumen generado")
        mock_client.models.generate_content.assert_called_once()
        kwargs = mock_client.models.generate_content.call_args[1]
        self.assertEqual(kwargs["model"], "gemini-3.8-flash")
        
        banned = ["temperature", "top_p", "top_k", "candidate_count", "thinking_budget", "thinking_level"]
        for param in banned:
            self.assertNotIn(param, kwargs)

    def test_no_banned_parameters_or_model_turns_in_codebase(self):
        banned = ["temperature", "top_p", "top_k", "candidate_count", "thinking_budget", 'role="model"', "role='model'"]
        prod_files = [
            "main.py", "config.py", "gemini_transcriber.py", 
            "summary_generator.py", "youtube_ingest.py", "utils.py"
        ]
        for fpath in prod_files:
            if not os.path.exists(fpath):
                continue
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            for b in banned:
                self.assertNotIn(b, content, f"Banned parameter/turn '{b}' found in {fpath}")

    def test_no_active_previous_fallback_models_in_codebase(self):
        prod_files = [
            "main.py", "config.py", "gemini_transcriber.py", 
            "summary_generator.py", "youtube_ingest.py", "utils.py",
            "drive_watcher.py", "drive_uploader.py", "docx_writer.py", "pdf_writer.py"
        ]
        for fpath in prod_files:
            if not os.path.exists(fpath):
                continue
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn("gemini-3.6-flash", content, f"Legacy model 'gemini-3.6-flash' found in {fpath}")
            self.assertNotIn("gemini-3.7-flash", content, f"Previous fallback model 'gemini-3.7-flash' found in {fpath}")

    def test_no_thinking_level_minimal_in_codebase(self):
        prod_files = [
            "main.py", "config.py", "gemini_transcriber.py", 
            "summary_generator.py", "youtube_ingest.py", "utils.py"
        ]
        for fpath in prod_files:
            if not os.path.exists(fpath):
                continue
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn("minimal", content, f"'minimal' thinking level found in {fpath}")

if __name__ == "__main__":
    unittest.main()
