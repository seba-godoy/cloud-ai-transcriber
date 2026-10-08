import os
import unittest
from unittest.mock import patch, MagicMock

from utils import SingleInstanceLock
import main

class TestSingleInstance(unittest.TestCase):
    def test_first_instance_acquires_lock(self):
        lock_name = "test_single_inst_lock_1"
        lock1 = SingleInstanceLock(lock_name=lock_name)
        acquired = lock1.acquire()
        try:
            self.assertTrue(acquired)
            self.assertTrue(lock1.is_acquired)
        finally:
            lock1.release()
            self.assertFalse(lock1.is_acquired)

    def test_second_instance_fails_lock_while_first_held(self):
        lock_name = "test_single_inst_lock_2"
        lock1 = SingleInstanceLock(lock_name=lock_name)
        lock2 = SingleInstanceLock(lock_name=lock_name)

        acquired1 = lock1.acquire()
        try:
            self.assertTrue(acquired1)
            acquired2 = lock2.acquire()
            self.assertFalse(acquired2)
            self.assertFalse(lock2.is_acquired)
        finally:
            lock1.release()

    def test_lock_released_allows_new_instance(self):
        lock_name = "test_single_inst_lock_3"
        lock1 = SingleInstanceLock(lock_name=lock_name)
        lock2 = SingleInstanceLock(lock_name=lock_name)

        lock1.acquire()
        lock1.release()

        acquired2 = lock2.acquire()
        try:
            self.assertTrue(acquired2)
        finally:
            lock2.release()

    def test_createmutex_real_error_raises_oserror(self):
        if os.name != 'nt':
            self.skipTest("Windows-specific ctypes test")

        lock = SingleInstanceLock("test_err_mutex")
        with patch("ctypes.windll.kernel32.CreateMutexW", return_value=0), \
             patch("ctypes.windll.kernel32.GetLastError", return_value=5): # Access denied
            with self.assertRaises(OSError) as ctx:
                lock.acquire()
            self.assertIn("CreateMutexW failed with WinError 5", str(ctx.exception))

    def test_posix_failed_flock_closes_handle_and_sets_none(self):
        lock = SingleInstanceLock("test_posix_fail_lock")
        mock_file = MagicMock()
        mock_fcntl = MagicMock()
        mock_fcntl.flock.side_effect = OSError("Lock held")

        with patch("os.name", "posix"), \
             patch.dict("sys.modules", {"fcntl": mock_fcntl}), \
             patch("builtins.open", return_value=mock_file):
            acquired = lock.acquire()
            self.assertFalse(acquired)
            self.assertFalse(lock.is_acquired)
            self.assertIsNone(lock.file_handle)
            mock_file.close.assert_called_once()

    @patch("main.list_pending_audio_files")
    @patch("main.Config.validate")
    @patch("main.StateStore")
    def test_main_fails_fast_when_lock_held_without_querying_drive(
        self, mock_state_store, mock_config_val, mock_list_pending
    ):
        mock_lock = MagicMock()
        mock_lock.acquire.return_value = False

        with self.assertRaises(SystemExit) as ctx:
            main.main(instance_lock=mock_lock)

        self.assertEqual(ctx.exception.code, 1)
        mock_list_pending.assert_not_called()
        mock_config_val.assert_not_called()
        mock_state_store.assert_not_called()

if __name__ == "__main__":
    unittest.main()
