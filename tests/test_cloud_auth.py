import json
import traceback
import unittest
from unittest.mock import MagicMock, mock_open, patch

import drive_client
from config import Config
from utils import is_network_or_dns_error, is_transient_error


class DriveAuthTests(unittest.TestCase):
    def setUp(self):
        self.originals = {name: getattr(Config, name) for name in (
            "DRIVE_AUTH_MODE", "DRIVE_OAUTH_CLIENT_FILE", "DRIVE_OAUTH_TOKEN_FILE",
            "DRIVE_OAUTH_TOKEN_JSON", "GOOGLE_APPLICATION_CREDENTIALS")}

    def tearDown(self):
        for name, value in self.originals.items():
            setattr(Config, name, value)

    @patch("drive_client.build")
    @patch("drive_client.Credentials.from_authorized_user_file")
    @patch("drive_client.os.path.exists", return_value=True)
    def test_local_valid_token_loads_file_without_flow(self, exists, from_file, build):
        Config.DRIVE_AUTH_MODE = "local"
        Config.DRIVE_OAUTH_TOKEN_FILE = "custom-token.json"
        creds = MagicMock(valid=True)
        from_file.return_value = creds
        with patch("drive_client.InstalledAppFlow.from_client_secrets_file") as flow:
            drive_client.get_drive_service()
        from_file.assert_called_once_with("custom-token.json", drive_client.SCOPES)
        flow.assert_not_called()

    @patch("drive_client.build")
    @patch("drive_client.open", new_callable=mock_open)
    @patch("drive_client.Credentials.from_authorized_user_file")
    @patch("drive_client.os.path.exists", return_value=True)
    def test_local_expired_token_refreshes_and_persists(self, exists, from_file, opened, build):
        Config.DRIVE_AUTH_MODE = "local"
        Config.DRIVE_OAUTH_TOKEN_FILE = "token.json"
        creds = MagicMock(valid=False, expired=True, refresh_token="refresh-value")
        creds.to_json.return_value = "updated"
        from_file.return_value = creds
        drive_client.get_drive_service()
        creds.refresh.assert_called_once()
        opened.assert_called_once_with("token.json", "w", encoding="utf-8")
        opened().write.assert_called_once_with("updated")

    @patch("drive_client.build")
    @patch("drive_client.open", new_callable=mock_open)
    @patch("drive_client.os.path.exists", return_value=False)
    def test_local_missing_token_runs_flow_and_saves(self, exists, opened, build):
        Config.DRIVE_AUTH_MODE = "local"
        Config.DRIVE_OAUTH_CLIENT_FILE = "client.json"
        Config.DRIVE_OAUTH_TOKEN_FILE = "token.json"
        creds = MagicMock(valid=True)
        creds.to_json.return_value = "authorized"
        with patch("drive_client.InstalledAppFlow.from_client_secrets_file") as factory:
            factory.return_value.run_local_server.return_value = creds
            drive_client.get_drive_service()
        factory.assert_called_once_with("client.json", drive_client.SCOPES)
        factory.return_value.run_local_server.assert_called_once_with(port=0)
        opened().write.assert_called_once_with("authorized")

    @patch("drive_client.build")
    @patch("drive_client.Credentials.from_authorized_user_info")
    def test_cloud_valid_json_is_memory_only(self, from_info, build):
        secret = {"client_id": "id", "client_secret": "secret", "refresh_token": "refresh"}
        Config.DRIVE_AUTH_MODE = "authorized_user_json"
        Config.DRIVE_OAUTH_TOKEN_JSON = json.dumps(secret)
        Config.GOOGLE_APPLICATION_CREDENTIALS = "must-not-be-used.json"
        from_info.return_value = MagicMock(valid=True)
        with patch("drive_client.open") as opened, patch(
                "drive_client.InstalledAppFlow.from_client_secrets_file") as flow:
            drive_client.get_drive_service()
        from_info.assert_called_once_with(secret, drive_client.SCOPES)
        opened.assert_not_called()
        flow.assert_not_called()

    @patch("drive_client.build")
    @patch("drive_client.Credentials.from_authorized_user_info")
    def test_cloud_expired_token_refreshes_without_write(self, from_info, build):
        Config.DRIVE_AUTH_MODE = "authorized_user_json"
        Config.DRIVE_OAUTH_TOKEN_JSON = json.dumps(
            {"client_id": "id", "client_secret": "secret", "refresh_token": "refresh"})
        creds = MagicMock(valid=False, expired=True, refresh_token="refresh")
        from_info.return_value = creds
        with patch("drive_client.open") as opened:
            drive_client.get_drive_service()
        creds.refresh.assert_called_once()
        opened.assert_not_called()

    def test_cloud_invalid_or_incomplete_json_is_sanitized(self):
        Config.DRIVE_AUTH_MODE = "authorized_user_json"
        for value, material in (("not-json-secret", "not-json-secret"),
                                (json.dumps({"client_secret": "super-secret"}), "super-secret")):
            with self.subTest(value=value):
                Config.DRIVE_OAUTH_TOKEN_JSON = value
                with self.assertRaises(drive_client.DriveAuthenticationError) as caught:
                    drive_client.get_drive_service()
                self.assertNotIn(material, str(caught.exception))
                self.assertFalse(is_transient_error(caught.exception))

    @patch("drive_client.Credentials.from_authorized_user_info")
    def test_cloud_refresh_network_failure_remains_transient_and_sanitized(self, from_info):
        secret = "VERY_SECRET_REFRESH_TOKEN"
        Config.DRIVE_AUTH_MODE = "authorized_user_json"
        Config.DRIVE_OAUTH_TOKEN_JSON = json.dumps(
            {"client_id": "id", "client_secret": "secret", "refresh_token": secret})
        creds = MagicMock(valid=False, expired=True, refresh_token=secret)
        creds.refresh.side_effect = ConnectionError(f"DNS failed with {secret}")
        from_info.return_value = creds
        with self.assertRaises(drive_client.DriveTransportError) as caught:
            drive_client.get_drive_service()
        self.assertTrue(is_transient_error(caught.exception))
        self.assertTrue(is_network_or_dns_error(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, "".join(traceback.format_exception(caught.exception)))

    @patch("drive_client.open", new_callable=mock_open)
    @patch("drive_client.Credentials.from_authorized_user_file")
    @patch("drive_client.os.path.exists", return_value=True)
    def test_local_refresh_network_failure_does_not_write(self, exists, from_file, opened):
        secret = "VERY_SECRET_LOCAL_REFRESH_TOKEN"
        Config.DRIVE_AUTH_MODE = "local"
        creds = MagicMock(valid=False, expired=True, refresh_token=secret)
        creds.refresh.side_effect = ConnectionError(f"connection failed with {secret}")
        from_file.return_value = creds
        with self.assertRaises(drive_client.DriveTransportError) as caught:
            drive_client.get_drive_service()
        self.assertTrue(is_transient_error(caught.exception))
        self.assertTrue(is_network_or_dns_error(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, "".join(traceback.format_exception(caught.exception)))
        opened.assert_not_called()

    @patch("drive_client.Credentials.from_authorized_user_info")
    def test_revoked_cloud_credentials_are_permanent_and_sanitized(self, from_info):
        secret = "revoked-refresh-secret"
        Config.DRIVE_AUTH_MODE = "authorized_user_json"
        Config.DRIVE_OAUTH_TOKEN_JSON = json.dumps(
            {"client_id": "id", "client_secret": "secret", "refresh_token": secret})
        creds = MagicMock(valid=False, expired=True, refresh_token=secret)
        creds.refresh.side_effect = ValueError(f"invalid_grant {secret}")
        from_info.return_value = creds
        with self.assertRaises(drive_client.DriveAuthenticationError) as caught:
            drive_client.get_drive_service()
        self.assertFalse(is_transient_error(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, "".join(traceback.format_exception(caught.exception)))


if __name__ == "__main__":
    unittest.main()
