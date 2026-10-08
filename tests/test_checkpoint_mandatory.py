import os
import ast
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from utils import ProgressPersistenceError, is_transient_error, is_disk_full_error
from gemini_transcriber import (
    save_progress_checkpoint,
    load_and_validate_progress_checkpoint,
    transcribe_audio
)

class TestCheckpointMandatory(unittest.TestCase):

    # =========================================================================
    # Requirement 2: AST Single Function Definition Test
    # =========================================================================

    def test_single_transcribe_audio_definition_ast(self):
        target_path = os.path.abspath("gemini_transcriber.py")
        with open(target_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename="gemini_transcriber.py")
            
        toplevel_funcs = [
            node.name for node in tree.body 
            if isinstance(node, ast.FunctionDef) and node.name == "transcribe_audio"
        ]
        self.assertEqual(
            len(toplevel_funcs), 1, 
            f"Expected exactly 1 top-level 'transcribe_audio' definition in gemini_transcriber.py, found {len(toplevel_funcs)}"
        )

    # =========================================================================
    # Requirement 2: Checkpoint Save Retry & Mandatory Abort Tests
    # =========================================================================

    def test_os_replace_fails_once_then_succeeds(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prog_file = os.path.join(tmpdir, "test_prog.json")
            state_data = {"test": 123}

            orig_replace = os.replace
            calls = 0

            def mock_replace(src, dst):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise PermissionError("File in use")
                return orig_replace(src, dst)

            with patch("os.replace", side_effect=mock_replace), \
                 patch("time.sleep"):
                save_progress_checkpoint(prog_file, state_data)

            self.assertEqual(calls, 2)
            self.assertTrue(os.path.exists(prog_file))

    def test_os_replace_fails_persistently_raises_progress_persistence_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prog_file = os.path.join(tmpdir, "test_prog.json")
            state_data = {"test": 123}

            with patch("os.replace", side_effect=PermissionError("File locked")), \
                 patch("time.sleep"):
                with self.assertRaises(ProgressPersistenceError):
                    save_progress_checkpoint(prog_file, state_data)

    def test_oserror_transient_retries_up_to_3_times(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prog_file = os.path.join(tmpdir, "test_oserr.json")
            state_data = {"test": 123}

            mock_sleep = MagicMock()
            with patch("os.replace", side_effect=OSError("Sharing violation")), \
                 patch("time.sleep", mock_sleep):
                with self.assertRaises(ProgressPersistenceError):
                    save_progress_checkpoint(prog_file, state_data)
            self.assertEqual(mock_sleep.call_count, 2)

    def test_type_error_in_json_dump_not_retried_and_propagates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prog_file = os.path.join(tmpdir, "test_type_err.json")
            unserializable_data = {"unsupported": set([1, 2, 3])}

            mock_sleep = MagicMock()
            with patch("time.sleep", mock_sleep):
                with self.assertRaises(TypeError):
                    save_progress_checkpoint(prog_file, unserializable_data)
            mock_sleep.assert_not_called()

    def test_type_error_not_classified_as_retryable(self):
        err = TypeError("Object of type set is not JSON serializable")
        self.assertFalse(is_transient_error(err))

    @patch("gemini_transcriber.detect_silence", return_value=[])
    @patch("gemini_transcriber.transcribe_chunk", return_value=("chunk text", "gemini-3.5-flash-lite"))
    @patch("gemini_transcriber.genai.Client")
    @patch("gemini_transcriber.AudioSegment.from_file")
    def test_next_chunk_not_processed_when_save_fails_persistently(
        self, mock_audio_from_file, mock_genai, mock_transcribe_chunk, mock_silence
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = os.path.join(tmpdir, "sample.mp3")
            with open(audio_path, "wb") as f:
                f.write(b"dummy audio data")

            mock_audio = MagicMock()
            mock_audio.__len__.return_value = 180000  # 3 chunks (180s)
            mock_audio.__getitem__.return_value = mock_audio
            mock_audio_from_file.return_value = mock_audio

            with patch("config.Config.TEMP_DIR", tmpdir), \
                 patch("gemini_transcriber.probe_audio_duration", return_value=180), \
                 patch("gemini_transcriber.extract_audio_chunk", side_effect=lambda s, d, a, b: open(d, "wb").write(b"audio")), \
                 patch("gemini_transcriber.save_progress_checkpoint", side_effect=ProgressPersistenceError("Lock fail")):
                
                with self.assertRaises(ProgressPersistenceError):
                    transcribe_audio(audio_path, use_pro_model=False, file_id="f1", file_name="sample.mp3")

                # Confirm ONLY chunk 1 was attempted; subsequent chunks were NOT processed!
                self.assertEqual(mock_transcribe_chunk.call_count, 1)

    def test_reading_permission_error_does_not_create_corrupt_backup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prog_file = os.path.join(tmpdir, "locked_prog.json")
            with open(prog_file, "w", encoding="utf-8") as f:
                f.write('{"valid": "json"}')

            with patch("builtins.open", side_effect=PermissionError("Read locked")), \
                 patch("time.sleep"):
                with self.assertRaises(ProgressPersistenceError):
                    load_and_validate_progress_checkpoint(prog_file, "f1", "file.mp3", "file", 5, 60000, 100)

            # Confirm file was NOT renamed to .corrupt
            corrupt_files = [f for f in os.listdir(tmpdir) if ".corrupt" in f]
            self.assertEqual(len(corrupt_files), 0)
            self.assertTrue(os.path.exists(prog_file))

    def test_truncated_json_creates_corrupt_backup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prog_file = os.path.join(tmpdir, "trunc_prog.json")
            with open(prog_file, "w", encoding="utf-8") as f:
                f.write('{"source_file_id": "f1", "incomplet')

            last_idx, _, _, _, _ = load_and_validate_progress_checkpoint(prog_file, "f1", "file.mp3", "file", 5, 60000, 100)

            self.assertEqual(last_idx, -1)
            corrupt_files = [f for f in os.listdir(tmpdir) if ".corrupt" in f]
            self.assertEqual(len(corrupt_files), 1)

    def test_disk_full_error_is_classified_as_permanent(self):
        err_enospc = OSError(28, "No space left on device")
        self.assertTrue(is_disk_full_error(err_enospc))
        self.assertFalse(is_transient_error(err_enospc))

        prog_err = ProgressPersistenceError("Save failed")
        prog_err.__cause__ = err_enospc
        self.assertFalse(is_transient_error(prog_err))

    # =========================================================================
    # Area 1: Metadata Validation & Schema 2 Tests
    # =========================================================================

    def test_full_v2_valid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prog_file = os.path.join(tmpdir, "valid_v2_prog.json")
            data = {
                "schema_version": 2,
                "source_file_id": "f123",
                "original_name": "audio.mp3",
                "total_chunks": 10,
                "chunk_duration_ms": 60000,
                "audio_size": 5000,
                "last_completed_chunk": 4,
                "last_idx": 3,
                "final_output": ["text 1..4"],
                "used_models": ["gemini-3.5-flash-lite"]
            }
            with open(prog_file, "w", encoding="utf-8") as f:
                json.dump(data, f)

            last_idx, output, models, _, _ = load_and_validate_progress_checkpoint(
                prog_file, "f123", "audio.mp3", "audio", 10, 60000, 5000
            )
            self.assertEqual(last_idx, 3)
            self.assertEqual(output, ["text 1..4"])

    def test_full_v1_valid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prog_file = os.path.join(tmpdir, "valid_v1_prog.json")
            data = {
                "source_file_id": "f123",
                "original_name": "audio.mp3",
                "total_chunks": 10,
                "chunk_duration_ms": 60000,
                "audio_size": 5000,
                "last_completed_chunk": 4,
                "last_idx": 3,
                "final_output": ["text 1..4"],
                "used_models": ["gemini-3.5-flash-lite"]
            }
            with open(prog_file, "w", encoding="utf-8") as f:
                json.dump(data, f)

            last_idx, output, models, _, _ = load_and_validate_progress_checkpoint(
                prog_file, "f123", "audio.mp3", "audio", 10, 60000, 5000
            )
            self.assertEqual(last_idx, 3)

    def test_genuine_legacy_valid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prog_file = os.path.join(tmpdir, "legacy_valid_prog.json")
            legacy_data = {
                "source_file_id": "legacy_f1",
                "original_name": "legacy_audio.mp3",
                "last_completed_chunk": 5,
                "last_idx": 4,
                "final_output": ["legacy chunk 1..5"],
                "used_models": ["gemini-3.5-flash-lite"]
            }
            with open(prog_file, "w", encoding="utf-8") as f:
                json.dump(legacy_data, f)

            last_idx, output, models, _, _ = load_and_validate_progress_checkpoint(
                prog_file, "legacy_f1", "legacy_audio.mp3", "legacy_audio", 12, 60000, 8000
            )
            self.assertEqual(last_idx, 4)
            self.assertEqual(output, ["legacy chunk 1..5"])

    def test_modern_missing_single_key_not_accepted_as_legacy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prog_file = os.path.join(tmpdir, "missing_key_prog.json")
            data = {
                "source_file_id": "f123",
                "original_name": "audio.mp3",
                "total_chunks": 10,
                # "chunk_duration_ms" is missing!
                "audio_size": 5000,
                "last_completed_chunk": 4,
                "last_idx": 3,
                "final_output": ["text"],
                "used_models": ["m"]
            }
            with open(prog_file, "w", encoding="utf-8") as f:
                json.dump(data, f)

            last_idx, _, _, _, _ = load_and_validate_progress_checkpoint(
                prog_file, "f123", "audio.mp3", "audio", 10, 60000, 5000
            )
            self.assertEqual(last_idx, -1)
            incomp = [f for f in os.listdir(tmpdir) if ".incompatible" in f]
            self.assertEqual(len(incomp), 1)

    def test_final_output_string_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prog_file = os.path.join(tmpdir, "str_output_prog.json")
            data = {
                "schema_version": 2,
                "source_file_id": "f123",
                "original_name": "audio.mp3",
                "total_chunks": 10,
                "chunk_duration_ms": 60000,
                "audio_size": 5000,
                "last_completed_chunk": 4,
                "last_idx": 3,
                "final_output": "NOT_A_LIST",
                "used_models": ["m"]
            }
            with open(prog_file, "w", encoding="utf-8") as f:
                json.dump(data, f)

            last_idx, _, _, _, _ = load_and_validate_progress_checkpoint(
                prog_file, "f123", "audio.mp3", "audio", 10, 60000, 5000
            )
            self.assertEqual(last_idx, -1)
            incomp = [f for f in os.listdir(tmpdir) if ".incompatible" in f]
            self.assertEqual(len(incomp), 1)

    def test_used_models_string_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prog_file = os.path.join(tmpdir, "str_models_prog.json")
            data = {
                "schema_version": 2,
                "source_file_id": "f123",
                "original_name": "audio.mp3",
                "total_chunks": 10,
                "chunk_duration_ms": 60000,
                "audio_size": 5000,
                "last_completed_chunk": 4,
                "last_idx": 3,
                "final_output": ["text"],
                "used_models": "NOT_A_LIST"
            }
            with open(prog_file, "w", encoding="utf-8") as f:
                json.dump(data, f)

            last_idx, _, _, _, _ = load_and_validate_progress_checkpoint(
                prog_file, "f123", "audio.mp3", "audio", 10, 60000, 5000
            )
            self.assertEqual(last_idx, -1)
            incomp = [f for f in os.listdir(tmpdir) if ".incompatible" in f]
            self.assertEqual(len(incomp), 1)

    def test_modern_total_chunks_present_chunk_duration_absent_incompatible(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prog_file = os.path.join(tmpdir, "tc_present_dur_absent.json")
            data = {
                "source_file_id": "f123",
                "original_name": "audio.mp3",
                "total_chunks": 10,
                "audio_size": 5000,
                "last_completed_chunk": 4,
                "last_idx": 3,
                "final_output": ["text"],
                "used_models": ["m"]
            }
            with open(prog_file, "w", encoding="utf-8") as f:
                json.dump(data, f)

            last_idx, _, _, _, _ = load_and_validate_progress_checkpoint(
                prog_file, "f123", "audio.mp3", "audio", 10, 60000, 5000
            )
            self.assertEqual(last_idx, -1)
            incomp = [f for f in os.listdir(tmpdir) if ".incompatible" in f]
            self.assertEqual(len(incomp), 1)

    def test_real_legacy_last_idx_29_resumes_from_chunk_31(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prog_file = os.path.join(tmpdir, "legacy_30_prog.json")
            legacy_data = {
                "source_file_id": "file_dns_123",
                "original_name": "long_audio.mp3",
                "last_completed_chunk": 30,
                "last_idx": 29,
                "final_output": ["chunk 1..30 completed"],
                "used_models": ["gemini-3.5-flash-lite"]
            }
            with open(prog_file, "w", encoding="utf-8") as f:
                json.dump(legacy_data, f)

            last_idx, output, models, _, _ = load_and_validate_progress_checkpoint(
                prog_file, "file_dns_123", "long_audio.mp3", "long_audio", 40, 60000, 10000
            )
            self.assertEqual(last_idx, 29)
            # Resumes from chunk 31 (last_idx + 2)
            self.assertEqual(last_idx + 2, 31)
            self.assertEqual(output, ["chunk 1..30 completed"])
            incomp = [f for f in os.listdir(tmpdir) if ".incompatible" in f]
            self.assertEqual(len(incomp), 0)

    def test_legacy_checkpoint_int_source_file_id_incompatible(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prog_file = os.path.join(tmpdir, "legacy_int_id.json")
            legacy_data = {
                "source_file_id": 123,
                "original_name": "audio.mp3",
                "last_completed_chunk": 2,
                "last_idx": 1,
                "final_output": ["text"],
                "used_models": ["m"]
            }
            with open(prog_file, "w", encoding="utf-8") as f:
                json.dump(legacy_data, f)

            last_idx, _, _, _, _ = load_and_validate_progress_checkpoint(
                prog_file, "f123", "audio.mp3", "audio", 10, 60000, 5000
            )
            self.assertEqual(last_idx, -1)
            incomp = [f for f in os.listdir(tmpdir) if ".incompatible" in f]
            self.assertEqual(len(incomp), 1)

    def test_legacy_checkpoint_int_original_name_incompatible(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            prog_file = os.path.join(tmpdir, "legacy_int_name.json")
            legacy_data = {
                "source_file_id": "f123",
                "original_name": 999,
                "last_completed_chunk": 2,
                "last_idx": 1,
                "final_output": ["text"],
                "used_models": ["m"]
            }
            with open(prog_file, "w", encoding="utf-8") as f:
                json.dump(legacy_data, f)

            last_idx, _, _, _, _ = load_and_validate_progress_checkpoint(
                prog_file, "f123", "audio.mp3", "audio", 10, 60000, 5000
            )
            self.assertEqual(last_idx, -1)
            incomp = [f for f in os.listdir(tmpdir) if ".incompatible" in f]
            self.assertEqual(len(incomp), 1)

    @patch("gemini_transcriber.detect_silence", return_value=[])
    @patch("gemini_transcriber.transcribe_chunk")
    @patch("gemini_transcriber.genai.Client")
    @patch("gemini_transcriber.AudioSegment.from_file")
    def test_all_chunks_completed_zero_transcribe_calls(
        self, mock_audio_from_file, mock_genai, mock_transcribe_chunk, mock_silence
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = os.path.join(tmpdir, "full_audio.mp3")
            with open(audio_path, "wb") as f:
                f.write(b"dummy audio data")

            mock_audio = MagicMock()
            mock_audio.__len__.return_value = 600000
            mock_audio.__getitem__.return_value = mock_audio
            mock_audio_from_file.return_value = mock_audio

            progress_file = os.path.join(tmpdir, "full_audio_progress.json")
            progress_data = {
                "schema_version": 2,
                "source_file_id": "f_full_100",
                "original_name": "full_audio.mp3",
                "total_chunks": 10,
                "chunk_duration_ms": 60000,
                "audio_size": len(b"dummy audio data"),
                "last_completed_chunk": 10,
                "last_idx": 9,
                "final_output": ["Chunk 1..10 completed"],
                "used_models": ["gemini-3.5-flash-lite"]
            }
            with open(progress_file, "w", encoding="utf-8") as f:
                json.dump(progress_data, f)

            with patch("config.Config.TEMP_DIR", tmpdir), \
                 patch("gemini_transcriber.probe_audio_duration", return_value=600):
                transcript, model = transcribe_audio(
                    audio_path, use_pro_model=False, file_id="f_full_100", file_name="full_audio.mp3"
                )

                mock_transcribe_chunk.assert_not_called()
                self.assertIn("Chunk 1..10 completed", transcript)

if __name__ == "__main__":
    unittest.main()
