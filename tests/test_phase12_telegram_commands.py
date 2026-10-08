import unittest
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from state_backend import FirestoreStateBackend
from telegram_commands import TelegramCommandRuntime, install_phase12_runtime
from tests.test_state_backend import FakeClient


class TelegramCommandRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 6, 22, 30, tzinfo=timezone.utc)
        self.client = FakeClient()
        self.backend = FirestoreStateBackend(
            "test", client=self.client, clock_fn=lambda: self.now,
            owner_id="phase12-worker", lease_ttl_seconds=60,
        )
        self.runtime = TelegramCommandRuntime()
        self.runtime.bind_backend(self.backend)
        self.messages = []
        self.send_patch = patch(
            "telegram_commands.send_telegram_message",
            side_effect=lambda text, **kwargs: self.messages.append(text) or True,
        )
        self.auth_patch = patch("config.Config.TELEGRAM_CHAT_ID", "5572783241")
        self.send_patch.start()
        self.auth_patch.start()

    def tearDown(self):
        self.send_patch.stop()
        self.auth_patch.stop()

    def add_job(self, key, **fields):
        base = {
            "source_key": key,
            "source": "drive",
            "file_id": f"file-{key[:8]}",
            "name": f"audio-{key[:8]}.m4a",
            "status": "in_progress",
            "completed_chunks": 0,
            "retry_count": 0,
            "updated_at": self.now,
        }
        base.update(fields)
        self.client.data[f"transcription_jobs/{key}"] = deepcopy(base)
        return base

    def authorized_update(self, text, update_id=100):
        return {
            "update_id": update_id,
            "message": {
                "chat": {"id": 5572783241},
                "text": text,
            },
        }

    def test_help_command_is_consumed_but_update_identity_is_preserved(self):
        update = self.authorized_update("/ayuda")
        result = self.runtime.process_updates([update])
        self.assertEqual(result[0]["update_id"], 100)
        self.assertEqual(result[0]["message"]["chat"]["id"], 5572783241)
        self.assertEqual(result[0]["message"]["text"], "")
        self.assertIn("/estado", self.messages[-1])
        self.assertIn("/reintentar", self.messages[-1])

    def test_unauthorized_command_is_not_consumed(self):
        update = {
            "update_id": 101,
            "message": {"chat": {"id": 999}, "text": "/estado"},
        }
        result = self.runtime.process_updates([update])
        self.assertEqual(result, [update])
        self.assertEqual(self.messages, [])

    def test_unknown_authorized_command_is_left_for_existing_pipeline(self):
        update = self.authorized_update("/desconocido")
        result = self.runtime.process_updates([update])
        self.assertEqual(result, [update])
        self.assertEqual(self.messages, [])

    def test_status_summarizes_durable_jobs(self):
        self.add_job("a" * 64, status="completed")
        self.add_job("b" * 64, status="retryable")
        self.add_job("c" * 64, status="failed_permanent")
        self.runtime.handle_command("/estado")
        text = self.messages[-1]
        self.assertIn("Bot Transcriptor operativo", text)
        self.assertIn("Trabajos registrados: 3", text)
        self.assertIn("✅ 1", text)
        self.assertIn("🔁 1", text)
        self.assertIn("❌ 1", text)
        self.assertIn("v1.1.0-cloud", text)

    def test_queue_lists_only_operational_states_and_short_ids(self):
        self.add_job("deadbeef" + "1" * 56, status="failed_permanent", name="fallido.m4a")
        self.add_job("feedface" + "2" * 56, status="retryable", name="retry.m4a")
        self.add_job("cafebabe" + "3" * 56, status="completed", name="done.m4a")
        self.runtime.handle_command("/cola")
        text = self.messages[-1]
        self.assertIn("deadbeef", text)
        self.assertIn("feedface", text)
        self.assertNotIn("cafebabe", text)
        self.assertIn("/trabajo <id>", text)

    def test_job_detail_resolves_short_source_key_prefix(self):
        key = "deadbeef" + "1" * 56
        self.add_job(
            key,
            status="failed_permanent",
            completed_chunks=7,
            total_chunks=12,
            retry_count=2,
            error_message="sanitized failure",
        )
        self.runtime.handle_command("/trabajo deadbeef")
        text = self.messages[-1]
        self.assertIn("Trabajo [deadbeef]", text)
        self.assertIn("Bloques completados: 7/12", text)
        self.assertIn("sanitized failure", text)
        self.assertIn("/reintentar deadbeef", text)

    def test_job_detail_rejects_ambiguous_prefix(self):
        self.add_job("abcd0000" + "1" * 56)
        self.add_job("abcd1111" + "2" * 56)
        self.runtime.handle_command("/trabajo abcd")
        self.assertIn("ambiguo", self.messages[-1].lower())

    def test_retry_requires_explicit_confirmation_before_mutation(self):
        key = "deadbeef" + "1" * 56
        original = self.add_job(
            key,
            status="failed_permanent",
            error_message="ffmpeg decode failed",
            completed_chunks=4,
            partial_uploads={"docx": "d1"},
            final_ids={"pdf": "p1"},
            lease_owner="stale",
            lease_expires_at=self.now,
        )
        self.runtime.handle_command("/reintentar deadbeef")
        self.assertEqual(self.client.data[f"transcription_jobs/{key}"]["status"], "failed_permanent")
        self.assertIn("/reintentar deadbeef confirmar", self.messages[-1])

        self.runtime.handle_command("/reintentar deadbeef confirmar")
        job = self.client.data[f"transcription_jobs/{key}"]
        self.assertEqual(job["status"], "retryable")
        self.assertIsNone(job["lease_owner"])
        self.assertIsNone(job["lease_expires_at"])
        self.assertEqual(job["completed_chunks"], original["completed_chunks"])
        self.assertEqual(job["partial_uploads"], original["partial_uploads"])
        self.assertEqual(job["final_ids"], original["final_ids"])
        self.assertEqual(job["requeue_reason"], "telegram_operator_requeue_phase12")
        self.assertIn("quedó reencolado", self.messages[-1])

    def test_retry_refuses_non_drive_and_non_permanent_jobs(self):
        key = "telegram1" + "1" * 55
        self.add_job(key, source="telegram", status="failed_permanent", error_message="bad")
        self.runtime.handle_command("/reintentar telegram1 confirmar")
        self.assertIn("limitado a trabajos de Drive", self.messages[-1])

        key2 = "retrying" + "2" * 56
        self.add_job(key2, status="retryable", error_message="temporary")
        self.runtime.handle_command("/reintentar retrying confirmar")
        self.assertIn("no está en failed_permanent", self.messages[-1])

    def test_retry_second_confirmation_is_safe_after_first_requeue(self):
        key = "deadbeef" + "1" * 56
        self.add_job(key, status="failed_permanent", error_message="permanent failure")
        self.runtime.handle_command("/reintentar deadbeef confirmar")
        self.runtime.handle_command("/reintentar deadbeef confirmar")
        self.assertIn("no está en failed_permanent", self.messages[-1])

    def test_bot_username_suffix_and_english_aliases_are_supported(self):
        self.assertTrue(self.runtime.handle_command("/status@MyTranscriberBot"))
        self.assertIn("Bot Transcriptor operativo", self.messages[-1])
        self.assertTrue(self.runtime.handle_command("/help"))
        self.assertIn("Comandos", self.messages[-1])

    def test_command_failure_is_reported_without_raising_into_polling_cycle(self):
        runtime = TelegramCommandRuntime()
        runtime.backend = SimpleNamespace(backend_name="firestore", client=MagicMock())
        runtime.backend.client.collection.side_effect = RuntimeError("db unavailable")
        result = runtime.process_updates([self.authorized_update("/estado")])
        self.assertEqual(result[0]["message"]["text"], "")
        self.assertIn("seguirá funcionando normalmente", self.messages[-1])


class Phase12InstallationTests(unittest.TestCase):
    def test_install_captures_backend_and_wraps_get_updates(self):
        backend = SimpleNamespace(backend_name="firestore")
        app = SimpleNamespace(
            create_state_backend=MagicMock(return_value=backend),
            get_telegram_updates=MagicMock(return_value=[]),
        )
        runtime = install_phase12_runtime(app)
        returned = app.create_state_backend()
        self.assertIs(returned, backend)
        self.assertIs(runtime.backend, backend)
        self.assertEqual(app.get_telegram_updates(offset=5), [])


if __name__ == "__main__":
    unittest.main()
