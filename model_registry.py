import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import tomllib
from functools import lru_cache
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv


DEFAULT_CODEX_MODELS_PATH = "~/.codex/models_cache.json"
DEFAULT_CODEX_CONFIG_PATH = "~/.codex/config.toml"
DEFAULT_CODEX_BASE_URL = "https://chatgpt.com"
CODEX_MODELS_PATH = "/backend-api/codex/models"
CHATGPT_MODELS_PATH = "/backend-api/models"
CODEX_USER_AGENT = "Codex Provider Bridge"
FETCH_TIMEOUT_SECONDS = 15.0
FETCH_CACHE_TTL_SECONDS = 60.0

# Tool flags the ChatGPT web model listing uses to advertise media abilities. The Codex
# model listing carries no media information at all, so this is the only real source.
IMAGE_TOOL_FLAGS = ("image_gen_tool_enabled", "dalle_3")

# In-memory cache for the upstream model listing. Never seeded with static model names:
# it only ever holds what the Codex backend actually reported.
_fetch_cache: dict[tuple, dict[str, Any]] = {}

# Never share account-specific responses across credentials, endpoints or versions.
_capabilities_cache: dict[tuple, dict[str, Any]] = {}
_cache_lock = threading.Lock()
_inflight: dict[tuple, Future] = {}


def _cached_fetch(cache, key, fetch, refresh=False):
    flight_key = (id(cache), key)
    with _cache_lock:
        future = _inflight.get(flight_key)
        entry = cache.get(key)
        if future is None and not refresh and entry is not None:
            if time.monotonic() - entry["at"] < FETCH_CACHE_TTL_SECONDS:
                return entry["result"]
        owner = future is None
        if owner:
            future = Future()
            _inflight[flight_key] = future
    if not owner:
        return future.result()
    try:
        result = fetch()
        with _cache_lock:
            cache[key] = {"at": time.monotonic(), "result": result}
            # Bound the cache across credential rotations without retaining tokens.
            while len(cache) > 128:
                del cache[next(iter(cache))]
        future.set_result(result)
        return result
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        with _cache_lock:
            _inflight.pop(flight_key, None)


def _request_key(url, headers, version=None):
    credential = headers.get("Authorization", "")
    return (url, headers.get("ChatGPT-Account-ID", ""),
            hashlib.sha256(credential.encode()).hexdigest(), version)



def _load_env() -> None:
    load_dotenv(override=False)


def _expand_user_path(path_value: str | None, default_path: str) -> Path:
    raw_path = (path_value or default_path).strip()
    return Path(raw_path).expanduser()


def _split_model_list(value: str | None) -> list[str]:
    if not value:
        return []

    if value.strip().startswith("["):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            pass

    normalized = value.replace("\n", ",").replace(";", ",")
    return [part.strip() for part in normalized.split(",") if part.strip()]


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _codex_home() -> Path:
    return _expand_user_path(os.getenv("CODEX_HOME"), "~/.codex")


def _models_cache_path() -> Path:
    return _expand_user_path(os.getenv("CHATGPT_MODELS_FILE"), str(_codex_home() / "models_cache.json"))


