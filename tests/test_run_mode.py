import unittest
from unittest.mock import MagicMock, call, patch

from config import Config
import main


class RunModeConfigTests(unittest.TestCase):
    def test_default_run_mode_is_continuous(self):
        self.assertEqual(Config.RUN_MODE, "continuous")

    def test_invalid_run_mode_fails_validation(self):
        configured = type("InvalidRunMode", (), {
            **{name: getattr(Config, name) for name in dir(Config) if name.isupper()},
            "RUN_MODE": "daemon",
            "validate": classmethod(Config.validate.__func__),
        })
        with self.assertRaisesRegex(Exception, "RUN_MODE.*continuous.*once"):
            configured.validate()


class PollingLifecycleTests(unittest.TestCase):
    @patch("main.get_telegram_updates", return_value=[])
    @patch("main.process_file")
    @patch("main.list_pending_audio_files", return_value=[{"id": "one"}, {"id": "two"}])
    def test_cycle_processes_drive_sequentially(self, _list, process, _updates):
        store = MagicMock()
        self.assertEqual(main.run_polling_cycle(store, 7), 7)
        self.assertEqual(process.call_args_list, [call({"id": "one"}, store),
                                                  call({"id": "two"}, store)])

    @patch("main.is_authorized_telegram_message", return_value=False)
    @patch("main.get_telegram_updates", return_value=[{"update_id": 10, "message": {"chat": {"id": 9}}}])
    @patch("main.list_pending_audio_files", return_value=[])
    def test_cycle_preserves_telegram_offset_for_skipped_update(self, _list, _updates, _authorized):
        store = MagicMock()
        self.assertEqual(main.run_polling_cycle(store, 4), 11)
        store.set_telegram_offset.assert_called_once_with(11)

    def test_continuous_mode_repeats_cycles_and_sleeps(self):
        stop = RuntimeError("stop test loop")
        with patch("main.run_polling_cycle", side_effect=[2, 3]) as cycle, \
             patch("main.time.sleep", side_effect=[None, stop]) as sleep:
            with self.assertRaisesRegex(RuntimeError, "stop test loop"):
                main.run_continuous(MagicMock(), 1)
        self.assertEqual(cycle.call_count, 2)
        self.assertEqual(sleep.call_count, 2)

    def test_continuous_failure_recovers_durably_advanced_offset(self):
        store = MagicMock()
        store.get_telegram_offset.return_value = 11
        received_offsets = []

        def cycle(_store, offset):
            received_offsets.append(offset)
            if len(received_offsets) == 1:
                store.set_telegram_offset(11)
                raise RuntimeError("failure after durable offset")
            return offset

        stop = RuntimeError("stop test loop")
        with patch("main.run_polling_cycle", side_effect=cycle), \
             patch("main.time.sleep", side_effect=[None, stop]):
            with self.assertRaisesRegex(RuntimeError, "stop test loop"):
                main.run_continuous(store, 4)

        self.assertEqual(received_offsets, [4, 11])
        store.set_telegram_offset.assert_called_once_with(11)
        store.get_telegram_offset.assert_called_once_with()

    def _run_main_once(self, *, backend="firestore", cycle_side_effect=None):
        store = MagicMock()
        store.get_telegram_offset.return_value = 12
        store.acquire_global_cycle_lease.return_value = True
        lock = MagicMock()
        with patch.object(main.Config, "RUN_MODE", "once"), \
             patch.object(main.Config, "STATE_BACKEND", backend), \
             patch("main.Config.validate"), \
             patch("main.create_state_backend", return_value=store), \
             patch("main.StateStore", return_value=store), \
             patch("main.run_polling_cycle", side_effect=cycle_side_effect) as cycle, \
             patch("main.send_telegram_message") as notify, \
             patch("main.time.sleep") as sleep:
            main.main(instance_lock=lock)
        return lock, cycle, notify, sleep

    def test_once_runs_exactly_one_cycle_exits_without_sleep_or_startup_message(self):
        lock, cycle, notify, sleep = self._run_main_once()
        cycle.assert_called_once()
        sleep.assert_not_called()
        notify.assert_not_called()
        lock.acquire.assert_not_called()

    def test_once_cycle_failure_surfaces_as_process_failure(self):
        with self.assertRaisesRegex(RuntimeError, "fatal cycle"):
            self._run_main_once(backend="local", cycle_side_effect=RuntimeError("fatal cycle"))

    def test_firestore_mode_does_not_acquire_local_lock(self):
        lock, _cycle, _notify, _sleep = self._run_main_once(backend="firestore")
        lock.acquire.assert_not_called()
        lock.release.assert_not_called()

    def test_firestore_once_acquires_before_cycle_and_releases_afterward(self):
        lock, cycle, _notify, _sleep = self._run_main_once()
        store = cycle.call_args.args[0]
        store.acquire_global_cycle_lease.assert_called_once_with()
        store.release_global_cycle_lease.assert_called_once_with()

    def test_live_foreign_cycle_is_clean_noop(self):
        store = MagicMock()
        store.get_telegram_offset.return_value = 0
        store.acquire_global_cycle_lease.return_value = False
        lock = MagicMock()
        with patch.object(main.Config, "RUN_MODE", "once"), \
             patch.object(main.Config, "STATE_BACKEND", "firestore"), \
             patch("main.Config.validate"), \
             patch("main.create_state_backend", return_value=store), \
             patch("main.run_polling_cycle") as cycle:
            self.assertIsNone(main.main(instance_lock=lock))
        cycle.assert_not_called()
        store.get_telegram_offset.assert_not_called()
        store.release_global_cycle_lease.assert_not_called()

    def test_fatal_cycle_releases_without_masking_original(self):
        store = MagicMock()
        store.get_telegram_offset.return_value = 0
        store.acquire_global_cycle_lease.return_value = True
        store.release_global_cycle_lease.side_effect = RuntimeError("release failure")
        with patch.object(main.Config, "RUN_MODE", "once"), \
             patch.object(main.Config, "STATE_BACKEND", "firestore"), \
             patch("main.Config.validate"), \
             patch("main.create_state_backend", return_value=store), \
             patch("main.run_polling_cycle", side_effect=ValueError("poll failure")):
            with self.assertRaisesRegex(ValueError, "poll failure"):
                main.main(instance_lock=MagicMock())
        store.release_global_cycle_lease.assert_called_once_with()

    @patch("main.get_telegram_updates", return_value=[])
    @patch("main.process_file")
    @patch("main.list_pending_audio_files", return_value=[{"id": "one"}, {"id": "two"}])
    def test_cycle_renews_between_files_and_ingestion_phases(self, _list, _process, _updates):
        store = MagicMock()
        main.run_polling_cycle(store, 0)
        self.assertGreaterEqual(store.renew_global_cycle_lease_or_raise.call_count, 4)


if __name__ == "__main__":
    unittest.main()
