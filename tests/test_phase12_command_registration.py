import unittest
from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from tools import register_telegram_commands


class TelegramCommandRegistrationTests(unittest.TestCase):
    def test_register_posts_expected_command_metadata_scoped_to_chat(self):
        calls = []

        def post(url, json, timeout):
            calls.append((url, json, timeout))
            return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": True})

        self.assertTrue(register_telegram_commands.register(
            "secret-token", "5572783241", post_fn=post))
        self.assertEqual(len(calls), 1)
        url, payload, timeout = calls[0]
        self.assertTrue(url.endswith("/setMyCommands"))
        self.assertIn("secret-token", url)
        self.assertEqual(timeout, 10)
        self.assertEqual(
            [item["command"] for item in payload["commands"]],
            ["ayuda", "estado", "cola", "trabajo", "reintentar"],
        )
        self.assertEqual(payload["scope"], {"type": "chat", "chat_id": 5572783241})

    def test_register_rejects_missing_token_or_chat(self):
        with self.assertRaises(ValueError):
            register_telegram_commands.register("", "5572783241")
        with self.assertRaises(ValueError):
            register_telegram_commands.register("token", "")
        with self.assertRaises(ValueError):
            register_telegram_commands.register("token", "not-a-chat")

    def test_username_chat_scope_is_allowed(self):
        calls = []

        def post(url, json, timeout):
            calls.append(json)
            return SimpleNamespace(status_code=200, json=lambda: {"ok": True})

        register_telegram_commands.register("token", "@authorizedchat", post_fn=post)
        self.assertEqual(calls[0]["scope"]["chat_id"], "@authorizedchat")

    def test_api_error_does_not_include_token_in_exception(self):
        def post(url, json, timeout):
            return SimpleNamespace(
                status_code=400,
                json=lambda: {"ok": False, "error_code": 400, "description": "Bad Request"},
            )

        token = "very-secret-token"
        with self.assertRaises(RuntimeError) as raised:
            register_telegram_commands.register(token, "5572783241", post_fn=post)
        self.assertNotIn(token, str(raised.exception))
        self.assertIn("Bad Request", str(raised.exception))

    def test_main_output_never_prints_token_or_chat_id(self):
        token = "very-secret-token"
        chat_id = "5572783241"
        output = StringIO()
        with patch.dict("os.environ", {
                "TELEGRAM_BOT_TOKEN": token,
                "TELEGRAM_CHAT_ID": chat_id,
             }, clear=False), \
             patch.object(register_telegram_commands, "register", return_value=True), \
             redirect_stdout(output):
            code = register_telegram_commands.main()
        self.assertEqual(code, 0)
        self.assertNotIn(token, output.getvalue())
        self.assertNotIn(chat_id, output.getvalue())
        self.assertIn('"ok": true', output.getvalue())
        self.assertIn('"scope": "authorized_chat"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