@lru_cache(maxsize=8)
def _installed_codex_version(executable: str, modified_at: int) -> str | None:
    try:
        result = subprocess.run([executable, "--version"], capture_output=True,
                                text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(?:codex-cli|codex)\s+(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", result.stdout)
    return match.group(1) if match else None


def _client_version() -> str | None:
    # A real cache version is valid evidence; latest_version in version.json is
    # an update notification, NOT the installed client version.
    configured = str(os.getenv("CHATGPT_CLIENT_VERSION") or "").strip()
    cached = str(_load_models_cache().get("client_version") or "").strip()
    if configured or cached:
        return configured or cached
    configured_executable = str(os.getenv("CHATGPT_CODEX_EXECUTABLE") or "").strip()
    candidates = ([str(Path(configured_executable).expanduser())] if configured_executable else
                  [shutil.which("codex"), str(Path.home() / ".local/bin/codex")])
    for executable in candidates:
        if not executable:
            continue
        try:
            version = _installed_codex_version(executable, Path(executable).stat().st_mtime_ns)
            if version:
                return version
        except OSError:
            pass
    return None


def _load_models_cache() -> dict[str, Any]:
    try:
        data = json.loads(_models_cache_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _extract_model_ids(payload: Any) -> list[str]:
    """Pull usable model ids out of a Codex model listing.

    Accepts the ``{"models": [...]}`` shape used by ``models_cache.json`` as well as a
    bare list or an OpenAI-style ``{"data": [...]}`` envelope. Models flagged as hidden
    or as unsupported in the API are skipped; nothing is invented.
    """

    entries: list[Any] | None = None
    if isinstance(payload, dict):
        for key in ("models", "data"):
            if isinstance(payload.get(key), list):
                entries = payload[key]
                break
    elif isinstance(payload, list):
        entries = payload

    if entries is None:
        return []

    model_ids: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            model_id = entry.strip()
            if model_id:
                model_ids.append(model_id)
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("visibility") not in {None, "list"}:
            continue
        if entry.get("supported_in_api") is False:
            continue

        model_id = str(entry.get("slug") or entry.get("id") or "").strip()
        if model_id:
            model_ids.append(model_id)

    return _dedupe(model_ids)


def _local_cache_fresh() -> bool:
    """Codex owns this file; use its write time rather than trust payload timestamps."""
    try:
        age = time.time() - _models_cache_path().stat().st_mtime
    except OSError:
        return False
    return 0 <= age < FETCH_CACHE_TTL_SECONDS


def _load_codex_model_ids() -> list[str]:
    # Expired disk data is not proof that this account still has these models.
    return _extract_model_ids(_load_models_cache()) if _local_cache_fresh() else []


def _load_codex_default_model() -> str | None:
    config_path = _expand_user_path(
        os.getenv("CHATGPT_CODEX_CONFIG_FILE"),
        str(_codex_home() / "config.toml"),
    )
    try:
        data = tomllib.loads(config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None

    model = str(data.get("model") or "").strip()
    return model or None


def _models_url() -> str | None:
    """Upstream model listing URL, or ``None`` when fetching is switched off.

    Setting ``CHATGPT_MODELS_URL`` to an empty value disables the upstream lookup.
    """

    configured = os.getenv("CHATGPT_MODELS_URL")
    if configured is not None and not configured.strip():
        return None
    if configured and configured.strip():
        return configured.strip()

    base_url = str(os.getenv("CHATGPT_BASE_URL") or DEFAULT_CODEX_BASE_URL).rstrip("/")
    return f"{base_url}{CODEX_MODELS_PATH}"


def _chatgpt_token() -> str:
    return str(os.getenv("CHATGPT_ACCESS_TOKEN") or "").strip()


def _chatgpt_headers(accept: str = "application/json", client_version: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": accept,
        "User-Agent": CODEX_USER_AGENT,
        "originator": "codex_cli_rs",
    }
    if client_version:
        headers["version"] = client_version
        headers["User-Agent"] = f"codex_cli_rs/{client_version} (Codex Provider Bridge)"
    token = _chatgpt_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    account_id = str(os.getenv("CHATGPT_ACCOUNT_ID") or "").strip() or (_codex_account_id() or "")
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    return headers


def _fetch_codex_result(refresh: bool = False) -> tuple[list[str], str | None]:
    url = _models_url()
    version = _client_version()
    headers = _chatgpt_headers(client_version=version)

    def fetch():
        if not url:
            return [], "upstream lookup disabled by CHATGPT_MODELS_URL"
        if "Authorization" not in headers:
            return [], "no CHATGPT_ACCESS_TOKEN for upstream model lookup"
        if not version:
            return [], ("cannot discover installed Codex client_version; install Codex, "
                        "provide its models cache, or set CHATGPT_CLIENT_VERSION")
        try:
            with httpx.Client(timeout=FETCH_TIMEOUT_SECONDS) as client:
                response = client.get(url, headers=headers, params={"client_version": version})
            if response.status_code >= 400:
                return [], f"upstream responded HTTP {response.status_code}"
            ids = _extract_model_ids(response.json())
            return ids, None if ids else "upstream returned no usable models"
        except (httpx.HTTPError, ValueError) as exc:
            # Exception messages may embed URLs/credentials. Keep diagnostics safe.
            return [], f"upstream model lookup failed: {type(exc).__name__}"

    ids, error = _cached_fetch(_fetch_cache, _request_key(url, headers, version), fetch, refresh)
    return list(ids), error


def _fetch_codex_model_ids(refresh: bool = False) -> list[str]:
    return _fetch_codex_result(refresh)[0]


def _codex_account_id() -> str | None:
    auth_file = _codex_home() / "auth.json"
    try:
        with open(auth_file) as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict) or not isinstance(data.get("tokens"), dict):
        return None
    account_id = str(data["tokens"].get("account_id") or "").strip()
    return account_id or None


def _capabilities_url() -> str | None:
    """ChatGPT web model listing, or ``None`` when the capability lookup is off."""

    configured = os.getenv("CHATGPT_CAPABILITIES_URL")
    if configured is not None and not configured.strip():
        return None
    if configured and configured.strip():
        return configured.strip()

    base_url = str(os.getenv("CHATGPT_BASE_URL") or DEFAULT_CODEX_BASE_URL).rstrip("/")
    return f"{base_url}{CHATGPT_MODELS_PATH}"


def _fetch_capabilities_payload(refresh: bool = False) -> tuple[Any, str | None]:
    url = _capabilities_url()
    headers = _chatgpt_headers()

    def fetch():
        if not url:
            return None, "capability lookup disabled by CHATGPT_CAPABILITIES_URL"
        if "Authorization" not in headers:
            return None, "no CHATGPT_ACCESS_TOKEN for capability lookup"
        try:
            with httpx.Client(timeout=FETCH_TIMEOUT_SECONDS) as client:
                response = client.get(url, headers=headers)
            if response.status_code >= 400:
                return None, f"upstream responded HTTP {response.status_code}"
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
                return None, "upstream returned an invalid capability listing"
            return payload, None
        except (httpx.HTTPError, ValueError) as exc:
            return None, f"capability lookup failed: {type(exc).__name__}"

    return _cached_fetch(_capabilities_cache, _request_key(url, headers), fetch, refresh)


def _image_tool_model_ids(payload: Any) -> list[str]:
    """Model slugs whose ``enabled_tools`` advertise an image generation tool."""

    if not isinstance(payload, dict):
        return []
    entries = payload.get("models")
    if not isinstance(entries, list):
        return []

    model_ids: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        tools = entry.get("enabled_tools")
        if not isinstance(tools, list):
            continue
        if not any(str(tool) in IMAGE_TOOL_FLAGS for tool in tools):
            continue
        model_id = str(entry.get("slug") or entry.get("id") or "").strip()
        if model_id:
            model_ids.append(model_id)

    return _dedupe(model_ids)


def capabilities(refresh: bool = False) -> dict[str, Any]:
    """What media abilities this account actually advertises.

    Image tool flags identify host models, not the underlying image generator.
    Missing or failed evidence remains unknown; it is not a negative capability
    assertion. This registry does not probe realtime model identities.
    """

    _load_env()
    payload, error = _fetch_capabilities_payload(refresh=refresh)
    image_models = _image_tool_model_ids(payload)
    entries = payload.get("models", []) if isinstance(payload, dict) else []
    known = bool(entries) and all(
        isinstance(entry, dict) and isinstance(entry.get("enabled_tools"), list)
        for entry in entries
    )
    image_available = True if image_models else (False if known and not error else None)
    return {
        "source": "chatgpt_models" if payload is not None else "none",
        "url": _capabilities_url(),
        "error": error,
        "image_generation": {
            "available": image_available,
            "status": "available" if image_available else ("unsupported" if image_available is False else "unknown"),
            "tool_models": image_models,
            "detail": "Tool-host model IDs, not image generation model identities.",
        },
        "realtime_speech": {
            "model_listing": False,
            "available": None,
            "status": "unknown",
            "detail": (
                "This registry has no realtime model discovery source. Configured or "
                "requested realtime models are unverified, not discovered capabilities."
            ),
        },
        "tool_calling": {
            "available": True,
            "scope": "bridge",
            "upstream_supports_function_tools": None,
            "detail": (
                "Function tools are forwarded to the Codex responses channel and streamed "
                "function_call items are converted into OpenAI tool_calls / function_call "
                "output. Non-function tool types (e.g. web_search) are dropped."
            ),
        },
    }


def is_available_model(model_id: str) -> bool:
    return model_id in available_model_ids()


def _model_object(model_id: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "openai",
    }


def available_model_ids(refresh: bool = False) -> list[str]:
    """Model ids the bridge will advertise.

    Resolution order is purely configuration- and upstream-driven:

    1. ``CHATGPT_MODELS`` when set (explicit override)
    2. ``CHATGPT_MODELS_FILE`` (Codex's ``models_cache.json``) while its mtime is
       younger than FETCH_CACHE_TTL_SECONDS; this local source has no account proof
    3. a live lookup against the Codex backend when the cache is empty or expired
    4. ``CHATGPT_EXTRA_MODELS`` appended

    There is deliberately no built-in fallback list: if no real source yields a model,
    the result is empty. Defaults only reorder discovered IDs. ``refresh=True``
    bypasses both the disk listing and the live TTL (but not explicit overrides),
    without overwriting Codex's disk cache; failures remain visible, not stale success.
    """

    _load_env()

    configured_models = _split_model_list(os.getenv("CHATGPT_MODELS"))
    if configured_models:
        model_ids = configured_models
    else:
        model_ids = [] if refresh else _load_codex_model_ids()
        if not model_ids:
            model_ids = _fetch_codex_model_ids(refresh=refresh)

    model_ids = _dedupe(model_ids + _split_model_list(os.getenv("CHATGPT_EXTRA_MODELS")))

    configured_default = str(os.getenv("CHATGPT_DEFAULT_MODEL") or "").strip()
    preferred = configured_default or (None if configured_models else _load_codex_default_model())
    if preferred in model_ids:
        model_ids = _dedupe([preferred] + model_ids)

    return model_ids


def available_models() -> list[dict[str, Any]]:
    return [_model_object(model_id) for model_id in available_model_ids()]


def default_model_id() -> str | None:
    model_ids = available_model_ids()
    return model_ids[0] if model_ids else None


def model_snapshot(include_capabilities: bool = False, refresh: bool = False) -> dict[str, Any]:
    """Models, default model and source in one call.

    The upstream lookups may block on the network, so async callers should run this in a
    thread pool instead of on the event loop. Capability probing is opt-in so cheap
    routes like ``/v1/models`` never pay for it.
    """

    model_ids = available_model_ids(refresh=refresh)
    snapshot: dict[str, Any] = {
        "model_ids": model_ids,
        "models": [_model_object(model_id) for model_id in model_ids],
        "default_model": model_ids[0] if model_ids else None,
        "source": model_source(use_local_cache=not refresh),
    }
    if include_capabilities:
        snapshot["capabilities"] = capabilities(refresh=refresh)
    return snapshot


def model_source(use_local_cache: bool = True) -> dict[str, Any]:
    """Where the current model list came from, for diagnostics."""

    _load_env()

    cache_path = str(_models_cache_path())
    if _split_model_list(os.getenv("CHATGPT_MODELS")):
        return {"source": "CHATGPT_MODELS", "cache_file": cache_path, "error": None}

    if use_local_cache and _load_codex_model_ids():
        return {"source": "models_cache", "cache_file": cache_path, "error": None}

    fetched, error = _fetch_codex_result()
    stale = bool(_extract_model_ids(_load_models_cache())) and not _local_cache_fresh()
    return {
        "source": "upstream" if fetched else "none",
        "cache_file": cache_path,
        "cache_stale": stale,
        "status": "available" if fetched else "unknown",
        "error": error,
    }


def _parse_alias_map(value: str | None) -> dict[str, str]:
    if not value:
        return {}

    stripped = value.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return {
                    str(source).strip(): str(target).strip()
                    for source, target in parsed.items()
                    if str(source).strip() and str(target).strip()
                }
        except json.JSONDecodeError:
            pass

    aliases = {}
    for item in stripped.replace("\n", ",").replace(";", ",").split(","):
        if "=" in item:
            source, target = item.split("=", 1)
        elif ":" in item:
            source, target = item.split(":", 1)
        else:
            continue

        source = source.strip()
        target = target.strip()
        if source and target:
            aliases[source] = target
    return aliases


def model_aliases() -> dict[str, str]:
    """Alias map. Only configuration provides aliases; none are built in."""

    _load_env()
    return {
        **_parse_alias_map(os.getenv("CHATGPT_MODEL_ALIASES")),
        **_parse_alias_map(os.getenv("CHATGPT_EXTRA_MODEL_ALIASES")),
    }


def resolve_model_name(model_name: str) -> str:
    return model_aliases().get(model_name, model_name)
