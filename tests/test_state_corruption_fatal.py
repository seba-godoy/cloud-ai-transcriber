import os
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from state_store import StateStore
from utils import StatePersistenceError
import main

class TestStateCorruptionFatal(unittest.TestCase):
    def test_corrupt_json_backs_up_and_raises_state_persistence_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "processed_files.json")
            init_path = os.path.join(tmpdir, "processed_files.initialized")
            with open(init_path, "w", encoding="utf-8") as f:
                f.write("2026-08-03T00:00:00Z")

            with open(file_path, "w", encoding="utf-8") as f:
                f.write("{ INVALID JSON CORRUPTED ...")

            with self.assertRaises(StatePersistenceError):
                StateStore(file_path=file_path)

            corrupt_files = [f for f in os.listdir(tmpdir) if f.startswith("processed_files.corrupt_")]
            self.assertEqual(len(corrupt_files), 1)

    def test_permission_error_raises_state_persistence_error_without_renaming(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = os.path.join(tmpdir, "processed_files.json")
            store = StateStore(file_path=file_path)
            with patch("builtins.open", side_effect=PermissionError("Access denied")):
                with self.assertRaises(StatePersistenceError):
                    store._load()

            corrupt_files = [f for f in os.listdir(tmpdir) if "corrupt" in f]
            self.assertEqual(len(corrupt_files), 0)

    @patch("main.log_error")
    @patch("main.sys.exit")
    @patch("main.StateStore")
    @patch("main.Config.validate")
    def test_main_exits_immediately_on_state_persistence_error_and_logs_once(
        self, mock_val, mock_state_store_cls, mock_sys_exit, mock_log_error
    ):
        mock_store = MagicMock()
        mock_store.get_telegram_offset.side_effect = StatePersistenceError("Corrupt state file")
        mock_state_store_cls.return_value = mock_store

        mock_sys_exit.side_effect = SystemExit(1)

        with self.assertRaises(SystemExit):
            main.main()

        mock_sys_exit.assert_called_once_with(1)
        # Assert log_error was called exactly once for main_init / StatePersistenceError
        self.assertEqual(mock_log_error.call_count, 1)
        args = mock_log_error.call_args[0]
        self.assertEqual(args[0], "main_init")
        self.assertIn("Corrupt state file", args[1])

if __name__ == "__main__":
    unittest.main()
