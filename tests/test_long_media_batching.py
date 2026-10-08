import os
import tempfile
import types
import unittest
from unittest.mock import patch

from config import Config
import long_media_runtime as runtime
from state_backend import FirestoreStateBackend


class LongMediaBatchingTests(unittest.TestCase):
    def setUp(self):
        self.original_save_chunk = FirestoreStateBackend.save_chunk
        self.original_temp_dir = Config.TEMP_DIR
        self.env_patch = patch.dict(
            os.environ,
            {
                "LONG_MEDIA_MAX_CHUNKS_PER_EXECUTION": "2",
                "LONG_MEDIA_MAX_EXECUTION_SECONDS": "2700",
                "LONG_MEDIA_DIRECT_MP4_THRESHOLD_SECONDS": "7200",
                "LONG_MEDIA_LOCAL_STAGE_MAX_BYTES": "1",
                "LONG_MEDIA_CACHE_DIR": "",
            },
            clear=False,
        )
        self.env_patch.start()
        runtime._STAGED_CACHE_ENTRIES.clear()

    def tearDown(self):
        FirestoreStateBackend.save_chunk = self.original_save_chunk
        Config.TEMP_DIR = self.original_temp_dir
        runtime._STAGED_CACHE_ENTRIES.clear()
        self.env_patch.stop()

    def fake_app(self):
        app = types.SimpleNamespace()
        app.logs = []
        app.warnings = []
        app.failures = []
        app.processed_items = []
        app.pipeline_paths = []
        app.download_calls = []
        app.log_ok = lambda message: app.logs.append(message)
        app.log_warn = lambda key, message: app.warnings.append((key, message))
        app.redact_secrets = str
        app.sanitize_filename = lambda name, fallback="audio_drive", suffix="": name
        app.get_drive_service = lambda: None
        app.handle_processing_failure = (
            lambda file_id, file_name, source, error, state_store:
            app.failures.append((file_id, file_name, source, error))
        )
        app.process_file = lambda item, state_store: app.processed_items.append(item["id"])
        app.run_polling_cycle = lambda state_store, offset: offset
        app.probe_audio_duration = lambda path: 3 * 60 * 60

        def download(file_id, file_name):
            app.download_calls.append((file_id, file_name))
            os.makedirs(Config.TEMP_DIR, exist_ok=True)
            path = os.path.join(Config.TEMP_DIR, file_name)
            with open(path, "wb") as handle:
                handle.write(b"source")
            return path

        app.download_file = download

        def execute(path, file_name, file_id, state_store, *, source, is_telegram_direct=False):
            app.pipeline_paths.append((path, file_name, file_id, source, is_telegram_direct))
            return True, "transcript"

        app.execute_pipeline = execute
        return app

    def install_with_durable_stub(self, app):
        durable_calls = []

        def durable_stub(backend, source_key, chunk_index, transcript,
                         model_used, word_count, owner_id, **metadata):
            durable_calls.append((source_key, chunk_index, metadata.get("total_chunks")))

        FirestoreStateBackend.save_chunk = durable_stub
        runtime.install_long_media_runtime(app)
        return durable_calls

    def backend(self):
        backend = object.__new__(FirestoreStateBackend)
        runtime._reset_batch_state(backend)
        return backend

    def test_chunk_budget_yields_only_after_chunk_is_durable(self):
        app = self.fake_app()
        durable_calls = self.install_with_durable_stub(app)
        backend = self.backend()

        backend.save_chunk("drive:abc", 0, "one", "model", 1, "worker", total_chunks=5)
        with self.assertRaises(runtime.CooperativeBatchYield) as caught:
            backend.save_chunk("drive:abc", 1, "two", "model", 1, "worker", total_chunks=5)

        self.assertEqual(durable_calls, [("drive:abc", 0, 5), ("drive:abc", 1, 5)])
        self.assertEqual(caught.exception.info.completed_chunks, 2)
        self.assertEqual(caught.exception.info.total_chunks, 5)
        self.assertEqual(caught.exception.info.reason, "chunk_budget")

    def test_final_chunk_never_yields(self):
        app = self.fake_app()
        durable_calls = self.install_with_durable_stub(app)
        backend = self.backend()
        backend._long_media_saved_chunks = 99

        backend.save_chunk("drive:abc", 4, "last", "model", 1, "worker", total_chunks=5)

        self.assertEqual(durable_calls[-1], ("drive:abc", 4, 5))

    def test_small_files_keep_single_execution_behavior(self):
        app = self.fake_app()
        self.install_with_durable_stub(app)
        backend = self.backend()

        for idx in range(2):
            backend.save_chunk("drive:small", idx, "x", "model", 1, "worker", total_chunks=2)

        self.assertIsNone(getattr(backend, "_long_media_batch_info", None))

    def test_wall_clock_budget_yields_after_persisting_chunk(self):
        app = self.fake_app()
        durable_calls = self.install_with_durable_stub(app)
        backend = object.__new__(FirestoreStateBackend)
        backend._long_media_saved_chunks = 0
        backend._long_media_batch_info = None
        backend._long_media_batch_started_at = 100.0

        with patch.object(runtime.time, "monotonic", return_value=2801.0):
            with self.assertRaises(runtime.CooperativeBatchYield) as caught:
                backend.save_chunk("drive:abc", 0, "one", "model", 1, "worker", total_chunks=5)

        self.assertEqual(durable_calls, [("drive:abc", 0, 5)])
        self.assertEqual(caught.exception.info.reason, "wall_clock_budget")

    def test_cooperative_yield_does_not_increment_failure_path(self):
        app = self.fake_app()
        self.install_with_durable_stub(app)
        backend = self.backend()
        info = runtime.CooperativeBatchInfo("drive:abc", 2, 5, 2, 12.0, "chunk_budget")

        app.handle_processing_failure(
            "abc", "long.mp3", "drive", runtime.CooperativeBatchYield(info), backend
        )

        self.assertEqual(app.failures, [])
        self.assertEqual(backend._long_media_batch_info, info)
        self.assertTrue(any("resume automatically" in line for line in app.logs))

    def test_after_long_batch_yield_other_drive_files_are_deferred(self):
        app = self.fake_app()

        def process(item, state_store):
            app.processed_items.append(item["id"])
            if item["id"] == "long":
                state_store._long_media_batch_info = runtime.CooperativeBatchInfo(
                    "drive:long", 90, 1680, 90, 1200.0, "chunk_budget"
                )

        app.process_file = process
        self.install_with_durable_stub(app)
        backend = self.backend()

        app.process_file({"id": "long"}, backend)
        app.process_file({"id": "other"}, backend)

        self.assertEqual(app.processed_items, ["long"])
        self.assertTrue(backend._long_media_cycle_yielded)

    def test_long_mp4_uses_direct_bounded_chunk_path(self):
        app = self.fake_app()
        self.install_with_durable_stub(app)
        backend = self.backend()

        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "lecture.mp4")
            with open(source, "wb") as handle:
                handle.write(b"fake")

            result = app.execute_pipeline(
                source,
                "lecture.mp4",
                "drive-id",
                backend,
                source="drive",
            )

            self.assertEqual(result, (True, "transcript"))
            working_path = app.pipeline_paths[-1][0]
            self.assertTrue(working_path.endswith(".mp4.longmedia"))
            self.assertFalse(os.path.exists(source))

    def test_short_mp4_keeps_legacy_pipeline_path(self):
        app = self.fake_app()
        app.probe_audio_duration = lambda path: 60 * 60
        self.install_with_durable_stub(app)
        backend = self.backend()

        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "lecture.mp4")
            with open(source, "wb") as handle:
                handle.write(b"fake")

            app.execute_pipeline(source, "lecture.mp4", "drive-id", backend, source="drive")

            self.assertEqual(app.pipeline_paths[-1][0], source)

    def test_persistent_cache_downloads_drive_source_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = os.path.join(tmp, "cache")
            Config.TEMP_DIR = os.path.join(tmp, "work")
            os.makedirs(Config.TEMP_DIR, exist_ok=True)
            with patch.dict(os.environ, {"LONG_MEDIA_CACHE_DIR": cache_dir}, clear=False):
                app = self.fake_app()
                self.install_with_durable_stub(app)
                identity = {"file_id": "id1", "size": "6", "modified_time": "stamp"}
                with patch.object(runtime, "_remote_drive_identity", return_value=identity):
                    first = app.download_file("id1", "long.mp3")
                    second = app.download_file("id1", "long.mp3")

                self.assertEqual(first, second)
                self.assertEqual(app.download_calls, [("id1", "long.mp3")])
                self.assertTrue(os.path.isfile(first))
                self.assertTrue(os.path.isfile(first + ".cache.json"))
                self.assertTrue(any("Reusing persistent" in line for line in app.logs))

    def test_ordinary_cached_source_is_staged_on_local_filesystem(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = os.path.join(tmp, "cache")
            Config.TEMP_DIR = os.path.join(tmp, "work")
            os.makedirs(Config.TEMP_DIR, exist_ok=True)
            with patch.dict(
                os.environ,
                {
                    "LONG_MEDIA_CACHE_DIR": cache_dir,
                    "LONG_MEDIA_LOCAL_STAGE_MAX_BYTES": "1024",
                },
                clear=False,
            ):
                app = self.fake_app()
                self.install_with_durable_stub(app)
                identity = {"file_id": "id1", "size": "6", "modified_time": "stamp"}
                with patch.object(runtime, "_remote_drive_identity", return_value=identity):
                    local_path = app.download_file("id1", "lecture.m4a")

                cached_path = os.path.join(cache_dir, "lecture.m4a")
                self.assertTrue(os.path.isfile(cached_path))
                self.assertTrue(os.path.isfile(cached_path + ".cache.json"))
                self.assertTrue(os.path.isfile(local_path))
                self.assertTrue(os.path.realpath(local_path).startswith(os.path.realpath(Config.TEMP_DIR)))
                self.assertFalse(os.path.islink(local_path))
                self.assertNotEqual(os.path.realpath(local_path), os.path.realpath(cached_path))
                self.assertIn(os.path.realpath(local_path), runtime._STAGED_CACHE_ENTRIES)
                self.assertTrue(any("Staged cached Drive source" in line for line in app.logs))

    def test_successful_pipeline_removes_persistent_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = os.path.join(tmp, "cache")
            Config.TEMP_DIR = os.path.join(tmp, "work")
            os.makedirs(cache_dir, exist_ok=True)
            os.makedirs(Config.TEMP_DIR, exist_ok=True)
            source = os.path.join(cache_dir, "long.mp3")
            marker = source + ".cache.json"
            with open(source, "wb") as handle:
                handle.write(b"source")
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write("{}")

            with patch.dict(os.environ, {"LONG_MEDIA_CACHE_DIR": cache_dir}, clear=False):
                app = self.fake_app()
                self.install_with_durable_stub(app)
                backend = self.backend()

                result = app.execute_pipeline(
                    source, "long.mp3", "id1", backend, source="drive"
                )

            self.assertEqual(result, (True, "transcript"))
            self.assertFalse(os.path.exists(source))
            self.assertFalse(os.path.exists(marker))
            self.assertTrue(any("cache released" in line for line in app.logs))


if __name__ == "__main__":
    unittest.main()
