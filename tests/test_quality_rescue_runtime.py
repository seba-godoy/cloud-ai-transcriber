import unittest
from unittest.mock import MagicMock, patch

from gemini_transcriber import GenerationResult
from quality_rescue_runtime import (
    UNINTELLIGIBLE_LABEL,
    _build_rescuing_transcribe_chunk,
)
from utils import TranscriptionUnavailableError


def result(text: str, reason: str | None) -> GenerationResult:
    return GenerationResult(text, reason, None, 1 if reason is not None else 0)


class QualityRescueRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.args = (
            self.client,
            "chunk.wav",
            "gemini-3.5-flash-lite",
            "gemini-3.8-flash",
            "prompt",
            60000,
            0.1,
        )

    def test_normal_transcription_bypasses_rescue(self):
        original = MagicMock(return_value=("normal transcript", "primary"))
        wrapped = _build_rescuing_transcribe_chunk(original)
        with patch("quality_rescue_runtime._transcribe_with_dedicated_model") as rescue:
            self.assertEqual(wrapped(*self.args), ("normal transcript", "primary"))
        rescue.assert_not_called()

    def test_transport_failure_keeps_existing_retry_semantics(self):
        original_error = TranscriptionUnavailableError(
            "Both default and fallback models failed to transcribe chunk: timeout"
        )
        original = MagicMock(side_effect=original_error)
        wrapped = _build_rescuing_transcribe_chunk(original)
        with patch("quality_rescue_runtime._transcribe_with_dedicated_model") as rescue:
            with self.assertRaises(TranscriptionUnavailableError) as raised:
                wrapped(*self.args)
        self.assertIs(raised.exception, original_error)
        rescue.assert_not_called()

    def test_dedicated_model_recovers_semantic_quality_failure(self):
        original = MagicMock(side_effect=TranscriptionUnavailableError(
            "Both gemini-3.5-flash-lite and gemini-3.8-flash produced poor quality transcript."
        ))
        wrapped = _build_rescuing_transcribe_chunk(original)
        with patch("quality_rescue_runtime._rescue_model", return_value="gemini-3.5-transcribe"), \
             patch("quality_rescue_runtime._transcribe_with_dedicated_model",
                   return_value=result("recovered spoken words", "STOP")):
            self.assertEqual(
                wrapped(*self.args),
                ("recovered spoken words", "gemini-3.5-transcribe"),
            )

    def test_three_natural_quality_failures_persist_unintelligible_placeholder(self):
        original = MagicMock(side_effect=TranscriptionUnavailableError(
            "Both gemini-3.5-flash-lite and gemini-3.8-flash returned empty transcript text."
        ))
        wrapped = _build_rescuing_transcribe_chunk(original)
        with patch("quality_rescue_runtime._rescue_model", return_value="gemini-3.5-transcribe"), \
             patch("quality_rescue_runtime._transcribe_with_dedicated_model",
                   return_value=result("", "STOP")):
            self.assertEqual(wrapped(*self.args), (UNINTELLIGIBLE_LABEL, "local-filter"))

    def test_abnormal_rescue_finish_reason_is_not_silently_dropped(self):
        original = MagicMock(side_effect=TranscriptionUnavailableError(
            "Both primary and fallback produced poor quality transcript."
        ))
        wrapped = _build_rescuing_transcribe_chunk(original)
        with patch("quality_rescue_runtime._rescue_model", return_value="gemini-3.5-transcribe"), \
             patch("quality_rescue_runtime._transcribe_with_dedicated_model",
                   return_value=result("partial", "MAX_TOKENS")):
            with self.assertRaises(TranscriptionUnavailableError) as raised:
                wrapped(*self.args)
        self.assertIn("finish_reason=MAX_TOKENS", str(raised.exception))

    def test_transient_rescue_failure_remains_retryable(self):
        original = MagicMock(side_effect=TranscriptionUnavailableError(
            "Both primary and fallback produced poor quality transcript."
        ))
        wrapped = _build_rescuing_transcribe_chunk(original)
        with patch("quality_rescue_runtime._rescue_model", return_value="gemini-3.5-transcribe"), \
             patch("quality_rescue_runtime._transcribe_with_dedicated_model",
                   side_effect=TimeoutError("temporary timeout")):
            with self.assertRaises(TranscriptionUnavailableError) as raised:
                wrapped(*self.args)
        self.assertIsInstance(raised.exception.__cause__, TimeoutError)
        self.assertIn("failed transiently", str(raised.exception))

    def test_permanent_rescue_integration_failure_does_not_brick_long_job(self):
        original = MagicMock(side_effect=TranscriptionUnavailableError(
            "Both primary and fallback produced poor quality transcript."
        ))
        wrapped = _build_rescuing_transcribe_chunk(original)
        with patch("quality_rescue_runtime._rescue_model", return_value="gemini-3.5-transcribe"), \
             patch("quality_rescue_runtime._transcribe_with_dedicated_model",
                   side_effect=ValueError("unsupported deterministic request")):
            self.assertEqual(wrapped(*self.args), (UNINTELLIGIBLE_LABEL, "local-filter"))


if __name__ == "__main__":
    unittest.main()
