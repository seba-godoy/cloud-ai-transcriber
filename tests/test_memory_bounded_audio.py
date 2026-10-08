import math
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from config import Config
from gemini_transcriber import audio_chunk_boundaries, probe_audio_duration, transcribe_audio


class MemoryBoundedAudioTests(unittest.TestCase):
    def test_ffprobe_duration_parses_deterministically_without_shell(self):
        result = MagicMock(stdout="3660.125\n")
        with patch("gemini_transcriber.subprocess.run", return_value=result) as run:
            self.assertEqual(probe_audio_duration("recording.m4a"), 3660.125)
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.args[0][0], "ffprobe")

    def test_ffprobe_rejects_invalid_durations_safely(self):
        for value in ("", "nan", "inf", "0", "-2", "not-a-number"):
            with self.subTest(value=value), patch("gemini_transcriber.subprocess.run",
                                                 return_value=MagicMock(stdout=value)):
                with self.assertRaisesRegex(ValueError, "duration"):
                    probe_audio_duration("private-name.m4a")

    def test_sixty_one_minutes_has_sixty_one_exact_boundaries(self):
        boundaries = audio_chunk_boundaries(61 * 60)
        self.assertEqual(len(boundaries), 61)
        self.assertEqual(boundaries[0], (0, 0, 60))
        self.assertEqual(boundaries[-1], (60, 3600, 3660))

    def test_final_partial_boundary(self):
        self.assertEqual(audio_chunk_boundaries(61.25), [(0, 0, 60), (1, 60, 61.25)])

    def test_transcriber_loads_only_materialized_chunk_and_cleans_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "full.m4a")
            open(source, "wb").close()
            seen = []
            def extract(src, dst, start, duration):
                self.assertEqual(src, source); self.assertFalse(any(os.path.exists(p) for p in seen))
                with open(dst, "wb") as handle: handle.write(b"chunk")
                seen.append(dst)
            chunk = MagicMock(); chunk.__len__.return_value = 60_000
            with patch.object(Config, "TEMP_DIR", temp_dir), patch.object(Config, "CHUNK_SLEEP_SECONDS", 0), \
                 patch("gemini_transcriber.probe_audio_duration", return_value=120), \
                 patch("gemini_transcriber.extract_audio_chunk", side_effect=extract), \
                 patch("gemini_transcriber.AudioSegment.from_file", return_value=chunk) as load, \
                 patch("gemini_transcriber.detect_silence", return_value=[(0, 60000)]), \
                 patch("gemini_transcriber.genai.Client"):
                transcribe_audio(source, file_id="id", file_name="full.m4a")
            self.assertEqual([c.args[0] for c in load.call_args_list], seen)
            self.assertNotIn(source, [c.args[0] for c in load.call_args_list])
            self.assertTrue(all(not os.path.exists(path) for path in seen))

    def test_chunk_cleanup_when_gemini_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = os.path.join(temp_dir, "full.m4a"); open(source, "wb").close()
            chunk_path = os.path.join(temp_dir, "full_chunk_0.wav")
            def extract(*args): open(args[1], "wb").close()
            chunk = MagicMock(); chunk.__len__.return_value = 1000
            with patch.object(Config, "TEMP_DIR", temp_dir), \
                 patch("gemini_transcriber.probe_audio_duration", return_value=1), \
                 patch("gemini_transcriber.extract_audio_chunk", side_effect=extract), \
                 patch("gemini_transcriber.AudioSegment.from_file", return_value=chunk), \
                 patch("gemini_transcriber.detect_silence", return_value=[]), \
                 patch("gemini_transcriber.transcribe_chunk", side_effect=RuntimeError("Gemini unavailable")), \
                 patch("gemini_transcriber.subprocess.run"), patch("gemini_transcriber.genai.Client"):
                with self.assertRaises(RuntimeError): transcribe_audio(source)
            self.assertFalse(os.path.exists(chunk_path))

    def test_execute_pipeline_estimate_never_decodes_full_source(self):
        from main import execute_pipeline
        store = MagicMock(backend_name="local")
        with patch("main.probe_audio_duration", return_value=10), \
             patch("main.AudioSegment.from_file") as full_decode, \
             patch("main.transcribe_audio", side_effect=RuntimeError("stop")), \
             patch("main.send_telegram_message"):
            success, _ = execute_pipeline("source.m4a", "source.m4a", "id", store, source="drive")
        self.assertFalse(success)
        full_decode.assert_not_called()

    def test_drive_initial_notification_claim_prevents_retry_storm(self):
        from main import process_file
        store = MagicMock(backend_name="firestore", owner_id="worker", lease_ttl_seconds=900)
        store.bind_source.return_value = "drive:id"
        store.acquire_lease.return_value = True
        store.claim_notification.side_effect = [True, False]
        with patch("main.send_telegram_message") as notify, \
             patch("main.download_file", return_value="source.m4a"), \
             patch("main.execute_pipeline"):
            process_file({"id": "id", "name": "class.m4a"}, store)
            process_file({"id": "id", "name": "class.m4a"}, store)
        notify.assert_called_once()


if __name__ == "__main__": unittest.main()
