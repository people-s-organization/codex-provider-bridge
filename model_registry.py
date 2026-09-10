import json
import os
import time
import tomllib
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv


DEFAULT_CODEX_MODELS_PATH = "~/.codex/models_cache.json"
DEFAULT_CODEX_CONFIG_PATH = "~/.codex/config.toml"
DEFAULT_CODEX_BASE_URL = "https://chatgpt.com"
CODEX_MODELS_PATH = "/backend-api/codex/models"
CHATGPT_MODELS_PATH = "/backend-api/models"
CODEX_USER_AGENT = "codex_cli_rs/0.0.0 (Codex Provider Bridge)"
FETCH_TIMEOUT_SECONDS = 15.0
FETCH_CACHE_TTL_SECONDS = 60.0

# Tool flags the ChatGPT web model listing uses to advertise media abilities. The Codex
# model listing carries no media information at all, so this is the only real source.
IMAGE_TOOL_FLAGS = ("image_gen_tool_enabled", "dalle_3")

# In-memory cache for the upstream model listing. Never seeded with static model names:
# it only ever holds what the Codex backend actually reported.
_fetch_cache: dict[str, Any] = {"at": 0.0, "ids": [], "error": None}

# In-memory cache for the ChatGPT web capability listing.
_capabilities_cache: dict[str, Any] = {"at": 0.0, "payload": None, "error": None}


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


def _models_cache_path() -> Path:
    return _expand_user_path(os.getenv("CHATGPT_MODELS_FILE"), DEFAULT_CODEX_MODELS_PATH)


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


def _load_codex_model_ids() -> list[str]:
    return _extract_model_ids(_load_models_cache())


