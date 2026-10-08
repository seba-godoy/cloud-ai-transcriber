import json
import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from config import Config
from google.auth.exceptions import TransportError
from utils import is_network_or_dns_error, is_transient_error, log_error, redact_secrets

SCOPES = ['https://www.googleapis.com/auth/drive']


class DriveAuthenticationError(RuntimeError):
    """Actionable Drive auth failure that never includes credential material."""


class DriveTransportError(ConnectionError):
    """Sanitized transient OAuth transport failure without sensitive upstream details."""


def _raise_sanitized(error, permanent_message, transient_message):
    """Preserve retry classification without exposing an upstream error message."""
    if (isinstance(error, TransportError) or is_network_or_dns_error(error)
            or is_transient_error(error)):
        # ConnectionError inheritance is sufficient for retry classification. Do not
        # retain the provider exception: Python tracebacks would expose its message.
        raise DriveTransportError(transient_message) from None
    raise DriveAuthenticationError(permanent_message) from None


def _local_credentials():
    token_path = Config.DRIVE_OAUTH_TOKEN_FILE
    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as error:
                _raise_sanitized(
                    error,
                    "Local Google Drive authorization is invalid or revoked; authorize the user again",
                    "Temporary network failure while refreshing local Google Drive authorization",
                )
        else:
            client_path = (Config.DRIVE_OAUTH_CLIENT_FILE
                           or Config.GOOGLE_APPLICATION_CREDENTIALS
                           or "credentials.json")
            flow = InstalledAppFlow.from_client_secrets_file(client_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, 'w', encoding='utf-8') as token_file:
            token_file.write(creds.to_json())
    return creds


def _authorized_user_credentials():
    try:
        info = json.loads(Config.DRIVE_OAUTH_TOKEN_JSON)
        if not isinstance(info, dict):
            raise ValueError("not an object")
        required = ("client_id", "client_secret", "refresh_token")
        if any(not info.get(field) for field in required):
            raise ValueError("missing required fields")
        creds = Credentials.from_authorized_user_info(info, SCOPES)
    except Exception:
        raise DriveAuthenticationError(
            "DRIVE_OAUTH_TOKEN_JSON is invalid or incomplete; provide a valid authorized-user OAuth JSON"
        ) from None
    if not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as error:
            _raise_sanitized(
                error,
                "Google Drive authorization is invalid or revoked; replace DRIVE_OAUTH_TOKEN_JSON",
                "Temporary network failure while refreshing Google Drive authorization",
            )
    return creds

def get_drive_service():
    """Initializes and returns a Google Drive v3 service instance using User OAuth."""
    try:
        if Config.DRIVE_AUTH_MODE == "authorized_user_json":
            creds = _authorized_user_credentials()
        elif Config.DRIVE_AUTH_MODE == "local":
            creds = _local_credentials()
        else:
            raise DriveAuthenticationError("Unsupported DRIVE_AUTH_MODE configuration")
        service = build('drive', 'v3', credentials=creds, cache_discovery=False)
        return service
    except Exception as e:
        log_error("get_drive_service", f"Failed to initialize Drive client via OAuth: {redact_secrets(str(e))}")
        if isinstance(e, (DriveAuthenticationError, DriveTransportError)):
            raise
        _raise_sanitized(
            e,
            "Failed to initialize Google Drive OAuth; verify the configured auth mode and credentials",
            "Temporary network failure while initializing the Google Drive client",
        )
