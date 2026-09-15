# Codex Provider Bridge

**English** | [简体中文](README.zh-CN.md)

Use your ChatGPT / Codex login through a local OpenAI-compatible API for DeepSeek Harness (DSH), OpenClaw, Hermes, and other agents.

Supports text, streaming, function calls, image input, and model-specific reasoning effort. This is a compatibility bridge, not a complete replacement for the OpenAI API.

## Quick start

Requires Python 3 with `venv` and a ChatGPT / Codex account with access to the requested model.

```bash
git clone https://github.com/people-s-organization/codex-provider-bridge.git
cd codex-provider-bridge
bash start.sh
```

The script creates a virtual environment, installs dependencies, and creates `.env` if needed. It uses `CHATGPT_ACCESS_TOKEN` or your existing `~/.codex/auth.json`; without credentials, it prompts you to log in.

- **Server / SSH:** `bash start.sh --auth device` — open the displayed authorization link on your own computer.
- **Browser login:** `INSTALL_PLAYWRIGHT_BROWSER=1 bash start.sh --auth browser`.

The default address is **http://127.0.0.1:8000**. Use the actual port printed at startup if it changes. The homepage provides test forms; `/health` reports service status.

## Connect your client

| Setting | Value |
|---|---|
| API type | OpenAI-compatible Chat Completions or Responses |
| Base URL | `http://127.0.0.1:8000/v1` (adjust host/port) |
| API key | Your `BRIDGE_API_KEY`; a placeholder is fine only when bridge authentication is disabled |
| Model | An actual ID returned by `/v1/models` |

**Do not give clients your ChatGPT access token.** Before exposing the bridge beyond localhost, set `BRIDGE_API_KEY` in `.env`; use TLS for remote access.

```bash
# Set BRIDGE_API_KEY in this shell to the bridge key, if authentication is enabled.
curl http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer ${BRIDGE_API_KEY:-unused}"

curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer ${BRIDGE_API_KEY:-unused}" \
  -H 'Content-Type: application/json' \
  -d '{"model":"<model-id>","messages":[{"role":"user","content":"Hello"}]}'
```

Replace `<model-id>` with a returned ID. Add `"stream": true` and use `curl -N` for streaming.

## DeepSeek Harness

Merge the [provider template](scripts/dsh-provider.example.yaml) into your existing DSH settings. Do not replace unrelated settings.

```yaml
llm-pi-ai:
  providers:
    codex-bridge:
      displayName: Codex Bridge
      api: openai-completions
      baseURL: http://127.0.0.1:8000/v1
      apiKeyEnv: CODEX_BRIDGE_API_KEY
      reasoning: medium
      compat:
        thinkingFormat: openai
        supportsReasoningEffort: true
      models:
        - id: <model-id>
          input: [text, image]
          reasoningEfforts:
            low: low
            medium: medium
            high: high
            xhigh: xhigh
            # max: ultra  # Enable only if this model accepts ultra.
```

- Replace the URL and model ID. Set `CODEX_BRIDGE_API_KEY` in DSH's environment or credential service to your bridge key.
- **Declare capabilities:** `input: [text, image]` enables images; `reasoningEfforts` enables reasoning choices. Only list capabilities your model supports.
- **Ultra:** add `max: ultra` under `reasoningEfforts`, then select **max** in DSH (or set `reasoning: max`). The request sends **ultra**, not `xhigh`. Current DSH uses `max` as the option key, not `ultra`.
- For Responses, change `api` to `openai-responses` and remove the Chat-specific `compat` block.

## Supported capabilities

| Capability | Endpoint / behavior |
|---|---|
| Text, streaming, function calls, image input | `/v1/chat/completions`, `/v1/responses` |
| Legacy text completions | `/v1/completions` |
| Model listing | `/v1/models` |
| Responses continuation | `store`, `previous_response_id`, GET/DELETE `/v1/responses/{id}` |
| Image generation | `/v1/images/generations`; requires an available upstream capability |
| Speech synthesis | `/v1/audio/speech`; requires an available upstream capability |

**Reasoning effort:** use `"reasoning_effort":"high"` for Chat or `"reasoning":{"effort":"high"}` for Responses. Responses also accepts the top-level alias, with the native field taking precedence. Model-specific strings such as `ultra`, `none`, and `minimal` pass through for upstream validation; they are not supported by every model. `extra high` remains an alias for `xhigh`. Omitting effort uses the upstream default, not necessarily “off.”

**Images:** Chat accepts `image_url` content parts; Responses accepts `input_image`. Both support image URLs and Base64 data URLs. Audio/video input and audio output inside chat are not supported; speech synthesis uses the separate endpoint above.

## Important limitations

- **Tools run in your client.** The bridge forwards function calls and results; only `type: function` tools are supported in chat/Responses requests.
- **Not all OpenAI parameters apply.** Output token limits and sampling settings such as `temperature` are ignored with compatibility warnings. Set `BRIDGE_STRICT_COMPATIBILITY=true` to reject ignored parameters. JSON/schema output is instruction-guided, not guaranteed strict structured output.
- **Responses history is temporary.** Stored responses expire, disappear on restart, and are not shared between processes. Replay full history when durability is needed.
- **Media availability depends on the account/model.** `/v1/models` is not a separate image/speech model catalog. Speech formats other than WAV/PCM require local `ffmpeg` on the realtime path.
- **Other APIs are not provided by subscription authentication.** Recognized endpoints such as embeddings, files, and transcription can be proxied when `OPENAI_API_KEY` is configured; otherwise they return `501`. Unknown paths return `404`. OpenAI API fallback uses separate credentials and may incur separate charges.

## Configuration and troubleshooting

Edit `.env`; see [.env.example](.env.example) for available settings.

| Setting | Purpose |
|---|---|
| `HOST` / `PORT` | Listening address; defaults to `127.0.0.1:8000` |
| `BRIDGE_API_KEY` | Client authentication to the bridge |
| `CHATGPT_ACCESS_TOKEN` | Optional alternative to Codex login |
| `CHATGPT_MODELS` / `CHATGPT_EXTRA_MODELS` | Override / extend the model list; does not grant model access |
| `OPENAI_API_KEY` | Optional OpenAI API proxy/media fallback credentials |
| `CHATGPT_MEDIA_MODEL` / `CHATGPT_REALTIME_MODEL` | Upstream models for image generation / speech |

- **401:** check the bridge key; if the error is from upstream, renew your Codex login or token.
- **Empty model list / model not found:** check account access and the homepage's discovery status; do not assume a model ID exists.
- **No reasoning or image option in DSH:** check the model capability declarations above.
- **Effort rejected upstream:** use a value supported by that specific model; the bridge does not downgrade it automatically.

For deployment, rollback, and operational details, see [OPERATIONS.md](OPERATIONS.md). For integration verification, see [DSH probe notes](scripts/DSH_PROBE.md).

This project depends on upstream ChatGPT / Codex behavior, which may change. Follow OpenAI's terms of service and your account's usage limits.
