import os
import tempfile
import unittest
from unittest.mock import patch
from utils import save_cached_transcript, get_cached_transcript, get_transcript_cache_dir

class TestTranscriptCache(unittest.TestCase):
    def test_save_and_get_cached_transcript_valid_hash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_key = "yt_test_123"
            sample_text = "Esta es una transcripción de prueba almacenada en la caché persistente."
            
            with patch("config.Config.TRANSCRIPT_CACHE_DIR", tmpdir):
                rel_path, sha256_hash = save_cached_transcript(source_key, sample_text)
                self.assertTrue(os.path.exists(rel_path))

                retrieved = get_cached_transcript(rel_path, sha256_hash)
                self.assertEqual(retrieved, sample_text)

    def test_corrupt_cache_checksum_mismatch_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_key = "yt_corrupt_456"
            sample_text = "Texto original"
            
            with patch("config.Config.TRANSCRIPT_CACHE_DIR", tmpdir):
                rel_path, sha256_hash = save_cached_transcript(source_key, sample_text)
                
                # Corrupt the file content
                with open(rel_path, "w", encoding="utf-8") as f:
                    f.write("Texto alterado maliciosamente")

                retrieved = get_cached_transcript(rel_path, sha256_hash)
                self.assertIsNone(retrieved)

    def test_relative_cache_resolved_against_state_file_directory_when_cwd_changes(self):
        with tempfile.TemporaryDirectory() as bot_data_dir, \
             tempfile.TemporaryDirectory() as other_cwd_dir:
            
            state_file_path = os.path.join(bot_data_dir, "processed_files.json")
            original_cwd = os.getcwd()
            try:
                os.chdir(other_cwd_dir)
                with patch("config.Config.LOCAL_STATE_FILE", state_file_path), \
                     patch("config.Config.TRANSCRIPT_CACHE_DIR", "transcript_cache"):
                    
                    resolved_dir = get_transcript_cache_dir()
                    expected_dir = os.path.abspath(os.path.join(bot_data_dir, "transcript_cache"))
                    self.assertEqual(resolved_dir, expected_dir)

                    cache_path, sha256_hash = save_cached_transcript("test_rel_key", "Contenido guardado")
                    self.assertTrue(cache_path.startswith(expected_dir))
                    self.assertTrue(os.path.exists(cache_path))
                    
                    # Ensure it was NOT created in the other working directory
                    unwanted_path = os.path.join(other_cwd_dir, "transcript_cache")
                    self.assertFalse(os.path.exists(unwanted_path))

                    retrieved = get_cached_transcript(cache_path, sha256_hash)
                    self.assertEqual(retrieved, "Contenido guardado")
            finally:
                os.chdir(original_cwd)

    def test_absolute_cache_dir(self):
        with tempfile.TemporaryDirectory() as abs_dir:
            custom_cache = os.path.join(abs_dir, "custom_cache_dir")
            with patch("config.Config.TRANSCRIPT_CACHE_DIR", custom_cache):
                resolved = get_transcript_cache_dir()
                self.assertEqual(resolved, os.path.abspath(custom_cache))

    def test_relative_local_state_file_resolves_consistently(self):
        with patch("config.Config.LOCAL_STATE_FILE", "processed_files.json"), \
             patch("config.Config.TRANSCRIPT_CACHE_DIR", "transcript_cache"):
            resolved = get_transcript_cache_dir()
            expected = os.path.abspath("transcript_cache")
            self.assertEqual(resolved, expected)

    def test_directory_creation_automatic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            deep_cache = os.path.join(tmpdir, "nested", "sub", "cache")
            self.assertFalse(os.path.exists(deep_cache))
            with patch("config.Config.TRANSCRIPT_CACHE_DIR", deep_cache):
                cache_path, sha_hash = save_cached_transcript("key_dir_create", "Texto de prueba")
                self.assertTrue(os.path.exists(deep_cache))
                self.assertTrue(os.path.exists(cache_path))
                self.assertEqual(get_cached_transcript(cache_path, sha_hash), "Texto de prueba")

if __name__ == "__main__":
    unittest.main()
