import os
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import youtube_ingest


class _FailingYoutubeDL:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, *args, **kwargs):
        raise RuntimeError("primary metadata route failed")

    def download(self, *args, **kwargs):
        raise RuntimeError("primary download route failed")


class YouTubeCloudRunFallbackTests(unittest.TestCase):
    @patch.object(youtube_ingest.yt_dlp, "YoutubeDL", _FailingYoutubeDL)
    @patch.object(youtube_ingest.subprocess, "run")
    def test_metadata_falls_back_to_cli(self, run_mock):
        run_mock.return_value = subprocess.CompletedProcess(
            args=["yt-dlp"], returncode=0, stdout="rj3rAEoHqBk\tFallback title\n", stderr=""
        )

        title, video_id = youtube_ingest.get_youtube_metadata("https://youtu.be/rj3rAEoHqBk")

        self.assertEqual(video_id, "rj3rAEoHqBk")
        self.assertEqual(title, "Fallback title")
        command = run_mock.call_args.args[0]
        self.assertIn("--js-runtimes", command)
        self.assertIn("deno:/usr/local/bin/deno", command)

    @patch.object(youtube_ingest.yt_dlp, "YoutubeDL", _FailingYoutubeDL)
    @patch.object(youtube_ingest.subprocess, "run", side_effect=RuntimeError("cli metadata failed"))
    @patch.object(youtube_ingest.requests, "get")
    def test_metadata_falls_back_to_oembed(self, get_mock, _run_mock):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"title": "oEmbed title"}
        get_mock.return_value = response

        title, video_id = youtube_ingest.get_youtube_metadata("https://youtu.be/rj3rAEoHqBk")

        self.assertEqual(video_id, "rj3rAEoHqBk")
        self.assertEqual(title, "oEmbed title")
        get_mock.assert_called_once()

    def test_pot_extractor_options_use_mweb_and_bundled_provider(self):
        with patch.dict(
            "os.environ",
            {"YT_DLP_BGUTIL_SERVER_HOME": "/opt/test-bgutil"},
        ):
            opts = youtube_ingest._youtube_pot_extractor_options()

        self.assertEqual(opts["extractor_args"]["youtube"]["player_client"], ["mweb"])
        self.assertEqual(
            opts["extractor_args"]["youtubepot-bgutilscript"]["server_home"],
            ["/opt/test-bgutil"],
        )

    @patch.object(youtube_ingest, "_wpc_provider_available", return_value=True)
    @patch.object(youtube_ingest, "_bgutil_provider_available", return_value=True)
    @patch.object(youtube_ingest, "_download_with_wpc_cli")
    @patch.object(youtube_ingest, "_download_with_cli")
    @patch.object(youtube_ingest, "_download_with_python_api")
    def test_wpc_route_is_attempted_before_bgutil_and_legacy_routes(
        self, api_mock, cli_mock, wpc_mock, _bgutil_mock, _wpc_available_mock
    ):
        with tempfile.TemporaryDirectory() as tmp:
            old_temp = youtube_ingest.Config.TEMP_DIR
            youtube_ingest.Config.TEMP_DIR = tmp
            try:
                expected_name = youtube_ingest.sanitize_filename(
                    "Fallback title.mp3", fallback="youtube_audio.mp3", suffix="rj3rAEoH"
                )
                expected_path = os.path.join(tmp, expected_name)

                def wpc_success(_url, _stem):
                    with open(expected_path, "wb") as handle:
                        handle.write(b"audio")
                    return expected_path

                wpc_mock.side_effect = wpc_success

                path, name = youtube_ingest.download_youtube_audio(
                    "https://youtu.be/rj3rAEoHqBk", "Fallback title", "rj3rAEoHqBk"
                )

                self.assertEqual(path, expected_path)
                self.assertEqual(name, expected_name)
                wpc_mock.assert_called_once()
                api_mock.assert_not_called()
                cli_mock.assert_not_called()
            finally:
                youtube_ingest.Config.TEMP_DIR = old_temp

    @patch.object(youtube_ingest, "_wpc_provider_available", return_value=False)
    @patch.object(youtube_ingest, "_bgutil_provider_available", return_value=True)
    @patch.object(youtube_ingest, "_download_with_cli")
    @patch.object(youtube_ingest, "_download_with_python_api")
    def test_bgutil_pot_route_is_attempted_when_browser_provider_is_unavailable(
        self, api_mock, cli_mock, _provider_mock, _wpc_mock
    ):
        with tempfile.TemporaryDirectory() as tmp:
            old_temp = youtube_ingest.Config.TEMP_DIR
            youtube_ingest.Config.TEMP_DIR = tmp
            try:
                expected_name = youtube_ingest.sanitize_filename(
                    "Fallback title.mp3", fallback="youtube_audio.mp3", suffix="rj3rAEoH"
                )
                expected_path = os.path.join(tmp, expected_name)

                def api_success(_url, _stem, player_client=None, use_pot=False):
                    self.assertEqual(player_client, "mweb")
                    self.assertTrue(use_pot)
                    with open(expected_path, "wb") as handle:
                        handle.write(b"audio")
                    return expected_path

                api_mock.side_effect = api_success

                path, name = youtube_ingest.download_youtube_audio(
                    "https://youtu.be/rj3rAEoHqBk", "Fallback title", "rj3rAEoHqBk"
                )

                self.assertEqual(path, expected_path)
                self.assertEqual(name, expected_name)
                api_mock.assert_called_once()
                cli_mock.assert_not_called()
            finally:
                youtube_ingest.Config.TEMP_DIR = old_temp

    @patch.object(youtube_ingest, "_wpc_provider_available", return_value=False)
    @patch.object(youtube_ingest, "_bgutil_provider_available", return_value=False)
    @patch.object(youtube_ingest, "_download_with_python_api", side_effect=RuntimeError("api failed"))
    @patch.object(youtube_ingest, "_download_with_cli")
    def test_download_uses_cli_default_after_python_api_failure(
        self, cli_mock, _api_mock, _bgutil_mock, _wpc_mock
    ):
        with tempfile.TemporaryDirectory() as tmp:
            old_temp = youtube_ingest.Config.TEMP_DIR
            youtube_ingest.Config.TEMP_DIR = tmp
            try:
                expected_name = youtube_ingest.sanitize_filename(
                    "Fallback title.mp3", fallback="youtube_audio.mp3", suffix="rj3rAEoH"
                )
                expected_path = os.path.join(tmp, expected_name)

                def cli_success(_url, _stem, player_client=None, use_pot=False):
                    self.assertIsNone(player_client)
                    self.assertFalse(use_pot)
                    with open(expected_path, "wb") as handle:
                        handle.write(b"audio")
                    return expected_path

                cli_mock.side_effect = cli_success

                path, name = youtube_ingest.download_youtube_audio(
                    "https://youtu.be/rj3rAEoHqBk", "Fallback title", "rj3rAEoHqBk"
                )

                self.assertEqual(path, expected_path)
                self.assertEqual(name, expected_name)
                self.assertGreater(os.path.getsize(path), 0)
            finally:
                youtube_ingest.Config.TEMP_DIR = old_temp

    @patch.object(youtube_ingest, "_wpc_provider_available", return_value=False)
    @patch.object(youtube_ingest, "_bgutil_provider_available", return_value=False)
    @patch.object(youtube_ingest, "_download_with_python_api", side_effect=RuntimeError("api failed"))
    @patch.object(youtube_ingest, "_download_with_cli")
    def test_download_uses_web_embedded_after_web_safari_fallback(
        self, cli_mock, _api_mock, _bgutil_mock, _wpc_mock
    ):
        with tempfile.TemporaryDirectory() as tmp:
            old_temp = youtube_ingest.Config.TEMP_DIR
            youtube_ingest.Config.TEMP_DIR = tmp
            try:
                expected_name = youtube_ingest.sanitize_filename(
                    "Fallback title.mp3", fallback="youtube_audio.mp3", suffix="rj3rAEoH"
                )
                expected_path = os.path.join(tmp, expected_name)
                calls = []

                def cli_side_effect(_url, _stem, player_client=None, use_pot=False):
                    calls.append(player_client)
                    self.assertFalse(use_pot)
                    if player_client in (None, "web_safari"):
                        raise RuntimeError(f"{player_client or 'default'} cli failed")
                    self.assertEqual(player_client, "web_embedded")
                    with open(expected_path, "wb") as handle:
                        handle.write(b"audio")
                    return expected_path

                cli_mock.side_effect = cli_side_effect

                path, _ = youtube_ingest.download_youtube_audio(
                    "https://youtu.be/rj3rAEoHqBk", "Fallback title", "rj3rAEoHqBk"
                )

                self.assertEqual(path, expected_path)
                self.assertEqual(calls, [None, "web_safari", "web_embedded"])
            finally:
                youtube_ingest.Config.TEMP_DIR = old_temp

    @patch.object(youtube_ingest, "_wpc_provider_available", return_value=True)
    @patch.object(youtube_ingest.subprocess, "run")
    def test_wpc_cli_uses_xvfb_chromium_and_mweb(self, run_mock, _available_mock):
        with tempfile.TemporaryDirectory() as tmp:
            old_temp = youtube_ingest.Config.TEMP_DIR
            youtube_ingest.Config.TEMP_DIR = tmp
            try:
                expected_path = os.path.join(tmp, "test.mp3")

                def fake_run(command, **kwargs):
                    self.assertEqual(command[0], "xvfb-run")
                    self.assertIn("youtube:player_client=mweb", command)
                    self.assertIn("youtubepot-wpc:browser_path=/usr/bin/chromium", command)
                    with open(expected_path, "wb") as handle:
                        handle.write(b"audio")
                    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

                run_mock.side_effect = fake_run
                path = youtube_ingest._download_with_wpc_cli("https://youtu.be/rj3rAEoHqBk", "test")
                self.assertEqual(path, expected_path)
            finally:
                youtube_ingest.Config.TEMP_DIR = old_temp

    def test_error_classification_recognizes_antibot_po_token_and_browser_runtime(self):
        self.assertEqual(
            youtube_ingest._classify_youtube_error(RuntimeError("Sign in to confirm you're not a bot")),
            "anti_bot_or_login_required",
        )
        self.assertEqual(
            youtube_ingest._classify_youtube_error(RuntimeError("PO Token required")),
            "po_token",
        )
        self.assertEqual(
            youtube_ingest._classify_youtube_error(RuntimeError("Chromium could not start under Xvfb")),
            "browser_runtime",
        )


if __name__ == "__main__":
    unittest.main()
