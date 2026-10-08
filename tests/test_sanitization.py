import unittest
from unittest.mock import patch
from utils import sanitize_filename, redact_secrets

class TestFilenameSanitizationAndRedaction(unittest.TestCase):
    def test_path_traversal(self):
        clean = sanitize_filename("../../malicioso.mp3")
        self.assertEqual(clean, "malicioso.mp3")

    def test_invalid_windows_chars(self):
        clean = sanitize_filename("clase: marketing?.m4a")
        self.assertEqual(clean, "clase_ marketing_.m4a")

    def test_reserved_windows_name(self):
        clean = sanitize_filename("CON.mp3")
        self.assertEqual(clean, "CON_file.mp3")

    def test_accented_unicode_chars(self):
        clean = sanitize_filename("nombre con tildes áéíóú.mp3")
        self.assertEqual(clean, "nombre con tildes áéíóú.mp3")

    def test_trailing_dots_and_spaces(self):
        clean = sanitize_filename("nombre terminado en punto. .")
        self.assertEqual(clean, "nombre terminado en punto")

    def test_suffix_collision_avoidance(self):
        clean1 = sanitize_filename("clase:ventas.mp3", suffix="a1b2c3d4")
        clean2 = sanitize_filename("clase?ventas.mp3", suffix="e5f6g7h8")
        self.assertNotEqual(clean1, clean2)
        self.assertTrue(clean1.endswith("_a1b2c3d4.mp3"))
        self.assertTrue(clean2.endswith("_e5f6g7h8.mp3"))

    @patch("config.Config.TELEGRAM_BOT_TOKEN", "123456:SECRET_BOT_TOKEN")
    @patch("config.Config.GEMINI_API_KEY", "AIzaSy_SECRET_GEMINI_KEY")
    def test_redact_secrets(self):
        raw = "Error at https://api.telegram.org/bot123456:SECRET_BOT_TOKEN/getFile?key=AIzaSy_SECRET_GEMINI_KEY"
        redacted = redact_secrets(raw)
        self.assertNotIn("SECRET_BOT_TOKEN", redacted)
        self.assertNotIn("SECRET_GEMINI_KEY", redacted)
        self.assertIn("[REDACTED_TELEGRAM_TOKEN]", redacted)

if __name__ == "__main__":
    unittest.main()
