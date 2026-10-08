import os
import json
import tempfile
import unittest
from unittest.mock import patch
import recuperar_estado

class TestRecuperarEstado(unittest.TestCase):
    def test_restore_valid_backup_with_custom_state_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_state = os.path.join(tmpdir, "custom_processed.json")
            rec_file = os.path.join(tmpdir, "custom_processed.recovery_required")
            init_file = os.path.join(tmpdir, "custom_processed.initialized")
            backup_file = os.path.join(tmpdir, "backup.json")

            with open(rec_file, "w", encoding="utf-8") as f:
                f.write("marker")
            with open(backup_file, "w", encoding="utf-8") as f:
                f.write(json.dumps({"f1": {"status": "completed"}}))

            recuperar_estado.restore_backup(backup_file, state_file_override=custom_state)

            self.assertTrue(os.path.exists(custom_state))
            with open(custom_state, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data, {"f1": {"status": "completed"}})

            self.assertFalse(os.path.exists(rec_file))
            self.assertTrue(os.path.exists(init_file))

    def test_restore_invalid_backup_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backup_file = os.path.join(tmpdir, "bad_backup.json")
            with open(backup_file, "w", encoding="utf-8") as f:
                f.write("NOT JSON {")

            with self.assertRaises(SystemExit):
                recuperar_estado.restore_backup(backup_file)

    def test_reset_without_literal_confirm_token_rejected(self):
        with self.assertRaises(SystemExit):
            recuperar_estado.reset_state("WRONG_TOKEN")

    def test_reset_with_custom_state_file_success(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_state = os.path.join(tmpdir, "my_state.json")
            rec_file = os.path.join(tmpdir, "my_state.recovery_required")
            init_file = os.path.join(tmpdir, "my_state.initialized")

            with open(rec_file, "w", encoding="utf-8") as f:
                f.write("marker")

            recuperar_estado.reset_state("RESET_STATE", state_file_override=custom_state)

            self.assertTrue(os.path.exists(custom_state))
            with open(custom_state, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertEqual(data, {})

            self.assertFalse(os.path.exists(rec_file))
            self.assertTrue(os.path.exists(init_file))

    def test_cleanup_failure_exits_with_code_1(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_state = os.path.join(tmpdir, "state.json")
            rec_file = os.path.join(tmpdir, "state.recovery_required")
            backup_file = os.path.join(tmpdir, "backup.json")

            with open(rec_file, "w", encoding="utf-8") as f:
                f.write("marker")
            with open(backup_file, "w", encoding="utf-8") as f:
                f.write(json.dumps({"f1": "ok"}))

            with patch("os.remove", side_effect=OSError("Permission denied")):
                with self.assertRaises(SystemExit) as ctx:
                    recuperar_estado.restore_backup(backup_file, state_file_override=custom_state)
                self.assertEqual(ctx.exception.code, 1)

if __name__ == "__main__":
    unittest.main()
