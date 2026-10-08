import os
import json
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta
from state_store import StateStore
from utils import StatePersistenceError

class TestStateStore(unittest.TestCase):
    def test_first_legitimate_run_creates_state_and_initialized(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "state.json")
            init_path = os.path.join(tmpdir, "state.initialized")

            self.assertFalse(os.path.exists(file_path))
            self.assertFalse(os.path.exists(init_path))

            store = StateStore(file_path=file_path)

            self.assertTrue(os.path.exists(file_path))
            self.assertTrue(os.path.exists(init_path))
            self.assertFalse(store.is_processed("file_1"))

    def test_legacy_state_migration_creates_initialized_and_preserves_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "state.json")
            init_path = os.path.join(tmpdir, "state.initialized")

            # 1. Create legacy state file without initialized marker
            legacy_data = {
                "legacy_file_1": {
                    "file_id": "legacy_file_1",
                    "name": "legacy_audio.mp3",
                    "status": "completed"
                }
            }
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(legacy_data, f)

            self.assertTrue(os.path.exists(file_path))
            self.assertFalse(os.path.exists(init_path))

            # 2. Instantiate StateStore
            store = StateStore(file_path=file_path)

            # 3. Assert initialized marker created and legacy records preserved
            self.assertTrue(os.path.exists(init_path))
            self.assertTrue(store.is_processed("legacy_file_1"))

            # 4. Deleting state file after migration must be fatal (StatePersistenceError)
            os.remove(file_path)
            with self.assertRaises(StatePersistenceError):
                StateStore(file_path=file_path)

    def test_second_boot_after_corruption_blocked_by_recovery_required(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "state.json")
            rec_path = os.path.join(tmpdir, "state.recovery_required")
            
            # Create recovery_required marker
            with open(rec_path, "w", encoding="utf-8") as f:
                f.write(datetime.now(timezone.utc).isoformat())

            with self.assertRaises(StatePersistenceError) as ctx:
                StateStore(file_path=file_path)
            self.assertIn("recuperación manual", str(ctx.exception).lower())

    def test_initialized_present_and_state_missing_raises_state_persistence_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "state.json")
            init_path = os.path.join(tmpdir, "state.initialized")

            with open(init_path, "w", encoding="utf-8") as f:
                f.write(datetime.now(timezone.utc).isoformat())

            with self.assertRaises(StatePersistenceError) as ctx:
                StateStore(file_path=file_path)
            self.assertIn("desapareció", str(ctx.exception).lower())

    def test_disappearance_during_runtime_raises_state_persistence_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "state.json")
            store = StateStore(file_path=file_path)

            # Delete file after initialization
            os.remove(file_path)

            with self.assertRaises(StatePersistenceError) as ctx:
                store.is_processed("file_1")
            self.assertIn("desapareció", str(ctx.exception).lower())

    def test_corrupt_json_creates_backup_and_recovery_required(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "state.json")
            init_path = os.path.join(tmpdir, "state.initialized")
            rec_path = os.path.join(tmpdir, "state.recovery_required")

            # Create legitimate initialized file first
            with open(init_path, "w", encoding="utf-8") as f:
                f.write(datetime.now(timezone.utc).isoformat())

            # Write corrupt JSON
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("{ INVALID JSON CORRUPTED ...")

            with self.assertRaises(StatePersistenceError):
                StateStore(file_path=file_path)

            corrupt_files = [f for f in os.listdir(tmpdir) if f.startswith("state.corrupt_") and f.endswith(".json")]
            self.assertEqual(len(corrupt_files), 1)
            self.assertTrue(os.path.exists(rec_path))

    def test_state_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "state.json")
            store = StateStore(file_path=file_path)

            self.assertFalse(store.is_processed("file_1"))
            self.assertFalse(store.is_permanently_failed("file_1"))

            store.mark_retryable("file_1", "test.mp3", "drive", "Network error")
            data = store._load()["file_1"]
            self.assertEqual(data["status"], "retryable")
            self.assertEqual(data["retry_count"], 1)

            store.mark_processed("file_1", "test.mp3", "gemini-3.5-flash-lite", final_ids={"docx": "d1", "pdf": "p1"})
            self.assertTrue(store.is_processed("file_1"))
            data_proc = store._load()["file_1"]
            self.assertEqual(data_proc["status"], "completed")
            self.assertEqual(data_proc["final_ids"], {"docx": "d1", "pdf": "p1"})
            self.assertNotIn("retry_count", data_proc)

    def test_max_retries_transition_to_failed_permanent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "state.json")
            store = StateStore(file_path=file_path)
            
            with patch("config.Config.MAX_FILE_RETRIES", 3):
                store.mark_retryable("f1", "test.mp3", "drive", "err 1")
                self.assertFalse(store.is_permanently_failed("f1"))
                
                store.mark_retryable("f1", "test.mp3", "drive", "err 2")
                self.assertFalse(store.is_permanently_failed("f1"))

                store.mark_retryable("f1", "test.mp3", "drive", "err 3")
                self.assertTrue(store.is_permanently_failed("f1"))

    def test_persistence_failure_raises_state_persistence_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "state.json")
            store = StateStore(file_path=file_path)
            
            with patch("os.replace", side_effect=OSError("Disk write protected")):
                with self.assertRaises(StatePersistenceError):
                    store.mark_processed("f1", "test.mp3", "model")

    def test_telegram_offset_preservation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "state.json")
            store = StateStore(file_path=file_path)

            store.set_telegram_offset(42)
            self.assertEqual(store.get_telegram_offset(), 42)

            store.mark_processed("f1", "a.mp3", "model")
            self.assertEqual(store.get_telegram_offset(), 42)

if __name__ == "__main__":
    unittest.main()
