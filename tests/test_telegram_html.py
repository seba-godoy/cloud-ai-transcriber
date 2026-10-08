import unittest
from unittest.mock import patch, MagicMock
import main
from config import Config

class TestTelegramHtmlFormatting(unittest.TestCase):
    def setUp(self):
        self.orig_token = Config.TELEGRAM_BOT_TOKEN
        self.orig_chat = Config.TELEGRAM_CHAT_ID
        Config.TELEGRAM_BOT_TOKEN = "test-token"
        Config.TELEGRAM_CHAT_ID = "123"

    def tearDown(self):
        Config.TELEGRAM_BOT_TOKEN = self.orig_token
        Config.TELEGRAM_CHAT_ID = self.orig_chat

    @patch("main.send_telegram_message")
    @patch("main.download_file")
    @patch("main.execute_pipeline")
    def test_process_file_escapes_malicious_filename_in_telegram_msg(
        self, mock_pipeline, mock_download, mock_send_tg
    ):
        mock_download.return_value = "local.mp3"
        mock_store = MagicMock()
        
        malicious_item = {
            "id": "item123",
            "name": "<script>alert(1)&archivo.mp3"
        }
        
        main.process_file(malicious_item, mock_store)
        
        mock_send_tg.assert_called()
        first_msg = mock_send_tg.call_args_list[0][0][0]
        
        self.assertIn("<b>Audio recibido:</b>", first_msg)
        self.assertIn("<code>&lt;script&gt;alert(1)&amp;archivo.mp3</code>", first_msg)
        self.assertNotIn("<script>", first_msg)

    @patch("main.send_telegram_message")
    def test_handle_processing_failure_escapes_malicious_filename(self, mock_send_tg):
        mock_store = MagicMock()
        malicious_filename = "<iframe src='bad.html'>&name.mp3"
        
        main.handle_processing_failure("id123", malicious_filename, "drive", Exception("Failure"), mock_store)
        
        sent_text = mock_send_tg.call_args[0][0]
        self.assertIn("<code>&lt;iframe src=&#x27;bad.html&#x27;&gt;&amp;name.mp3</code>", sent_text)
        self.assertNotIn("<iframe", sent_text)

if __name__ == "__main__":
    unittest.main()
