import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class TestContainerPortability(unittest.TestCase):
    def test_posix_default_temp_dir_is_not_a_windows_path(self):
        import config
        if os.name == "nt":
            self.skipTest("POSIX path assertion")
        self.assertEqual(
            config.Config._default_temp_dir,
            os.path.join(tempfile.gettempdir(), "agente_transcriptor_bot"),
        )
        self.assertNotIn("C:\\", config.Config._default_temp_dir)

    def test_dockerfile_uses_non_root_runtime_and_required_packages(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM python:3.12-slim", dockerfile)
        self.assertIn("ffmpeg fonts-dejavu-core", dockerfile)
        self.assertIn("USER bot", dockerfile)
        self.assertIn('["python", "phase12_entrypoint.py"]', dockerfile)

    def test_dockerignore_excludes_runtime_state_and_secrets(self):
        patterns = set((ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())
        required = {".env", "credentials.json", "token.json", "processed_files.json",
                    "transcript_cache/", "tmp/", "output/", ".git/", "*.pem", "*.key",
                    "*_progress.json", "*_progress.corrupt_*.json",
                    "*_progress.incompatible_*.json"}
        self.assertTrue(required.issubset(patterns))


if __name__ == "__main__":
    unittest.main()
