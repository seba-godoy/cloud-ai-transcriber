import unittest
from gemini_transcriber import classify_audio_block

class TestSilenceClassification(unittest.TestCase):
    def test_classify_audio_block(self):
        self.assertEqual(classify_audio_block(0.99), "silence")
        self.assertEqual(classify_audio_block(0.98), "silence")
        self.assertEqual(classify_audio_block(0.97), "weak")
        self.assertEqual(classify_audio_block(0.95), "weak")
        self.assertEqual(classify_audio_block(0.94), "speech")
        self.assertEqual(classify_audio_block(0.65), "speech")

if __name__ == "__main__":
    unittest.main()
