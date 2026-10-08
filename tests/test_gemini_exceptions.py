import unittest
from unittest.mock import patch, MagicMock
from googleapiclient.errors import HttpError
from utils import is_transient_error, TranscriptionUnavailableError
from gemini_transcriber import GenerationResult, transcribe_chunk

class TestGeminiExceptions(unittest.TestCase):
    def test_transcription_unavailable_error_classified_as_transient(self):
        err = TranscriptionUnavailableError("Empty transcript returned")
        self.assertTrue(is_transient_error(err))

    @patch("time.sleep")
    @patch("gemini_transcriber._generate")
    def test_transcribe_chunk_raises_transcription_unavailable_on_empty_text(self, mock_generate, mock_sleep):
        mock_generate.return_value = GenerationResult("", "STOP", None, 1)
        
        mock_client = MagicMock()
        mock_file = MagicMock()
        mock_file.name = "files/123"
        mock_file.state = "ACTIVE"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        with self.assertRaises(TranscriptionUnavailableError) as ctx:
            transcribe_chunk(mock_client, "chunk.mp3", "flash", "pro", "prompt", 60000)
        self.assertTrue(is_transient_error(ctx.exception))

    @patch("time.sleep")
    @patch("gemini_transcriber._generate")
    def test_fallback_503_raises_transient_transcription_unavailable_error(self, mock_generate, mock_sleep):
        err503 = HttpError(resp=MagicMock(status=503), content=b'Service Unavailable')
        mock_generate.side_effect = [Exception("Default failed"), err503]

        mock_client = MagicMock()
        mock_file = MagicMock()
        mock_file.name = "files/123"
        mock_file.state = "ACTIVE"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        with self.assertRaises(TranscriptionUnavailableError) as ctx:
            transcribe_chunk(mock_client, "chunk.mp3", "flash", "pro", "prompt", 60000)
        self.assertTrue(is_transient_error(ctx.exception))

    @patch("time.sleep")
    @patch("gemini_transcriber._generate")
    def test_fallback_401_raises_permanent_exception(self, mock_generate, mock_sleep):
        err401 = HttpError(resp=MagicMock(status=401), content=b'Unauthorized')
        mock_generate.side_effect = [Exception("Default failed"), err401]

        mock_client = MagicMock()
        mock_file = MagicMock()
        mock_file.name = "files/123"
        mock_file.state = "ACTIVE"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        with self.assertRaises(HttpError) as ctx:
            transcribe_chunk(mock_client, "chunk.mp3", "flash", "pro", "prompt", 60000)
        self.assertEqual(ctx.exception.resp.status, 401)
        self.assertFalse(is_transient_error(ctx.exception))

    @patch("time.sleep")
    @patch("gemini_transcriber._generate")
    def test_fallback_400_invalid_argument_raises_permanent_exception(self, mock_generate, mock_sleep):
        err400 = HttpError(resp=MagicMock(status=400), content=b'INVALID_ARGUMENT: Invalid model specified')
        mock_generate.side_effect = [Exception("Default failed"), err400]

        mock_client = MagicMock()
        mock_file = MagicMock()
        mock_file.name = "files/123"
        mock_file.state = "ACTIVE"
        mock_client.files.upload.return_value = mock_file
        mock_client.files.get.return_value = mock_file

        with self.assertRaises(HttpError) as ctx:
            transcribe_chunk(mock_client, "chunk.mp3", "flash", "pro", "prompt", 60000)
        self.assertEqual(ctx.exception.resp.status, 400)
        self.assertFalse(is_transient_error(ctx.exception))

if __name__ == "__main__":
    unittest.main()
