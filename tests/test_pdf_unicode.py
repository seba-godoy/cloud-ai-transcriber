import os
import tempfile
import unittest
from unittest.mock import patch
from pdf_writer import create_pdf, resolve_pdf_fonts

class TestPdfUnicodeGeneration(unittest.TestCase):
    def test_unicode_pdf_creation(self):
        sample_transcript = "[00:00-01:00] Transcripción de prueba con tildes (áéíóú), eñes (ñ, Ñ) y signos españoles (¿qué tal?, ¡excelente!)."
        sample_filename = "clase_español.mp3"

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("config.Config.OUTPUT_DIR", tmpdir):
                pdf_path = create_pdf(sample_transcript, sample_filename)
                
                self.assertTrue(os.path.exists(pdf_path))
                self.assertGreater(os.path.getsize(pdf_path), 0)
                self.assertTrue(pdf_path.endswith(".pdf"))

    @patch("pdf_writer.os.path.isfile", return_value=False)
    def test_raises_runtime_error_when_no_ttf_font_found(self, mock_exists):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("config.Config.OUTPUT_DIR", tmpdir):
                with self.assertRaises(RuntimeError) as ctx:
                    create_pdf("sample text", "test.mp3")
                self.assertIn("No valid TrueType Unicode font found", str(ctx.exception))

    def test_configured_fonts_take_precedence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            regular = os.path.join(tmpdir, "custom-regular.ttf")
            bold = os.path.join(tmpdir, "custom-bold.ttf")
            for path in (regular, bold):
                with open(path, "wb") as font_file:
                    font_file.write(b"test")
            self.assertEqual(resolve_pdf_fonts(regular, bold), (regular, bold))

    @patch("pdf_writer.os.path.isfile")
    def test_linux_dejavu_fallback(self, isfile):
        isfile.side_effect = lambda path: path in {
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        }
        regular, bold = resolve_pdf_fonts(None, None)
        self.assertTrue(regular.endswith("DejaVuSans.ttf"))
        self.assertTrue(bold.endswith("DejaVuSans-Bold.ttf"))

if __name__ == "__main__":
    unittest.main()
