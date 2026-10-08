import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import requests
import yt_dlp

from config import Config
from utils import (
    calculate_backoff,
    is_transient_error,
    log_error,
    log_ok,
    log_start,
    log_warn,
    redact_secrets,
    sanitize_filename,
)

PERMANENT_YOUTUBE_ERRORS = [
    "video unavailable",
    "private video",
    "removed by the user",
    "is not a valid url",
    "invalid url",
    "sign in to confirm your age",
    "copyright claim",
    "this video has been removed",
    "playlist",
]

_YOUTUBE_VIDEO_ID_PATTERNS = (
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{6,})", re.IGNORECASE),
    re.compile(r"[?&]v=([A-Za-z0-9_-]{6,})", re.IGNORECASE),
    re.compile(r"youtube\.com/(?:shorts|embed)/([A-Za-z0-9_-]{6,})", re.IGNORECASE),
)


def _deno_path() -> str:
    return os.environ.get("YT_DLP_DENO_PATH", "/usr/local/bin/deno")


def _bgutil_server_home() -> str:
    return os.environ.get("YT_DLP_BGUTIL_SERVER_HOME", "/opt/bgutil-provider")


def _bgutil_provider_available() -> bool:
    return os.path.isfile(os.path.join(_bgutil_server_home(), "src", "generate_once.ts"))


def _wpc_browser_path() -> str:
    return os.environ.get("YT_DLP_WPC_BROWSER_PATH", "/usr/bin/chromium")


def _wpc_provider_available() -> bool:
    return os.path.isfile(_wpc_browser_path()) and shutil.which("xvfb-run") is not None


def _youtube_js_runtime_options() -> dict:
    """Pin yt-dlp's embedded Python API to the Deno binary in our container."""
    return {
        "js_runtimes": {
            "deno": {
                "path": _deno_path(),
            }
        }
    }


def _youtube_pot_extractor_options(player_client: str = "mweb") -> dict:
    """Configure yt-dlp's mweb + bundled BgUtil PO-token provider path."""
    return {
        "extractor_args": {
            "youtube": {
                "player_client": [player_client],
            },
            "youtubepot-bgutilscript": {
                "server_home": [_bgutil_server_home()],
            },
        }
    }


def _extract_video_id_from_url(url: str) -> str | None:
    for pattern in _YOUTUBE_VIDEO_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def _classify_youtube_error(error: Exception) -> str:
    text = str(error).lower()
    if "sign in to confirm" in text or "login_required" in text or "not a bot" in text:
        return "anti_bot_or_login_required"
    if "po token" in text or ("pot" in text and "token" in text):
        return "po_token"
    if "http error 403" in text or "status code 403" in text:
        return "http_403"
    if "http error 429" in text or "too many requests" in text:
        return "rate_limited"
    if "video unavailable" in text or "this video is unavailable" in text:
        return "video_unavailable"
    if "javascript" in text or "js runtime" in text or "deno" in text:
        return "javascript_runtime"
    if "chrome" in text or "chromium" in text or "xvfb" in text or "browser" in text:
        return "browser_runtime"
    if is_transient_error(error):
        return "transient_network_or_service"
    return "other"


def _log_route_failure(route: str, error: Exception) -> None:
    classification = _classify_youtube_error(error)
    log_warn(
        "youtube_route_failed",
        f"route={route} class={classification} error={redact_secrets(str(error))}",
    )


def is_transient_youtube_error(error: Exception) -> bool:
    err_str = str(error).lower()
    for perm in PERMANENT_YOUTUBE_ERRORS:
        if perm in err_str:
            return False
    return is_transient_error(error)


def _metadata_with_python_api(url: str) -> tuple[str, str]:
    ydl_opts = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        **_youtube_js_runtime_options(),
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise RuntimeError("yt-dlp Python API returned empty metadata")

    info_dict = dict(info) if hasattr(info, "items") else info
    entry_type = info_dict.get("_type", "video")
    if entry_type in ("playlist", "multi_video") or "entries" in info_dict:
        raise ValueError("Playlists are not supported. Please provide a link to a single YouTube video.")

    title = info_dict.get("title") or "YouTube_Video"
    video_id = info_dict.get("id") or _extract_video_id_from_url(url)
    if not video_id:
        video_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return title, video_id


def _yt_dlp_cli_base() -> list[str]:
    return [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--no-warnings",
        "--js-runtimes",
        f"deno:{_deno_path()}",
    ]


def _runtime_cwd() -> str | None:
    """Use /app inside the production image, but never assume it exists elsewhere."""
    if os.path.isdir("/app") and os.access("/app", os.R_OK | os.X_OK):
        return "/app"
    return None


