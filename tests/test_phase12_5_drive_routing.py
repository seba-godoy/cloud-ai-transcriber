import unittest
from types import SimpleNamespace
from unittest.mock import patch

from config import Config
from drive_watcher import list_pending_audio_files
from phase12_entrypoint import install_drive_routing_runtime


class _Request:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class _FilesApi:
    def __init__(self, by_folder):
        self.by_folder = by_folder
        self.queries = []

    def list(self, **kwargs):
        query = kwargs["q"]
        self.queries.append(query)
        folder_id = query.split("'", 2)[1]
        return _Request({"files": self.by_folder.get(folder_id, []), "nextPageToken": None})


class _DriveService:
    def __init__(self, by_folder):
        self.files_api = _FilesApi(by_folder)

    def files(self):
        return self.files_api


class _State:
    backend_name = "firestore"

    def __init__(self):
        self.bound = {}
        self.jobs = {}

    def bind_source(self, source, file_id, name=""):
        self.bound[file_id] = (source, name)
        return self._key(file_id)

    def _key(self, file_id):
        return f"drive-key-{file_id}"

    def ensure_job(self, source_key, **fields):
        self.jobs.setdefault(source_key, {}).update(fields)

    def is_processed(self, file_id):
        return False

    def is_permanently_failed(self, file_id):
        return False

    def is_retry_due(self, file_id):
        return True

    def get_job(self, source_key):
        return self.jobs.get(source_key)


class Phase125DriveRoutingTests(unittest.TestCase):
    def setUp(self):
        self.saved = {
            "primary_in": Config.DRIVE_PRIMARY_INPUT_FOLDER_ID,
            "primary_out": Config.DRIVE_PRIMARY_OUTPUT_FOLDER_ID,
            "legacy_in": Config.DRIVE_INPUT_FOLDER_ID,
            "legacy_out": Config.DRIVE_OUTPUT_FOLDER_ID,
            "secondary_in": Config.DRIVE_SECONDARY_INPUT_FOLDER_ID,
            "secondary_out": Config.DRIVE_SECONDARY_OUTPUT_FOLDER_ID,
            "routes": dict(Config._DRIVE_FILE_ROUTES),
        }
        Config.DRIVE_PRIMARY_INPUT_FOLDER_ID = "primary-input"
        Config.DRIVE_PRIMARY_OUTPUT_FOLDER_ID = "primary-output"
        Config.DRIVE_INPUT_FOLDER_ID = "primary-input"
        Config.DRIVE_OUTPUT_FOLDER_ID = "primary-output"
        Config.DRIVE_SECONDARY_INPUT_FOLDER_ID = "secondary-input"
        Config.DRIVE_SECONDARY_OUTPUT_FOLDER_ID = "secondary-output"
        Config._DRIVE_FILE_ROUTES = {}

    def tearDown(self):
        Config.DRIVE_PRIMARY_INPUT_FOLDER_ID = self.saved["primary_in"]
        Config.DRIVE_PRIMARY_OUTPUT_FOLDER_ID = self.saved["primary_out"]
        Config.DRIVE_INPUT_FOLDER_ID = self.saved["legacy_in"]
        Config.DRIVE_OUTPUT_FOLDER_ID = self.saved["legacy_out"]
        Config.DRIVE_SECONDARY_INPUT_FOLDER_ID = self.saved["secondary_in"]
        Config.DRIVE_SECONDARY_OUTPUT_FOLDER_ID = self.saved["secondary_out"]
        Config._DRIVE_FILE_ROUTES = self.saved["routes"]

    def test_config_exposes_both_routes_and_resets_to_primary(self):
        self.assertEqual(
            Config.drive_folder_routes(),
            [
                {"owner_id": "primary", "input_folder_id": "primary-input", "output_folder_id": "primary-output"},
                {"owner_id": "secondary", "input_folder_id": "secondary-input", "output_folder_id": "secondary-output"},
            ],
        )

        Config.register_drive_file_route(
            "secondary-file", owner_id="secondary",
            input_folder_id="secondary-input", output_folder_id="secondary-output",
        )
        route = Config.activate_drive_file_route("secondary-file")
        self.assertEqual(route["owner_id"], "secondary")
        self.assertEqual(Config.DRIVE_OUTPUT_FOLDER_ID, "secondary-output")

        Config.activate_default_drive_route()
        self.assertEqual(Config.DRIVE_OUTPUT_FOLDER_ID, "primary-output")

    def test_watcher_scans_both_inputs_and_persists_owner_metadata(self):
        service = _DriveService({
            "primary-input": [{"id": "s1", "name": "Primary clase.mp3", "mimeType": "audio/mpeg"}],
            "secondary-input": [{"id": "c1", "name": "Secondary clase.m4a", "mimeType": "audio/mp4"}],
        })
        state = _State()

        with patch("drive_watcher.get_drive_service", return_value=service):
            items = list_pending_audio_files(state)

        self.assertEqual({item["id"] for item in items}, {"s1", "c1"})
        by_id = {item["id"]: item for item in items}
        self.assertEqual(by_id["s1"]["owner_id"], "primary")
        self.assertEqual(by_id["s1"]["output_folder_id"], "primary-output")
        self.assertEqual(by_id["c1"]["owner_id"], "secondary")
        self.assertEqual(by_id["c1"]["output_folder_id"], "secondary-output")
        self.assertEqual(state.jobs["drive-key-s1"]["owner_id"], "primary")
        self.assertEqual(state.jobs["drive-key-c1"]["owner_id"], "secondary")
        self.assertEqual(len(service.files_api.queries), 2)

    def test_telegram_boundary_resets_output_after_secondary_drive_job(self):
        calls = []

        def original_get_updates(*args, **kwargs):
            calls.append((args, kwargs, Config.DRIVE_OUTPUT_FOLDER_ID))
            return [{"update_id": 1}]

        app_module = SimpleNamespace(get_telegram_updates=original_get_updates)
        Config.DRIVE_OUTPUT_FOLDER_ID = "secondary-output"

        install_drive_routing_runtime(app_module)
        result = app_module.get_telegram_updates(offset=9, timeout=5)

        self.assertEqual(result, [{"update_id": 1}])
        self.assertEqual(Config.DRIVE_OUTPUT_FOLDER_ID, "primary-output")
        self.assertEqual(calls[0][2], "primary-output")
        self.assertTrue(app_module._phase12_5_drive_routing_installed)


if __name__ == "__main__":
    unittest.main()