def _load_codex_default_model() -> str | None:
    config_path = _expand_user_path(
        os.getenv("CHATGPT_CODEX_CONFIG_FILE"),
        DEFAULT_CODEX_CONFIG_PATH,
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


def _chatgpt_headers(accept: str = "application/json") -> dict[str, str]:
    headers = {
        "Accept": accept,
        "User-Agent": CODEX_USER_AGENT,
        "originator": "codex_cli_rs",
        "version": "0.0.0",
    }
    token = _chatgpt_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    account_id = str(os.getenv("CHATGPT_ACCOUNT_ID") or "").strip() or (_codex_account_id() or "")
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id
    return headers


def _fetch_codex_model_ids() -> list[str]:
    """Ask the Codex backend which models are actually available.

    Only called when no local cache and no explicit ``CHATGPT_MODELS`` are available.
    Failures are reported through :func:`model_source` instead of being papered over
    with a built-in list.
    """

    now = time.monotonic()
    fetched_at = float(_fetch_cache.get("at") or 0.0)
    if now - fetched_at < FETCH_CACHE_TTL_SECONDS:
        return list(_fetch_cache.get("ids") or [])

    url = _models_url()
    token = _chatgpt_token()
    model_ids: list[str] = []
    error: str | None = None

    if not url:
        error = "upstream lookup disabled by CHATGPT_MODELS_URL"
    elif not token:
        error = "no CHATGPT_ACCESS_TOKEN for upstream model lookup"
    else:
        params = {}
        client_version = str(_load_models_cache().get("client_version") or "").strip()
        if client_version:
            params["client_version"] = client_version

        try:
            with httpx.Client(timeout=FETCH_TIMEOUT_SECONDS) as client:
                response = client.get(url, headers=_chatgpt_headers(), params=params)
            if response.status_code >= 400:
                error = f"upstream responded HTTP {response.status_code}"
            else:
                model_ids = _extract_model_ids(response.json())
                if not model_ids:
                    error = "upstream returned no usable models"
        except (httpx.HTTPError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"

    _fetch_cache.update({"at": now, "ids": model_ids, "error": error})
    return list(model_ids)


def _codex_account_id() -> str | None:
    auth_file = os.path.expanduser("~/.codex/auth.json")
    try:
        with open(auth_file) as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None

    account_id = str((data.get("tokens") or {}).get("account_id") or "").strip()
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


def _fetch_capabilities_payload() -> tuple[Any, str | None]:
    now = time.monotonic()
    fetched_at = float(_capabilities_cache.get("at") or 0.0)
    if now - fetched_at < FETCH_CACHE_TTL_SECONDS:
        return _capabilities_cache.get("payload"), _capabilities_cache.get("error")

    url = _capabilities_url()
    payload: Any = None
    error: str | None = None

    if not url:
        error = "capability lookup disabled by CHATGPT_CAPABILITIES_URL"
    elif not _chatgpt_token():
        error = "no CHATGPT_ACCESS_TOKEN for capability lookup"
    else:
        try:
            with httpx.Client(timeout=FETCH_TIMEOUT_SECONDS) as client:
                response = client.get(url, headers=_chatgpt_headers())
            if response.status_code >= 400:
                error = f"upstream responded HTTP {response.status_code}"
            else:
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            error = f"{type(exc).__name__}: {exc}"

    _capabilities_cache.update({"at": now, "payload": payload, "error": error})
    return payload, error


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


def capabilities() -> dict[str, Any]:
    """What media abilities this account actually advertises.

    Media is a *capability*, not a listed model. Neither the Codex listing nor the
    ChatGPT web listing contains ``gpt-image-*`` / ``tts-*`` / ``gpt-realtime-*``
    entries, and the media-specific listing routes return 404, so there is no model id
    to discover. The ChatGPT web listing does flag the image tool per model, which is
    the one real signal available.
    """

    _load_env()
    payload, error = _fetch_capabilities_payload()
    image_models = _image_tool_model_ids(payload)
    return {
        "source": "chatgpt_models" if payload is not None else "none",
        "url": _capabilities_url(),
        "error": error,
        "image_generation": {
            "available": bool(image_models),
            "tool_models": image_models,
        },
        "realtime_speech": {
            "model_listing": False,
            "detail": (
                "The realtime API exposes no model listing endpoint; supply "
                "CHATGPT_REALTIME_MODEL or the request model."
            ),
        },
        "tool_calling": {
            "available": False,
            "scope": "bridge",
            "upstream_supports_function_tools": True,
            "detail": (
                "The Codex responses upstream accepts custom function tools and returns "
                "function_call items (verified), but this bridge neither forwards client "
                "tools nor emits tool_calls: text answers only for now."
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


def available_model_ids() -> list[str]:
    """Model ids the bridge will advertise.

    Resolution order is purely configuration- and upstream-driven:

    1. ``CHATGPT_MODELS`` when set (explicit override)
    2. whatever ``CHATGPT_MODELS_FILE`` (Codex's ``models_cache.json``) currently lists
    3. a live lookup against the Codex backend when the cache has nothing
    4. ``CHATGPT_EXTRA_MODELS`` appended

    There is deliberately no built-in fallback list: if no real source yields a model,
    the result is empty.
    """

    _load_env()

    configured_models = _split_model_list(os.getenv("CHATGPT_MODELS"))
    if configured_models:
        model_ids = configured_models
    else:
        model_ids = _load_codex_model_ids()
        if not model_ids:
            model_ids = _fetch_codex_model_ids()

    model_ids = _dedupe(model_ids + _split_model_list(os.getenv("CHATGPT_EXTRA_MODELS")))

    configured_default = str(os.getenv("CHATGPT_DEFAULT_MODEL") or "").strip()
    if configured_default:
        model_ids = _dedupe([configured_default] + model_ids)
    elif not configured_models:
        codex_default = _load_codex_default_model()
        if codex_default:
            model_ids = _dedupe([codex_default] + model_ids)

    return model_ids


def available_models() -> list[dict[str, Any]]:
    return [_model_object(model_id) for model_id in available_model_ids()]


def default_model_id() -> str | None:
    model_ids = available_model_ids()
    return model_ids[0] if model_ids else None


def model_snapshot(include_capabilities: bool = False) -> dict[str, Any]:
    """Models, default model and source in one call.

    The upstream lookups may block on the network, so async callers should run this in a
    thread pool instead of on the event loop. Capability probing is opt-in so cheap
    routes like ``/v1/models`` never pay for it.
    """

    model_ids = available_model_ids()
    snapshot: dict[str, Any] = {
        "model_ids": model_ids,
        "models": [_model_object(model_id) for model_id in model_ids],
        "default_model": model_ids[0] if model_ids else None,
        "source": model_source(),
    }
    if include_capabilities:
        snapshot["capabilities"] = capabilities()
    return snapshot


def model_source() -> dict[str, Any]:
    """Where the current model list came from, for diagnostics."""

    _load_env()

    cache_path = str(_models_cache_path())
    if _split_model_list(os.getenv("CHATGPT_MODELS")):
        return {"source": "CHATGPT_MODELS", "cache_file": cache_path, "error": None}

    if _load_codex_model_ids():
        return {"source": "models_cache", "cache_file": cache_path, "error": None}

    fetched = _fetch_codex_model_ids()
    return {
        "source": "upstream" if fetched else "none",
        "cache_file": cache_path,
        "error": _fetch_cache.get("error"),
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