def _subprocess_env() -> dict[str, str]:
    """Build a writable Deno/browser environment for both Cloud Run and tests/tools."""
    env = os.environ.copy()
    preferred = env.get("DENO_DIR", "/app/.deno_cache")
    try:
        os.makedirs(preferred, exist_ok=True)
        if not os.access(preferred, os.W_OK | os.X_OK):
            raise OSError(f"DENO_DIR is not writable: {preferred}")
        deno_dir = preferred
    except OSError:
        deno_dir = os.path.join(tempfile.gettempdir(), "yt-dlp-deno-cache")
        os.makedirs(deno_dir, exist_ok=True)
    env["DENO_DIR"] = deno_dir
    env.setdefault("HOME", tempfile.gettempdir())
    return env


def _metadata_with_cli(url: str) -> tuple[str, str]:
    command = _yt_dlp_cli_base() + [
        "--skip-download",
        "--print",
        "%(id)s\t%(title)s",
        url,
    ]
    result = subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
        cwd=_runtime_cwd(),
        env=_subprocess_env(),
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines or "\t" not in lines[-1]:
        stderr = result.stderr.strip()
        raise RuntimeError(f"yt-dlp CLI metadata output was invalid: {stderr or 'empty output'}")
    video_id, title = lines[-1].split("\t", 1)
    return title or "YouTube_Video", video_id


def _metadata_with_oembed(url: str) -> tuple[str, str]:
    """Last-resort public metadata path that does not depend on Innertube/player extraction."""
    response = requests.get(
        "https://www.youtube.com/oembed",
        params={"url": url, "format": "json"},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    title = payload.get("title") or "YouTube_Video"
    video_id = _extract_video_id_from_url(url)
    if not video_id:
        video_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return title, video_id


def get_youtube_metadata(url: str) -> tuple[str, str]:
    """Extract title/video ID using independent routes suitable for Cloud Run."""
    log_start(f"Extracting metadata for {url}")
    last_err = None

    routes = (
        ("python_api", _metadata_with_python_api),
        ("cli", _metadata_with_cli),
        ("oembed", _metadata_with_oembed),
    )

    for route_name, route in routes:
        try:
            title, video_id = route(url)
            log_ok(f"Metadata obtained via {route_name}: ID '{video_id}', Title '{title}'")
            return title, video_id
        except ValueError:
            raise
        except Exception as error:
            last_err = error
            _log_route_failure(route_name, error)

    raise last_err or RuntimeError("Could not extract YouTube metadata through any route")


def _cleanup_download_candidates(clean_stem: str) -> None:
    pattern = os.path.join(Config.TEMP_DIR, f"{clean_stem}.*")
    for path in glob.glob(pattern):
        try:
            os.remove(path)
        except OSError:
            pass


def _download_with_python_api(
    url: str,
    clean_stem: str,
    player_client: str | None = None,
    use_pot: bool = False,
) -> str:
    target_local_path = os.path.join(Config.TEMP_DIR, f"{clean_stem}.mp3")
    ydl_opts_download = {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "outtmpl": os.path.join(Config.TEMP_DIR, f"{clean_stem}.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        **_youtube_js_runtime_options(),
    }
    if use_pot:
        ydl_opts_download.update(_youtube_pot_extractor_options(player_client or "mweb"))
    elif player_client:
        ydl_opts_download["extractor_args"] = {
            "youtube": {
                "player_client": [player_client],
            }
        }

    with yt_dlp.YoutubeDL(ydl_opts_download) as ydl_down:
        error_code = ydl_down.download([url])
        if error_code != 0:
            raise RuntimeError(f"yt-dlp Python API returned error code {error_code}")
    if not os.path.isfile(target_local_path) or os.path.getsize(target_local_path) <= 0:
        raise RuntimeError("yt-dlp Python API did not create a non-empty MP3")
    return target_local_path


def _download_with_cli(
    url: str,
    clean_stem: str,
    player_client: str | None = None,
    use_pot: bool = False,
) -> str:
    target_local_path = os.path.join(Config.TEMP_DIR, f"{clean_stem}.mp3")
    command = _yt_dlp_cli_base()
    if use_pot:
        command += [
            "--extractor-args",
            f"youtube:player_client={player_client or 'mweb'}",
            "--extractor-args",
            f"youtubepot-bgutilscript:server_home={_bgutil_server_home()}",
        ]
    elif player_client:
        command += [
            "--extractor-args",
            f"youtube:player_client={player_client}",
        ]
    command += [
        "--format",
        "bestaudio/best",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "192K",
        "--output",
        os.path.join(Config.TEMP_DIR, f"{clean_stem}.%(ext)s"),
        url,
    ]
    result = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=300,
        cwd=_runtime_cwd(),
        env=_subprocess_env(),
    )
    if result.returncode != 0:
        error_text = (result.stderr or result.stdout or "yt-dlp CLI failed").strip()
        raise RuntimeError(error_text[-4000:])
    if not os.path.isfile(target_local_path) or os.path.getsize(target_local_path) <= 0:
        raise RuntimeError("yt-dlp CLI did not create a non-empty MP3")
    return target_local_path


def _download_with_wpc_cli(url: str, clean_stem: str) -> str:
    """Use the browser-backed WPC provider under Xvfb as an independent PO-token route."""
    if not _wpc_provider_available():
        raise RuntimeError("WPC browser runtime is not available")

    target_local_path = os.path.join(Config.TEMP_DIR, f"{clean_stem}.mp3")
    command = [
        "xvfb-run",
        "-a",
        "-s",
        "-screen 0 1280x1024x24 -nolisten tcp",
        *_yt_dlp_cli_base(),
        "--extractor-args",
        "youtube:player_client=mweb",
        "--extractor-args",
        f"youtubepot-wpc:browser_path={_wpc_browser_path()}",
        "--format",
        "bestaudio/best",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "192K",
        "--output",
        os.path.join(Config.TEMP_DIR, f"{clean_stem}.%(ext)s"),
        url,
    ]
    result = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=360,
        cwd=_runtime_cwd(),
        env=_subprocess_env(),
    )
    if result.returncode != 0:
        error_text = (result.stderr or result.stdout or "yt-dlp WPC route failed").strip()
        raise RuntimeError(error_text[-6000:])
    if not os.path.isfile(target_local_path) or os.path.getsize(target_local_path) <= 0:
        raise RuntimeError("yt-dlp WPC route did not create a non-empty MP3")
    return target_local_path


