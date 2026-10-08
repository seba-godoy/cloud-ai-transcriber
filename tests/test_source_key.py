import unittest
from utils import make_source_key

class TestSourceKey(unittest.TestCase):
    def test_different_sources_produce_different_keys(self):
        key_drive = make_source_key("drive", "file_123")
        key_tg = make_source_key("telegram", "file_123")
        key_yt = make_source_key("youtube", "file_123")
        self.assertNotEqual(key_drive, key_tg)
        self.assertNotEqual(key_drive, key_yt)

    def test_stable_key_output(self):
        key1 = make_source_key("drive", "abc_456")
        key2 = make_source_key("drive", "abc_456")
        self.assertEqual(key1, key2)

    def test_key_length_and_valid_chars(self):
        key = make_source_key("drive", "file_123")
        self.assertEqual(len(key), 64) # sha256 hex length
        self.assertTrue(all(c in "0123456789abcdef" for c in key))

if __name__ == "__main__":
    unittest.main()
