import unittest
from pathlib import Path

from config import Config, build_worker_id


class ConfigValidationTests(unittest.TestCase):
    @staticmethod
    def configured(**overrides):
        values = {name: getattr(Config, name) for name in dir(Config) if name.isupper()}
        values.update({
            "STATE_BACKEND": "local", "DRIVE_AUTH_MODE": "local",
            "DRIVE_OAUTH_CLIENT_FILE": "credentials.json", "DRIVE_OAUTH_TOKEN_FILE": "token.json",
            "DRIVE_OAUTH_TOKEN_JSON": None, "GOOGLE_APPLICATION_CREDENTIALS": None,
            "GEMINI_API_KEY": "gemini", "DRIVE_INPUT_FOLDER_ID": "input",
            "DRIVE_OUTPUT_FOLDER_ID": "output", "TELEGRAM_BOT_TOKEN": "telegram",
            "TELEGRAM_CHAT_ID": "chat", "FIRESTORE_LEASE_TTL_SECONDS": 900,
        })
        values.update(overrides)
        return type("TestConfig", (), {**values, "validate": classmethod(Config.validate.__func__)})

    def test_invalid_drive_mode_fails(self):
        with self.assertRaisesRegex(Exception, "DRIVE_AUTH_MODE"):
            self.configured(DRIVE_AUTH_MODE="other").validate()

    def test_local_accepts_deprecated_legacy_alias(self):
        self.configured(DRIVE_OAUTH_CLIENT_FILE=None,
                        GOOGLE_APPLICATION_CREDENTIALS="credentials.json").validate()

    def test_cloud_requires_token_json(self):
        with self.assertRaisesRegex(Exception, "DRIVE_OAUTH_TOKEN_JSON"):
            self.configured(DRIVE_AUTH_MODE="authorized_user_json").validate()

    def test_cloud_and_firestore_do_not_require_adc_key_file(self):
        self.configured(DRIVE_AUTH_MODE="authorized_user_json", DRIVE_OAUTH_TOKEN_JSON="{}",
                        DRIVE_OAUTH_CLIENT_FILE=None, DRIVE_OAUTH_TOKEN_FILE=None,
                        GOOGLE_APPLICATION_CREDENTIALS=None, STATE_BACKEND="firestore",
                        FIRESTORE_PROJECT_ID="project").validate()


class WorkerIdentityTests(unittest.TestCase):
    def test_explicit_worker_wins(self):
        self.assertEqual(build_worker_id({"WORKER_ID": "chosen"}, lambda: "uuid"), "chosen")

    def test_cloud_run_identity_is_readable_and_unique(self):
        env = {"CLOUD_RUN_EXECUTION": "daily", "CLOUD_RUN_TASK_INDEX": "2",
               "CLOUD_RUN_TASK_ATTEMPT": "1"}
        value = build_worker_id(env, lambda: "unique")
        self.assertEqual(value, "cloud-run-daily-task-2-attempt-1-unique")

    def test_fallback_workers_do_not_collide(self):
        first = build_worker_id({}, lambda: "one")
        second = build_worker_id({}, lambda: "two")
        self.assertEqual(first, "worker-one")
        self.assertEqual(second, "worker-two")
        self.assertNotEqual(first, second)


class FirestoreADCTests(unittest.TestCase):
    def test_backend_is_lazy_and_source_has_no_explicit_credentials(self):
        from state_backend import FirestoreStateBackend
        backend = FirestoreStateBackend(project_id="project")
        self.assertIsNone(backend._client)
        source = Path("state_backend.py").read_text(encoding="utf-8")
        client_line = next(line for line in source.splitlines()
                           if "firestore.Client(" in line)
        self.assertNotIn("credentials=", client_line)
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", source)


if __name__ == "__main__":
    unittest.main()
