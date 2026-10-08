import unittest
from unittest.mock import patch
from telegram_notifier import split_long_text, send_long_telegram_message
from config import Config

class TestLongMessageSplit(unittest.TestCase):
    def setUp(self):
        self.orig_token = Config.TELEGRAM_BOT_TOKEN
        self.orig_chat = Config.TELEGRAM_CHAT_ID
        Config.TELEGRAM_BOT_TOKEN = "test-token"
        Config.TELEGRAM_CHAT_ID = "123"

    def tearDown(self):
        Config.TELEGRAM_BOT_TOKEN = self.orig_token
        Config.TELEGRAM_CHAT_ID = self.orig_chat

    def test_paragraph_5000_chars_split_correctly(self):
        words = ["palabra"] * 700
        text = " ".join(words)
        self.assertGreater(len(text), 5000)

        chunks = split_long_text(text, max_chars=4000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 4000)

        reconstructed = "".join(chunks)
        self.assertEqual(reconstructed, text)

    def test_text_without_spaces_forced_split(self):
        text = "x" * 5000
        chunks = split_long_text(text, max_chars=4000)

        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0]), 4000)
        self.assertEqual(len(chunks[1]), 1000)

        reconstructed = "".join(chunks)
        self.assertEqual(reconstructed, text)

    def test_text_with_newlines_and_paragraphs(self):
        p1 = "a" * 2500
        p2 = "b" * 2500
        text = f"{p1}\n\n{p2}"

        chunks = split_long_text(text, max_chars=4000)
        self.assertEqual(len(chunks), 2)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 4000)

        reconstructed = "".join(chunks)
        self.assertEqual(reconstructed, text)

    @patch("telegram_notifier.send_telegram_message_with_retry", return_value=True)
    def test_send_long_telegram_message_calls_send_message_per_chunk(self, mock_send_msg):
        text = ("Instrucción importante\n\n" + "x" * 3000 + "\n\n" + "y" * 3000)
        send_long_telegram_message(text, max_chars=4000)

        self.assertGreater(mock_send_msg.call_count, 1)
        sent_text = "".join(call[0][0] for call in mock_send_msg.call_args_list)
        self.assertEqual(sent_text, text)
        for call in mock_send_msg.call_args_list:
            chunk = call[0][0]
            self.assertLessEqual(len(chunk), 4000)

if __name__ == "__main__":
    unittest.main()
