import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from state_store import StateStore
from main import process_youtube_url
from utils import TelegramDeliveryError
from config import Config

class TestYouTubeSeparateState(unittest.TestCase):
    def setUp(self):
        self.orig_token = Config.TELEGRAM_BOT_TOKEN
        self.orig_chat = Config.TELEGRAM_CHAT_ID
        Config.TELEGRAM_BOT_TOKEN = "test-token"
        Config.TELEGRAM_CHAT_ID = "123"

    def tearDown(self):
        Config.TELEGRAM_BOT_TOKEN = self.orig_token
        Config.TELEGRAM_CHAT_ID = self.orig_chat

    @patch("main.send_long_telegram_message", return_value=True)
    @patch("main.send_telegram_message")
    @patch("main.download_youtube_audio")
    @patch("main.execute_pipeline")
    @patch("main.get_youtube_metadata")
    def test_resending_link_reuses_existing_transcript_without_download_or_retranscribe(
        self, mock_metadata, mock_exec_pipeline, mock_download_yt, mock_send_tg, mock_send_long_tg
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "state.json")
            store = StateStore(file_path=file_path)

            mock_metadata.return_value = ("Video Titulo", "vid_12345")
            store.save_youtube_transcript("vid_12345", "Video Titulo", "Este es el transcript previo guardado.")

            with patch("summary_generator.generate_summary", return_value="Resumen generado con éxito") as mock_gen_sum:
                process_youtube_url("https://youtube.com/watch?v=vid_12345", store)

            mock_download_yt.assert_not_called()
            mock_exec_pipeline.assert_not_called()
            mock_gen_sum.assert_called_once()
            mock_send_long_tg.assert_called_once_with("Resumen generado con éxito", parse_mode=None, required=True)

            yt_state = store.get_youtube_state("vid_12345")
            self.assertEqual(yt_state.get("summary_status"), "completed")
            self.assertTrue(store.is_processed("vid_12345"))

    @patch("main.send_long_telegram_message")
    @patch("main.send_telegram_message")
    @patch("main.download_youtube_audio")
    @patch("main.execute_pipeline")
    @patch("main.get_youtube_metadata")
    def test_resending_link_when_summary_already_exists_attempts_delivery_only(
        self, mock_metadata, mock_exec_pipeline, mock_download_yt, mock_send_tg, mock_send_long_tg
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "state.json")
            store = StateStore(file_path=file_path)

            mock_metadata.return_value = ("Video Titulo", "vid_99999")
            store.save_youtube_transcript("vid_99999", "Video Titulo", "Transcript content.")
            store.save_youtube_summary_pending_delivery("vid_99999", "Video Titulo", "Resumen previo no entregado.")

            mock_send_long_tg.return_value = True

            with patch("summary_generator.generate_summary") as mock_gen_sum:
                process_youtube_url("https://youtube.com/watch?v=vid_99999", store)

            mock_download_yt.assert_not_called()
            mock_exec_pipeline.assert_not_called()
            mock_gen_sum.assert_not_called()
            mock_send_long_tg.assert_called_once_with("Resumen previo no entregado.", parse_mode=None, required=True)

            yt_state = store.get_youtube_state("vid_99999")
            self.assertEqual(yt_state.get("summary_status"), "completed")

    @patch("main.send_long_telegram_message", side_effect=TelegramDeliveryError("Network Error", transient=True))
    @patch("main.send_telegram_message")
    @patch("main.download_youtube_audio")
    @patch("main.execute_pipeline")
    @patch("main.get_youtube_metadata")
    def test_summary_delivery_failure_marks_summary_failed_and_saves_pending(
        self, mock_metadata, mock_exec_pipeline, mock_download_yt, mock_send_tg, mock_send_long_tg
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "state.json")
            store = StateStore(file_path=file_path)

            mock_metadata.return_value = ("Video Titulo", "vid_88888")
            store.save_youtube_transcript("vid_88888", "Video Titulo", "Transcript content.")

            with patch("summary_generator.generate_summary", return_value="Nuevo resumen generado."):
                process_youtube_url("https://youtube.com/watch?v=vid_88888", store)

            yt_state = store.get_youtube_state("vid_88888")
            self.assertNotEqual(yt_state.get("summary_status"), "completed")
            self.assertEqual(store.get_youtube_summary("vid_88888"), "Nuevo resumen generado.")

    @patch("main.send_long_telegram_message", return_value=True)
    @patch("main.send_telegram_message")
    @patch("main.download_youtube_audio")
    @patch("main.execute_pipeline")
    @patch("main.get_youtube_metadata")
    def test_summary_with_special_symbols_sent_as_plain_text(
        self, mock_metadata, mock_exec_pipeline, mock_download_yt, mock_send_tg, mock_send_long_tg
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "state.json")
            store = StateStore(file_path=file_path)

            mock_metadata.return_value = ("Video Especial", "vid_symbols")
            store.save_youtube_transcript("vid_symbols", "Video Especial", "Transcript text")

            special_summary = (
                "Resumen Especial:\n"
                "- Punto 1: A & B comparativo\n"
                "- Punto 2: Resultado 5 < 10\n"
                "- Punto 3: Sintaxis List<Cliente> usuarios\n"
                "- Punto 4: Inyección <script>alert('xss')</script>"
            )

            with patch("summary_generator.generate_summary", return_value=special_summary):
                process_youtube_url("https://youtube.com/watch?v=vid_symbols", store)

            mock_send_long_tg.assert_called_once_with(special_summary, parse_mode=None, required=True)
            yt_state = store.get_youtube_state("vid_symbols")
            self.assertEqual(yt_state.get("summary_status"), "completed")

if __name__ == "__main__":
    unittest.main()
