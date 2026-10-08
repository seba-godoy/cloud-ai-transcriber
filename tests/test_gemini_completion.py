import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gemini_transcriber import (
    GenerationResult, _result_from_response, normalize_finish_reason,
    transcribe_chunk,
)
from utils import TranscriptionUnavailableError


def result(text, reason):
    return GenerationResult(text, reason, None, 1 if reason is not None else 0)


class GeminiCompletionTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        remote = MagicMock(name="remote")
        remote.name = "files/chunk"
        remote.state = "ACTIVE"
        self.client.files.upload.return_value = remote
        self.client.files.get.return_value = remote

    def call(self, side_effect, duration=60000, silence_ratio=0.1):
        with patch("gemini_transcriber._generate", side_effect=side_effect):
            return transcribe_chunk(self.client, "chunk.mp3", "primary", "fallback",
                                    "prompt", duration, silence_ratio)

    def test_stop_nonempty_is_accepted_for_full_length_chunk(self):
        self.assertEqual(self.call([result("complete words", "STOP")]),
                         ("complete words", "primary"))

    def test_abnormal_partial_reasons_trigger_fallback(self):
        for reason in ("RECITATION", "MAX_TOKENS", "SAFETY"):
            with self.subTest(reason=reason):
                self.assertEqual(self.call([result("partial opening", reason),
                                            result("fallback complete answer", "STOP")]),
                                 ("fallback complete answer", "fallback"))

    def test_missing_candidate_and_missing_reason_trigger_fallback(self):
        for primary in (GenerationResult("partial", None, None, 0),
                        GenerationResult("partial", None, None, 1)):
            with self.subTest(candidate_count=primary.number_of_candidates):
                self.assertEqual(self.call([primary, result("fallback complete", "STOP")]),
                                 ("fallback complete", "fallback"))

    def test_both_abnormal_raise_and_partial_is_never_returned(self):
        with self.assertRaises(TranscriptionUnavailableError):
            self.call([result("primary secret partial", "MAX_TOKENS"),
                       result("fallback partial", "RECITATION")])

    def test_short_speech_regression_prefers_materially_complete_verification(self):
        opening = "A brief opening sentence has ended now"
        complete = " ".join(f"neutral{i}" for i in range(42))
        self.assertEqual(self.call([result(opening, "STOP"), result(complete, "STOP")],
                                   duration=15023, silence_ratio=0.05),
                         (complete, "fallback"))

    def test_short_slow_speech_does_not_use_fixed_minimum(self):
        primary = "very slow speech"
        self.assertEqual(self.call([result(primary, "STOP"), result("slow speech", "STOP")],
                                   duration=15000, silence_ratio=0.1),
                         (primary, "primary"))

    def test_short_speech_keeps_valid_primary_when_verification_is_empty(self):
        self.assertEqual(
            self.call([result("gracias", "STOP"), result("", "STOP")],
                      duration=27275, silence_ratio=0.0),
            ("gracias", "primary"),
        )

    def test_short_speech_keeps_valid_primary_when_verification_errors(self):
        self.assertEqual(
            self.call([result("gracias", "STOP"), RuntimeError("temporary timeout")],
                      duration=27275, silence_ratio=0.0),
            ("gracias", "primary"),
        )

    def test_short_speech_still_raises_when_both_results_are_unusable(self):
        with self.assertRaises(TranscriptionUnavailableError):
            self.call([result("", "STOP"), result("", "STOP")],
                      duration=27275, silence_ratio=0.0)

    def test_safe_diagnostic_log_has_metadata_not_text(self):
        secret_transcript = "private spoken material"
        with patch("gemini_transcriber.log_ok") as log:
            self.call([result(secret_transcript, "STOP")])
        rendered = " ".join(str(call) for call in log.call_args_list)
        self.assertIn("finish_reason=STOP", rendered)
        self.assertIn("output_word_count=3", rendered)
        self.assertNotIn(secret_transcript, rendered)

    def test_response_metadata_and_enum_normalization(self):
        enumish = SimpleNamespace(name="MAX_TOKENS")
        response = SimpleNamespace(text="partial", candidates=[SimpleNamespace(
            finish_reason=enumish, finish_message="limit reached")])
        parsed = _result_from_response(response)
        self.assertEqual((parsed.text, parsed.finish_reason, parsed.finish_message,
                          parsed.number_of_candidates),
                         ("partial", "MAX_TOKENS", "limit reached", 1))
        self.assertEqual(normalize_finish_reason("FinishReason.STOP"), "STOP")

    def test_prompt_requires_beginning_to_end_without_omissions(self):
        with open("gemini_transcriber.py", encoding="utf-8") as source_file:
            source = source_file.read()
        self.assertIn("beginning through the end", source)
        self.assertIn("Do not summarize or omit later speakers or later portions", source)
        self.assertIn("Never stop after only the first sentence", source)


if __name__ == "__main__":
    unittest.main()