def download_youtube_audio(url: str, title: str, video_id: str) -> tuple[str, str]:
    """Download audio with browser-backed PO-token, BgUtil, API, CLI, and embedded fallbacks."""
    os.makedirs(Config.TEMP_DIR, exist_ok=True)
    suffix = video_id[:8] if video_id else ""
    file_name = sanitize_filename(f"{title}.mp3", fallback="youtube_audio.mp3", suffix=suffix)
    clean_stem = os.path.splitext(file_name)[0]
    target_local_path = os.path.join(Config.TEMP_DIR, file_name)

    log_start(f"Downloading audio: {video_id} -> {file_name}")
    last_err = None
    routes = []

    if _wpc_provider_available():
        log_ok(f"YouTube browser-backed PO-token route available with {_wpc_browser_path()}")
        routes.append(("cli_mweb_wpc", lambda: _download_with_wpc_cli(url, clean_stem)))
    else:
        log_warn(
            "youtube_wpc_provider_unavailable",
            f"Chromium/Xvfb unavailable for WPC route (browser={_wpc_browser_path()})",
        )

    if _bgutil_provider_available():
        log_ok(f"YouTube BgUtil PO-token provider available at {_bgutil_server_home()}")
        routes.extend(
            [
                (
                    "python_api_mweb_pot",
                    lambda: _download_with_python_api(url, clean_stem, "mweb", use_pot=True),
                ),
                (
                    "cli_mweb_pot",
                    lambda: _download_with_cli(url, clean_stem, "mweb", use_pot=True),
                ),
            ]
        )
    else:
        log_warn(
            "youtube_pot_provider_unavailable",
            f"BgUtil provider not found at {_bgutil_server_home()}; continuing with legacy fallbacks",
        )

    routes.extend(
        [
            ("python_api", lambda: _download_with_python_api(url, clean_stem)),
            ("cli_default", lambda: _download_with_cli(url, clean_stem)),
            ("cli_web_safari", lambda: _download_with_cli(url, clean_stem, "web_safari")),
            ("cli_web_embedded", lambda: _download_with_cli(url, clean_stem, "web_embedded")),
        ]
    )

    for route_name, route in routes:
        _cleanup_download_candidates(clean_stem)
        try:
            produced_path = route()
            if produced_path != target_local_path and os.path.isfile(produced_path):
                os.replace(produced_path, target_local_path)
            if not os.path.isfile(target_local_path) or os.path.getsize(target_local_path) <= 0:
                raise RuntimeError(f"route {route_name} did not produce the expected MP3")
            log_ok(f"Audio downloaded and converted to MP3 via {route_name}")
            return target_local_path, file_name
        except Exception as error:
            last_err = error
            _log_route_failure(route_name, error)

    raise last_err or RuntimeError("Could not download YouTube audio through any route")
