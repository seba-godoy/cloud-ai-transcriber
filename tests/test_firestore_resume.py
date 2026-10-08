import os
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from config import Config
from gemini_transcriber import transcribe_audio


class FakeChunk:
    def __len__(self): return 60_000
    def export(self, path, format=None):
        with open(path, "wb") as handle: handle.write(b"audio")


class FakeAudio:
    def __init__(self, count): self.count = count
    def __len__(self): return self.count * 60_000
    def __getitem__(self, item): return FakeChunk()


class MemoryChunkBackend:
    backend_name = "firestore"
    owner_id = "worker"
    lease_ttl_seconds = 900
    def __init__(self, chunks=None, fail_at=None, lose_global_at=None):
        self.chunks = list(chunks or []); self.fail_at = fail_at; self.events = []
        self.lose_global_at = lose_global_at; self.global_renewals = 0
    def ensure_job(self, *args, **kwargs): pass
    def list_completed_chunks(self, key): return list(self.chunks)
    def renew_lease(self, key, owner, ttl): return True
    def renew_global_cycle_lease_or_raise(self):
        from state_backend import LeaseLostError
        self.global_renewals += 1
        if self.global_renewals == self.lose_global_at:
            raise LeaseLostError("global lease lost")
    def save_chunk(self, key, index, transcript, model, words, owner_id, **metadata):
        self.events.append(("save", index))
        if index == self.fail_at: raise OSError("write unavailable")
        self.chunks.append({"chunk_index": index, "transcript": transcript,
                            "model_used": model, "word_count": words, **metadata})


class FirestoreResumeTests(unittest.TestCase):
    def _completed(self, count):
        return [{"chunk_index": i, "transcript": f"[{i}]\ntext {i}\n", "output_lines": [f"[{i}]", f"text {i}", ""],
                 "model_used": "model", "word_count": 2, "prev_transcript": f"text {i}"} for i in range(count)]

    def _run(self, backend, chunk_count, transcribe_side_effect):
        with TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "audio.mp3")
            with open(path, "wb") as handle: handle.write(b"source")
            with patch.object(Config, "TEMP_DIR", tmp), patch.object(Config, "CHUNK_SLEEP_SECONDS", 0), \
                 patch("gemini_transcriber.AudioSegment.from_file", return_value=FakeAudio(chunk_count)), \
                 patch("gemini_transcriber.probe_audio_duration", return_value=chunk_count * 60), \
                 patch("gemini_transcriber.detect_silence", return_value=[]), \
                 patch("gemini_transcriber.subprocess.run"), patch("gemini_transcriber.time.sleep"), \
                 patch("gemini_transcriber.genai.Client"), \
                 patch("gemini_transcriber.transcribe_chunk", side_effect=transcribe_side_effect) as call:
                result = transcribe_audio(path, file_id="id", file_name="audio.mp3",
                                          state_backend=backend, source="drive")
                return result, call

    def test_thirty_completed_chunks_resume_with_human_chunk_31(self):
        backend = MemoryChunkBackend(self._completed(30))
        result, call = self._run(backend, 31, [("new text", "model")])
        self.assertEqual(call.call_count, 1)
        self.assertIn("text 29", result[0])
        self.assertEqual(backend.events, [("save", 30)])

    def test_new_instance_reconstructs_previous_transcript(self):
        backend = MemoryChunkBackend(self._completed(2))
        result, call = self._run(backend, 3, [("text 1", "model")])
        # Duplicate detection proves prev_transcript came from persisted chunks.
        self.assertEqual(result[0].count("text 1"), 1)
        self.assertEqual(call.call_count, 1)

    def test_chunk_is_persisted_before_next_gemini_call(self):
        backend = MemoryChunkBackend()
        def transcribe(*args):
            index = len([event for event in backend.events if event[0] == "gemini"])
            if index: self.assertIn(("save", index - 1), backend.events)
            backend.events.append(("gemini", index)); return f"text {index}", "model"
        self._run(backend, 2, transcribe)
        self.assertEqual(backend.events, [("gemini", 0), ("save", 0), ("gemini", 1), ("save", 1)])
        self.assertEqual(backend.global_renewals, 8)

    def test_lost_global_lease_stops_before_next_gemini_call(self):
        backend = MemoryChunkBackend(lose_global_at=4)
        with self.assertRaisesRegex(Exception, "global lease lost"):
            self._run(backend, 2, [("one", "model"), ("two", "model")])
        self.assertEqual(backend.events, [("save", 0)])

    def test_persistence_failure_prevents_next_chunk(self):
        backend = MemoryChunkBackend(fail_at=0)
        with self.assertRaises(OSError): self._run(backend, 2, [("one", "model"), ("two", "model")])
        self.assertEqual(backend.events, [("save", 0)])


if __name__ == "__main__": unittest.main()
