import json
import os
import tomllib
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


FALLBACK_MODEL_IDS = [
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
    "gpt-5.2",
    "gpt-5.2-codex",
    "gpt-4.1",
    "gpt-4.1-mini",
]
MEDIA_MODEL_IDS = [
    "gpt-image-2",
    "gpt-image-1.5",
    "gpt-image-1",
    "gpt-image-1-mini",
    "gpt-realtime-1.5",
    "gpt-4o-mini-tts",
    "tts-1",
    "tts-1-hd",
]


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


def _load_codex_model_ids() -> list[str]:
    cache_path = _expand_user_path(
        os.getenv("CHATGPT_MODELS_FILE"),
        "~/.codex/models_cache.json",
    )
    try:
        data = json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        return []

    model_ids = []
    for model in data.get("models", []):
        if not isinstance(model, dict):
            continue
        if model.get("visibility") not in {None, "list"}:
            continue
        if model.get("supported_in_api") is False:
            continue

        model_id = str(model.get("slug") or model.get("id") or "").strip()
        if model_id:
            model_ids.append(model_id)

    return _dedupe(model_ids)


def _load_codex_default_model() -> str | None:
    config_path = _expand_user_path(
        os.getenv("CHATGPT_CODEX_CONFIG_FILE"),
        "~/.codex/config.toml",
    )
    try:
        data = tomllib.loads(config_path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None

    model = str(data.get("model") or "").strip()
    return model or None


def _model_object(model_id: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "openai",
    }


def available_model_ids() -> list[str]:
    _load_env()

    configured_models = _split_model_list(os.getenv("CHATGPT_MODELS"))
    if configured_models:
        model_ids = configured_models
    else:
        model_ids = _dedupe((_load_codex_model_ids() or FALLBACK_MODEL_IDS) + MEDIA_MODEL_IDS)

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


def default_model_id() -> str:
    model_ids = available_model_ids()
    return model_ids[0] if model_ids else FALLBACK_MODEL_IDS[0]


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


def _default_model_aliases() -> dict[str, str]:
    model_ids = available_model_ids()
    if not model_ids:
        return {}

    aliases = {"gpt-4.1": model_ids[0]}
    mini_target = next(
        (
            model_id
            for model_id in model_ids
            if "mini" in model_id and model_id != "gpt-4.1-mini"
        ),
        None,
    )
    if mini_target:
        aliases["gpt-4.1-mini"] = mini_target

    return aliases


def model_aliases() -> dict[str, str]:
    _load_env()

    configured_aliases = os.getenv("CHATGPT_MODEL_ALIASES")
    if configured_aliases:
        return _parse_alias_map(configured_aliases)

    return {
        **_default_model_aliases(),
        **_parse_alias_map(os.getenv("CHATGPT_EXTRA_MODEL_ALIASES")),
    }


def resolve_model_name(model_name: str) -> str:
    return model_aliases().get(model_name, model_name)
