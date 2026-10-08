import os
import tempfile
import uuid
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

DRIVE_AUTH_MODES = ("local", "authorized_user_json")


def build_worker_id(environ=None, uuid_factory=uuid.uuid4):
    """Build a unique, readable lease owner without relying on host identity."""
    env = os.environ if environ is None else environ
    explicit = env.get("WORKER_ID", "").strip()
    if explicit:
        return explicit
    execution = env.get("CLOUD_RUN_EXECUTION", "").strip()
    task_index = env.get("CLOUD_RUN_TASK_INDEX", "").strip()
    task_attempt = env.get("CLOUD_RUN_TASK_ATTEMPT", "").strip()
    unique = str(uuid_factory())
    if execution or task_index or task_attempt:
        parts = ["cloud-run", execution or "execution", f"task-{task_index or 'unknown'}",
                 f"attempt-{task_attempt or 'unknown'}", unique]
        return "-".join(parts)
    return f"worker-{unique}"


class Config:
    RUN_MODE = os.getenv("RUN_MODE", "continuous").strip().lower()
    STATE_BACKEND = os.getenv("STATE_BACKEND", "local").strip().lower()
    FIRESTORE_PROJECT_ID = os.getenv("FIRESTORE_PROJECT_ID", "")
    FIRESTORE_DATABASE_ID = os.getenv("FIRESTORE_DATABASE_ID", "(default)")
    FIRESTORE_LEASE_TTL_SECONDS = int(os.getenv("FIRESTORE_LEASE_TTL_SECONDS", 900))
    WORKER_ID = build_worker_id()
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
    GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.8-flash")

    DRIVE_AUTH_MODE = os.getenv("DRIVE_AUTH_MODE", "local").strip().lower()
    DRIVE_OAUTH_CLIENT_FILE = os.getenv("DRIVE_OAUTH_CLIENT_FILE")
    DRIVE_OAUTH_TOKEN_FILE = os.getenv("DRIVE_OAUTH_TOKEN_FILE", "token.json")
    DRIVE_OAUTH_TOKEN_JSON = os.getenv("DRIVE_OAUTH_TOKEN_JSON")
    # Deprecated Drive-only alias, deliberately not read in cloud auth mode.
    GOOGLE_APPLICATION_CREDENTIALS = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
                                      if DRIVE_AUTH_MODE == "local" else None)

    # Backwards-compatible Primary folder pair. The existing environment variable
    # names remain authoritative for Primary so production can be upgraded without
    # moving any historical folders or jobs.
    DRIVE_PRIMARY_INPUT_FOLDER_ID = os.getenv("DRIVE_INPUT_FOLDER_ID")
    DRIVE_PRIMARY_OUTPUT_FOLDER_ID = os.getenv("DRIVE_OUTPUT_FOLDER_ID")
    DRIVE_INPUT_FOLDER_ID = DRIVE_PRIMARY_INPUT_FOLDER_ID
    DRIVE_OUTPUT_FOLDER_ID = DRIVE_PRIMARY_OUTPUT_FOLDER_ID

    # Optional second logical user. Both values must be configured together.
    DRIVE_SECONDARY_INPUT_FOLDER_ID = os.getenv("DRIVE_SECONDARY_INPUT_FOLDER_ID")
    DRIVE_SECONDARY_OUTPUT_FOLDER_ID = os.getenv("DRIVE_SECONDARY_OUTPUT_FOLDER_ID")

    # Per-process routing cache. Cloud Run executes this worker serially under
    # the existing global one-shot lease, so an active output route is safe.
    _DRIVE_FILE_ROUTES = {}

    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
    TELEGRAM_MAX_DOWNLOAD_BYTES = int(os.getenv("TELEGRAM_MAX_DOWNLOAD_BYTES", 20 * 1024 * 1024))

    POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", 60))
    CHUNK_SLEEP_SECONDS = int(os.getenv("CHUNK_SLEEP_SECONDS", 3))
    LOCAL_STATE_FILE = os.getenv("LOCAL_STATE_FILE", "processed_files.json")
    TRANSCRIPT_CACHE_DIR = os.getenv("TRANSCRIPT_CACHE_DIR", "transcript_cache")
    _default_temp_dir = (r"C:\temp\agente_transcriptor_bot" if os.name == "nt"
                         else os.path.join(tempfile.gettempdir(), "agente_transcriptor_bot"))
    TEMP_DIR = os.getenv("TEMP_DIR", _default_temp_dir)
    OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")

    MAX_FILE_RETRIES = int(os.getenv("MAX_FILE_RETRIES", 5))
    MAX_NETWORK_FILE_RETRIES = int(os.getenv("MAX_NETWORK_FILE_RETRIES", 0))
    FILE_RETRY_BASE_SECONDS = int(os.getenv("FILE_RETRY_BASE_SECONDS", 300))
    FILE_RETRY_MAX_SECONDS = int(os.getenv("FILE_RETRY_MAX_SECONDS", 3600))
    NETWORK_FILE_RETRY_BASE_SECONDS = int(os.getenv("NETWORK_FILE_RETRY_BASE_SECONDS", 30))
    NETWORK_FILE_RETRY_MAX_SECONDS = int(os.getenv("NETWORK_FILE_RETRY_MAX_SECONDS", 300))

    PDF_FONT_REGULAR = os.getenv("PDF_FONT_REGULAR")
    PDF_FONT_BOLD = os.getenv("PDF_FONT_BOLD")

    @classmethod
    def drive_folder_routes(cls):
        """Return configured Drive input/output routes for logical users."""
        primary_input = cls.DRIVE_PRIMARY_INPUT_FOLDER_ID or cls.DRIVE_INPUT_FOLDER_ID
        primary_output = cls.DRIVE_PRIMARY_OUTPUT_FOLDER_ID or cls.DRIVE_OUTPUT_FOLDER_ID
        routes = [{
            "owner_id": "primary",
            "input_folder_id": primary_input,
            "output_folder_id": primary_output,
        }]
        if cls.DRIVE_SECONDARY_INPUT_FOLDER_ID and cls.DRIVE_SECONDARY_OUTPUT_FOLDER_ID:
            routes.append({
                "owner_id": "secondary",
                "input_folder_id": cls.DRIVE_SECONDARY_INPUT_FOLDER_ID,
                "output_folder_id": cls.DRIVE_SECONDARY_OUTPUT_FOLDER_ID,
            })
        return routes

    @classmethod
    def register_drive_file_route(cls, file_id: str, *, owner_id: str,
                                  input_folder_id: str, output_folder_id: str):
        if not file_id:
            return
        cls._DRIVE_FILE_ROUTES[str(file_id)] = {
            "owner_id": owner_id,
            "input_folder_id": input_folder_id,
            "output_folder_id": output_folder_id,
        }

    @classmethod
    def activate_drive_file_route(cls, file_id: str):
        """Activate the output folder associated with a discovered Drive file."""
        route = cls._DRIVE_FILE_ROUTES.get(str(file_id))
        if route and route.get("output_folder_id"):
            cls.DRIVE_OUTPUT_FOLDER_ID = route["output_folder_id"]
            return route
        return None

    @classmethod
    def activate_default_drive_route(cls):
        """Reset output routing to Primary for Telegram/YouTube and legacy flows."""
        default_output = cls.DRIVE_PRIMARY_OUTPUT_FOLDER_ID or cls.DRIVE_OUTPUT_FOLDER_ID
        cls.DRIVE_OUTPUT_FOLDER_ID = default_output
        return default_output

    @classmethod
    def route_for_drive_file(cls, file_id: str):
        return cls._DRIVE_FILE_ROUTES.get(str(file_id))

    @classmethod
    def validate(cls):
        if cls.RUN_MODE not in ("continuous", "once"):
            raise Exception("RUN_MODE must be either 'continuous' or 'once'")
        if cls.STATE_BACKEND not in ("local", "firestore"):
            raise Exception("STATE_BACKEND must be either 'local' or 'firestore'")
        if cls.STATE_BACKEND == "firestore" and not cls.FIRESTORE_PROJECT_ID:
            raise Exception("FIRESTORE_PROJECT_ID is required when STATE_BACKEND=firestore")
        if cls.FIRESTORE_LEASE_TTL_SECONDS < 120:
            raise Exception("FIRESTORE_LEASE_TTL_SECONDS must be at least 120 seconds")
        if cls.DRIVE_AUTH_MODE not in DRIVE_AUTH_MODES:
            raise Exception("DRIVE_AUTH_MODE must be either 'local' or 'authorized_user_json'")
        missing = []
        if not cls.GEMINI_API_KEY: missing.append("GEMINI_API_KEY")
        if cls.DRIVE_AUTH_MODE == "local":
            if not cls.DRIVE_OAUTH_TOKEN_FILE:
                missing.append("DRIVE_OAUTH_TOKEN_FILE")
        elif not cls.DRIVE_OAUTH_TOKEN_JSON:
            missing.append("DRIVE_OAUTH_TOKEN_JSON")
        if not cls.DRIVE_PRIMARY_INPUT_FOLDER_ID and not cls.DRIVE_INPUT_FOLDER_ID:
            missing.append("DRIVE_INPUT_FOLDER_ID")
        if not cls.DRIVE_PRIMARY_OUTPUT_FOLDER_ID and not cls.DRIVE_OUTPUT_FOLDER_ID:
            missing.append("DRIVE_OUTPUT_FOLDER_ID")
        if bool(cls.DRIVE_SECONDARY_INPUT_FOLDER_ID) != bool(cls.DRIVE_SECONDARY_OUTPUT_FOLDER_ID):
            missing.append("DRIVE_SECONDARY_INPUT_FOLDER_ID + DRIVE_SECONDARY_OUTPUT_FOLDER_ID (configure both or neither)")
        if not cls.TELEGRAM_BOT_TOKEN: missing.append("TELEGRAM_BOT_TOKEN")
        if not cls.TELEGRAM_CHAT_ID: missing.append("TELEGRAM_CHAT_ID")

        if missing:
            raise Exception(f"Missing required environment variables: {', '.join(missing)}")
