import unittest
from unittest.mock import patch, MagicMock
import youtube_ingest
from youtube_ingest import get_youtube_metadata, download_youtube_audio
from main import process_youtube_url

class TestYouTubeDecoupled(unittest.TestCase):
    def test_youtube_ingest_has_no_statestore_import(self):
        self.assertNotIn("StateStore", dir(youtube_ingest))
        self.assertNotIn("state_store", dir(youtube_ingest))

    @patch("yt_dlp.YoutubeDL")
    def test_playlist_rejection(self, mock_ydl_class):
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = {"_type": "playlist", "id": "pl123", "title": "My Playlist"}
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        with self.assertRaises(ValueError) as ctx:
            get_youtube_metadata("https://youtube.com/playlist?list=pl123")
        self.assertIn("Playlists are not supported", str(ctx.exception))

    @patch("yt_dlp.YoutubeDL")
    def test_python_api_uses_explicit_deno_runtime(self, mock_ydl_class):
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = {"_type": "video", "id": "vid123", "title": "Video"}
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        with patch.dict("os.environ", {"YT_DLP_DENO_PATH": "/custom/deno"}):
            title, video_id = get_youtube_metadata("https://youtu.be/vid123")

        self.assertEqual((title, video_id), ("Video", "vid123"))
        opts = mock_ydl_class.call_args.args[0]
        self.assertEqual(opts["js_runtimes"], {"deno": {"path": "/custom/deno"}})

    @patch("yt_dlp.YoutubeDL")
    @patch("youtube_ingest.os.makedirs")
    @patch("youtube_ingest.os.path.isfile", return_value=True)
    @patch("youtube_ingest.os.path.getsize", return_value=1)
    def test_download_python_api_uses_same_explicit_deno_runtime(
        self, mock_getsize, mock_isfile, mock_makedirs, mock_ydl_class
    ):
        mock_ydl = MagicMock()
        mock_ydl.download.return_value = 0
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        with patch.dict("os.environ", {"YT_DLP_DENO_PATH": "/custom/deno"}):
            download_youtube_audio("https://youtu.be/vid123", "Video", "vid123")

        opts = mock_ydl_class.call_args.args[0]
        self.assertEqual(opts["js_runtimes"], {"deno": {"path": "/custom/deno"}})
        mock_ydl.download.assert_called_once_with(["https://youtu.be/vid123"])
        mock_isfile.assert_called()
        mock_getsize.assert_called()

    @patch("main.send_long_telegram_message")
    @patch("main.send_telegram_message")
    @patch("main.download_youtube_audio")
    @patch("main.get_youtube_metadata")
    def test_main_checks_statestore_before_download(
        self, mock_get_meta, mock_download_audio, mock_send_tg, mock_send_long_tg
    ):
        mock_get_meta.return_value = ("Título de Video", "vid_12345")
        
        mock_store = MagicMock()
        mock_store.is_processed.return_value = True # Already completed

        process_youtube_url("https://youtube.com/watch?v=vid_12345", mock_store)

        mock_get_meta.assert_called_once()
        mock_store.is_processed.assert_called_once_with("vid_12345")
        # Audio download should NOT be called since it's already completed
        mock_download_audio.assert_not_called()

if __name__ == "__main__":
    unittest.main()
