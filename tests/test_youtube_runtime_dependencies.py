from pathlib import Path
import unittest


class YouTubeRuntimeDependencyTests(unittest.TestCase):
    def test_yt_dlp_default_extra_is_pinned_to_current_stable(self):
        requirements = (Path(__file__).parents[1] / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("yt-dlp[default]==2026.8.19", requirements)
        self.assertNotIn("yt-dlp==2026.7.4", requirements)

    def test_bgutil_pot_provider_is_pinned_to_security_fixed_release(self):
        requirements = (Path(__file__).parents[1] / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("bgutil-ytdlp-pot-provider==2.0.0", requirements)

    def test_wpc_pot_provider_is_pinned(self):
        requirements = (Path(__file__).parents[1] / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("yt-dlp-getpot-wpc==1.1.2", requirements)

    def test_deno_runtime_is_bundled_in_transcriber_image(self):
        dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM denoland/deno:bin-2.9.6 AS deno", dockerfile)
        self.assertIn("COPY --from=deno /deno /usr/local/bin/deno", dockerfile)
        self.assertIn("DENO_DIR=/app/.deno_cache", dockerfile)
        self.assertIn("/app/.deno_cache", dockerfile)

    def test_bgutil_deno_provider_runtime_is_bundled(self):
        dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM brainicism/bgutil-ytdlp-pot-provider:2.0.0-deno AS pot_provider", dockerfile)
        self.assertIn("COPY --from=pot_provider /app /opt/bgutil-provider", dockerfile)
        self.assertIn("YT_DLP_BGUTIL_SERVER_HOME=/opt/bgutil-provider", dockerfile)

    def test_browser_runtime_is_bundled_for_wpc_provider(self):
        dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("YT_DLP_WPC_BROWSER_PATH=/usr/bin/chromium", dockerfile)
        self.assertIn("chromium", dockerfile)
        self.assertIn("xvfb", dockerfile)
        self.assertIn("xauth", dockerfile)


if __name__ == "__main__":
    unittest.main()
