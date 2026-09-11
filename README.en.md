# Codex Provider Bridge

[简体中文](README.md) | **English**

Bridges a ChatGPT Web / Codex login session into a local interface that aims to be as OpenAI-compatible as possible, for agents such as DeepSeek Harness, OpenClaw, and Hermes that support OpenAI-compatible interfaces.

The project connects to ChatGPT's `codex/responses` channel and exposes common OpenAI-style routes:

- `GET /v1/models`
- `POST /v1/completions`
- `POST /v1/responses`
- `POST /v1/chat/completions`
- `POST /v1/images/generations`
- `POST /v1/audio/speech`
- Corresponding non-`/v1` aliases: `/completions`, `/responses`, `/chat/completions`, `/images/generations`, `/audio/speech`
- Recognized endpoints not supported by the subscription channel (such as embeddings and files): proxied to the official OpenAI API when `OPENAI_API_KEY` is configured; otherwise return `501 unsupported_endpoint`. Unknown paths always return `404`.
- `GET /`
- `GET /health`
- `GET /routes`

## Use Cases

- Local desktop: start with browser login or an existing token.
- Cloud server / SSH: an existing token, `~/.codex/auth.json`, or pure-HTTP device-code login is recommended.
- LAN sharing: listens only on `127.0.0.1` by default. Set `BRIDGE_API_KEY` before listening on a public-facing address; set `ALLOW_UNAUTHENTICATED_PUBLIC=true` only if you explicitly accept the risk of unauthenticated access.

## Authentication Methods

At startup, authentication is attempted in this order:

1. `CHATGPT_ACCESS_TOKEN` in `.env`
2. `access_token` in `~/.codex/auth.json`
3. Browser login
4. Device-code login

You can control the preference with the `--auth` command-line argument:

- `prompt`: the default; if neither 1 nor 2 is present, asks at startup whether to use browser login or device-code.
- `auto`: automatic behavior; in an interactive terminal, if neither 1 nor 2 is present, also lets you manually choose 3 or 4.
- `browser`: prefers browser login.
- `device`: goes directly to device-code, suitable for cloud servers.

If you do not pass `--auth`, the default is `prompt`.  
The `CHATGPT_AUTH_METHOD` environment variable is also retained as a compatibility fallback, but the command-line argument takes precedence.

On headless Linux, if the current terminal is not interactive, `prompt` automatically falls back to the recommended method, usually device-code.

## Quick Start

### Option 1: One-Step Startup

```bash
chmod +x start.sh
./start.sh
```

`start.sh` automatically:

1. Creates `.venv`
2. Installs dependencies
3. Starts the service

If you also need desktop browser login, you can install the Playwright browser before the first startup:

```bash
INSTALL_PLAYWRIGHT_BROWSER=1 ./start.sh
```

To explicitly specify the authentication method:

```bash
./start.sh --auth prompt
./start.sh --auth browser
./start.sh --auth device
```

### Option 2: Manual Startup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py --auth prompt
```

## Cloud Server Deployment Recommendations

The recommended order on a cloud server is:

1. Set `CHATGPT_ACCESS_TOKEN` directly
2. Or copy `~/.codex/auth.json`
3. Or explicitly use `--auth device`

Example:

```bash
cp .env.example .env
./start.sh --auth device
```

After startup, the terminal prints an authorization link and code. Open the link in your own local browser and enter the code; the cloud server keeps polling and saves the token to `.env`.

If you start interactively over SSH and neither `.env` nor `~/.codex/auth.json` has a usable token, the default `--auth prompt` first asks you to choose:

- 3. Browser login
- 4. Device-code login

You can make the choice manually at that point.

## Local Desktop Deployment Recommendations

If your local machine has a browser environment, simply run:

```bash
./start.sh
```

Without an existing token, the program attempts to open a browser for login. If browser login fails, it automatically falls back to device-code.

## Configuration

See [.env.example](./.env.example):

```env
CHATGPT_ACCESS_TOKEN=
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com
CHATGPT_ACCOUNT_ID=
CHATGPT_AUTH_METHOD=prompt
HOST=127.0.0.1
PORT=8000
BRIDGE_API_KEY=
ALLOW_UNAUTHENTICATED_PUBLIC=false
BRIDGE_STRICT_COMPATIBILITY=false
CHATGPT_BASE_URL=https://chatgpt.com
CHATGPT_MODELS=
CHATGPT_EXTRA_MODELS=
CHATGPT_DEFAULT_MODEL=
CHATGPT_MEDIA_MODEL=
CHATGPT_REALTIME_MODEL=
CHATGPT_MODELS_FILE=~/.codex/models_cache.json
CHATGPT_CODEX_CONFIG_FILE=~/.codex/config.toml
# If unset, the default upstream URL is used; explicitly setting an empty value disables the corresponding discovery.
# CHATGPT_MODELS_URL=
# CHATGPT_CAPABILITIES_URL=
# CHATGPT_CLIENT_VERSION=
CHATGPT_MODEL_ALIASES=
CHATGPT_EXTRA_MODEL_ALIASES=
BRIDGE_RESPONSE_STORE_MAX=256
BRIDGE_RESPONSE_STORE_TTL=3600
BRIDGE_CORS_ORIGINS=*
STREAM_KEEPALIVE_SECONDS=15
```

Details:

- `CHATGPT_ACCESS_TOKEN`: if you already have a token, enter it to start directly.
- `OPENAI_API_KEY`: optional; images and speech both prefer ChatGPT/Codex capabilities, using this as a fallback only when the corresponding capability fails or a token is missing. Recognized endpoints without built-in support also use it to proxy to the official OpenAI API; unknown paths still return 404.
- `OPENAI_BASE_URL`: OpenAI API address; defaults to `https://api.openai.com`.
- `CHATGPT_ACCOUNT_ID`: optional; read from `~/.codex/auth.json` by default and included in the realtime speech handshake.
- `CHATGPT_AUTH_METHOD`: compatibility fallback setting; accepts `prompt | auto | browser | device`.
- `HOST` / `PORT`: service listening address; defaults to `127.0.0.1:8000`.
- `BRIDGE_API_KEY`: Bearer key for client access to the bridge, distinct from the upstream `OPENAI_API_KEY`. Once configured, endpoints require authentication except for `/health` and necessary preflight requests.
- `ALLOW_UNAUTHENTICATED_PUBLIC`: whether to explicitly allow listening on a non-loopback address without authentication; defaults to `false`.
- `BRIDGE_STRICT_COMPATIBILITY`: returns errors for request parameters that cannot be honored, rather than accepting them and explaining via warning headers; does not change the pass-through semantics of function-tool `strict`.
- `CHATGPT_BASE_URL`: defaults to `https://chatgpt.com`.
- `CHATGPT_MODELS`: explicitly specifies the model list returned by `/v1/models`, as comma-separated values or a JSON array; when set, neither the cache nor upstream is read.
- `CHATGPT_EXTRA_MODELS`: appends models to the automatically obtained list, such as newly released models not yet reflected in the local cache.
- `CHATGPT_DEFAULT_MODEL`: model selected by default in the homepage test form; if unset, uses `model` from `~/.codex/config.toml`, then falls back to the first model in the current list.
- `CHATGPT_MEDIA_MODEL`: upstream Responses model used when the image endpoint calls the Codex `image_generation` tool; if unset, resolves in this order: `CHATGPT_DEFAULT_MODEL` → a genuinely existing `model` from the request → the discovered default model. Image model names such as `gpt-image-*` are not treated as Responses models, as upstream would reject them outright.
- `CHATGPT_REALTIME_MODEL`: model used when the speech endpoint calls the realtime WebSocket; defaults to the request's `model`.
- `CHATGPT_MODELS_FILE`: Codex model cache path; defaults to `~/.codex/models_cache.json`.
- `CHATGPT_CODEX_CONFIG_FILE`: Codex configuration path; by default reads `model` from `~/.codex/config.toml`.
- `CHATGPT_MODELS_URL`: URL used to query upstream for the model list when the cache is empty; defaults to `$CHATGPT_BASE_URL/backend-api/codex/models`. Set to an empty value to disable upstream queries.
- `CHATGPT_CAPABILITIES_URL`: capability-discovery URL (the ChatGPT web model list, which uses `enabled_tools` to flag image tools); defaults to `$CHATGPT_BASE_URL/backend-api/models`. Set to an empty value to disable capability discovery.
- `CHATGPT_MODEL_ALIASES` / `CHATGPT_EXTRA_MODEL_ALIASES`: model alias mappings, as a JSON object or `old=new,old2=new2`; no aliases are included by default.
- `BRIDGE_RESPONSE_STORE_MAX` / `BRIDGE_RESPONSE_STORE_TTL`: maximum number of Responses stored by the bridge and expiry in seconds; in-process storage only.
- `BRIDGE_CORS_ORIGINS`: allowed browser origins, comma-separated; `*` means any origin (credentials are not allowed in that case).
- `STREAM_KEEPALIVE_SECONDS`: sends an SSE `: ping` comment line after this many idle seconds; 0 disables it.

## Usage

After startup, open the homepage:

- `http://127.0.0.1:8000/`
- Or the actual address displayed on the homepage

If the default port is occupied, the program automatically switches to a nearby available port. Use the actual listening address shown in the startup logs and on the homepage; `/health` returns only minimal health information.

When configuring an agent, point its Base URL to:

```text
http://<your-host>:<port>/v1
```

If `BRIDGE_API_KEY` is set, the client must use the same key. If bridge authentication is disabled but the client requires an API Key, you may enter a placeholder. Do not configure the client directly with the upstream ChatGPT access token.

For operational parameters, strict compatibility mode, rollback deployment, and real-agent acceptance testing, see [OPERATIONS.md](OPERATIONS.md).

## DeepSeek Harness Integration and Verification

In the DSH provider configuration, use `api: openai-completions` and point the Base URL to this bridge's `/v1` (for example, the current deployment's `http://127.0.0.1:8790/v1`; the default startup port is 8000). Use a model ID actually returned by the bridge's `/v1/models`, and set the API Key according to the bridge authentication configuration above.

DSH's bash, read, web_search, and similar tools are client-executed function tools; the bridge does not need to implement each tool by name. DSH's function named `web_search` is not the same as a provider-native `type: web_search`: the former can be forwarded, while the latter is not a currently supported tool type.

Reproducible verification scripts (the service must already be running; adjust arguments/environment variables if using a different port):

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/openai_surface_check.py --endpoint http://127.0.0.1:8790
.venv/bin/python scripts/responses_tool_chain_check.py --endpoint http://127.0.0.1:8790
DSH_RUNTIME=/path/to/dsh-runtime BRIDGE_BASE_URL=http://127.0.0.1:8790/v1 node scripts/dsh_tool_probe.mjs
```

- The interface checks report PASS, FAIL, and SKIP separately; authentication checks are skipped when no authentication key is supplied, and skips must not be counted as passes.
- The Responses tool-chain script performs a fixed addition, then submits only the tool result using `previous_response_id` to check whether the model consumes the result.
- The DSH probe loads the **installed, real `todo_write` plugin definition and execution code**, completing two interaction rounds through actual pi-ai/OpenAI SDK serialization and SSE parsing; tool execution writes only to an isolated in-memory session. See [scripts/DSH_PROBE.md](scripts/DSH_PROBE.md).
- This does not constitute execution-based acceptance testing of every DSH tool, nor complete end-to-end acceptance testing of an active GUI session, the permission system, and persistence. Handwritten harness-shaped schema tests are regression tests only and cannot replace the evidence above.

## API Compatibility Scope

### OpenAI API Endpoint Comparison

| Endpoint | Status | Notes |
|---|---|---|
| `GET /v1/models` | ✅ | Actual discovery results, with no hardcoded list or fallback |
| `GET /v1/models/{id}` | ✅ | Returns a model object on a match; otherwise `404 model_not_found` |
| `POST /v1/chat/completions` | ✅ | Text, streaming, tool calls, image input |
| `POST /v1/responses` | ✅ | Text, streaming, tool calls, `store`, `previous_response_id` |
| `GET /v1/responses/{id}` | ✅ | Retrieves a response stored by the bridge (in-process; see below) |
| `DELETE /v1/responses/{id}` | ✅ | Deletes a response stored by the bridge |
| `POST /v1/completions` | ✅ | Legacy Completions shape, mapped to one text call |
| `POST /v1/images/generations` | ✅ | Codex `image_generation` tool; `501` without credentials |
| `POST /v1/audio/speech` | ✅ | Codex realtime; `501` without credentials |
| `POST /v1/responses/input_tokens` | ❌ | The subscription channel provides no tokenizer; no fabricated numbers are returned |
| `POST /v1/embeddings` / `moderations` | ❌ | The subscription channel exposes neither capability; proxied as-is when `OPENAI_API_KEY` is configured |
| `files` / `batches` / `fine_tuning` / `vector_stores` / `assistants` | ❌ | Same as above: `501 unsupported_endpoint` without credentials, proxied with credentials |
| `audio/transcriptions`, `audio/translations`, `images/edits`, `images/variations` | ❌ | Same as above |

Endpoints marked ❌ do not return fake data: without a real capability, they return `501` with a reason; when `OPENAI_API_KEY` is configured, they are proxied as-is to the official API. Unknown paths return `404 Invalid URL (...)`, matching official behavior.

### Protocol-Level Consistency

- Request validation failures return **`400`** + `{"error":{"message","type","param","code"}}`, with `param` identifying the invalid field. The full request body is not echoed; some errors include the relevant function name or response ID. Upstream errors may include a truncated reason with credentials redacted; this must not be treated as a guarantee of complete privacy redaction.
- Unknown models return **`404 model_not_found`** (determined only when the model list is actually available; if discovery sources are empty, the decision is left to upstream).
- `store` / `previous_response_id`: upstream is stateless and rejects `store`, so **the bridge itself** maintains a bounded, expiring, in-process-only response store (`BRIDGE_RESPONSE_STORE_MAX`, `BRIDGE_RESPONSE_STORE_TTL`). `store` defaults to `true` (matching the official API). With `store:false`, the response is not stored, and a later attempt to continue with that ID returns `404 previous_response_not_found`. Records are lost on restart and are not shared across processes; do not rely on this if you need persistence across restarts.
- Streaming responses use `text/event-stream`. After more than `STREAM_KEEPALIVE_SECONDS` of inactivity (default 15; 0 disables it), a `: ping` comment line is sent to avoid proxy disconnections. Chat/legacy Completions send `data: [DONE]` at the end; native Responses use terminal events such as `response.completed` and do not require `[DONE]`. Upstream errors occurring after response headers have been sent are reported through SSE error events; HTTP 200 alone does not mean generation succeeded.
- Direct browser access is supported: `BRIDGE_CORS_ORIGINS` controls allowed origins (default `*`; credentials are allowed only when specific origins are configured).
- 401 uses `{"error":{"code":"invalid_api_key",...}}`; rate-limit-related upstream errors retain their status code and `Retry-After`.

### Capability Boundaries (Clarified Up Front)

- ✅ **Text Q&A**: single-turn, multi-turn, and SSE streaming are available; supports `reasoning_effort` and the legacy Completions shape. JSON output formats are guided through instructions; strict server-side schema compliance is not guaranteed.
- ✅ **Function tool calling**: `tools` / legacy `functions` are forwarded upstream, and streamed upstream `function_call` results are converted to OpenAI-shaped `tool_calls` (chat) or `function_call` items (responses). Non-streaming and SSE streaming, `tool_choice` values `auto`/`none`/`required`/a specific function, and `parallel_tool_calls` are all supported.
- ⚠️ **The bridge does not execute tools**: it only “passes the model's call requests to the client, then carries the client's results back.” Your client is what actually reads/writes files and runs commands. Historically, this has been the most easily misunderstood point, so it is stated separately here.
- ⚠️ **Only `type: "function"` tools are supported**: other tool types (including non-function tools in mixed tool lists) are explicitly rejected, no longer silently discarded.
- ⚠️ **Output limits are not supported**: default compatibility mode accepts these parameters but reports that they are not honored through `X-Bridge-Compatibility-Warnings`; `BRIDGE_STRICT_COMPATIBILITY=true` rejects them. The bridge does not truncate characters to pretend it has enforced a token budget.

Verification (tested on 2026-09-10, all against real upstream services):

| Scenario | Result |
|---|---|
| Non-streaming chat + `tools` | `finish_reason: "tool_calls"`, `tool_calls[0].function = {"name":"get_weather","arguments":"{\"city\":\"Paris\"}"}`, `content: null` |
| Streaming chat | First sends `{"index":0,"id":"call_…","type":"function","function":{"name":…,"arguments":""}}`, then argument delta chunks, and finally `finish_reason: "tool_calls"` |
| Parallel calls | Each of two tools returns a separate `tool_calls` entry, with `index` 0/1 respectively |
| Result round trip | After replaying `tool_calls` + `role:"tool"` results, the model responds with `It's currently 18°C and sunny in Paris.` |
| `/v1/responses` | Non-streaming output contains `function_call` items (`call_id`/`name`/`arguments`); streaming passes through events such as `response.function_call_arguments.delta` |
| `strict: true` tool schema | Upstream accepted a compliant example and rejected a noncompliant example. The bridge preserves `strict` and the schema as-is, does not automatically downgrade them, and does not promise that every schema will work |

`/health` returns only minimal health status, startup time, and deployment commit, and no longer triggers external capability discovery. Detailed capabilities are displayed on the homepage (authentication is required when a key is configured). The function-calling capability indicator denotes only the bridge's protocol support; it does not claim that every upstream model has been tested successfully.

### Text Endpoints

- `/v1/chat/completions`: supports regular and SSE streaming responses; `messages[].content` accepts strings as well as common text / image content parts.
- `/v1/responses`: supports non-streaming and SSE streaming; non-streaming returns `output_text`, `output`, and `usage`.
- `/v1/completions`: supports the legacy Completions shape, mapping it to one Chat/Responses text call.
- `reasoning_effort` and `reasoning.effort` support `low` / `medium` / `high` / `xhigh`, and also accept `extra high`.
- `response_format={"type":"json_object"}` and common `json_schema` formats are converted into additional instructions to guide upstream to return pure JSON.
- `/v1/responses` `text.format.type=json_schema` is converted into additional instructions to guide upstream to return pure JSON conforming to the schema.
- Content parts in multi-turn conversations are retyped by role: assistant turns send only `output_text` / `refusal`, and user turns send only `input_text` / `input_image`. Upstream rejects the entire request with `Invalid value: 'input_text'` if an assistant turn receives `input_text`, so client-supplied message items in `/v1/responses` `input` receive the same normalization (other item types pass through unchanged).
- Tool history is strictly paired: assistant `tool_calls` → `function_call`, tool results → `function_call_output`. Missing IDs, orphaned results, or duplicate results cause request-validation errors and are no longer downgraded to user text. Legacy `function_call` / `role:"function"` are also replayed with pairing semantics.
- system/developer text is extracted into upstream's separate `instructions`, not sent as system-role input items (which upstream rejects), and not downgraded to user text. Other message ordering and Responses reasoning/item metadata are preserved.
- Responses supports bridge-side in-memory storage and continuation through `previous_response_id`, including submissions containing only `function_call_output`: pairing is checked after stored history is merged. Unknown previous IDs return 404; invalid pairing returns 400. Streaming requests are also prechecked before SSE response headers are sent. A history snapshot is saved before generation to prevent new records from losing history if old records are deleted or expire during generation.
- Responses storage does not survive restarts or share state across processes. Chat Completions `store:true` remains unsupported and produces a compatibility-mode warning or a strict-mode rejection.
- Error responses are normalized to the OpenAI-style `{ "error": { "message", "type", "param", "code" } }`.

Areas where compatibility is attempted but full equivalence is not possible:

- `tool_choice="required"`, specific functions, and `parallel_tool_calls` are all supported (see “Capability Boundaries”). The bridge only forwards calls, not executes tools; the client is the actual tool executor. Tools whose `type` is not `function` (such as `web_search`) are explicitly rejected.
- `n > 1`, `best_of`, and `logprobs` return explicit errors. The upstream Codex responses channel returns one answer per turn and does not return token-level logprobs.
- `max_tokens` / `max_completion_tokens` / `max_output_tokens` / `truncation` are not forwarded upstream: tests showed that the Codex responses channel returns `400 Unsupported parameter` for all four. Compatibility mode therefore omits them and warns in response headers; strict mode returns a validation error.
- Chat Completions audio output modality is not handled here; use `/v1/audio/speech`. For full OpenAI behavior on audio input, transcription, translation, and similar endpoints, configure `OPENAI_API_KEY` to use proxying.
- Endpoints without built-in support, such as Assistants, Files, Batches, Vector Stores, Fine-tuning, Embeddings, and Moderations: proxied to the official API when `OPENAI_API_KEY` is available; otherwise return `501 unsupported_endpoint`, explaining that ChatGPT/Codex subscription auth does not expose the corresponding REST capabilities to the bridge layer.

`/v1/models` maintains no hardcoded primary or fallback list. Its lookup order is:

1. `CHATGPT_MODELS`
2. Unexpired `$CODEX_HOME/models_cache.json` (default `~/.codex`, based on file mtime, 60 seconds)
3. Query upstream (`CHATGPT_MODELS_URL`) when the cache is empty or expired; caches are isolated by URL/account/credentials/actual client version, and concurrent requests are coalesced
4. Append `CHATGPT_EXTRA_MODELS`

If none of these provides a source, `/v1/models` returns an empty list; the homepage model snapshot includes sources and failure reasons. An expired cache is not presented as available models after a failed refresh. Explicit environment configuration is a user declaration, not evidence of verified calls. The upstream-required client_version comes from `CHATGPT_CLIENT_VERSION`, the actual cache, or an installed `codex --version`; the version is not hardcoded.

The default test model can only be chosen from the actual discovered set (configuring a nonexistent model does not add it to the list), in this order:

1. `CHATGPT_DEFAULT_MODEL`
2. `model` in `~/.codex/config.toml`
3. The first model in the current list (the homepage does not prefill a model when the list is empty)

There are no built-in aliases by default; aliases are determined entirely by `CHATGPT_MODEL_ALIASES` / `CHATGPT_EXTRA_MODEL_ALIASES`.

### Media Endpoints

- `/v1/images/generations`: prefers the ChatGPT/Codex backend's Responses `image_generation` tool and returns `b64_json`; if there is no ChatGPT token but `OPENAI_API_KEY` is available, falls back to proxying to the OpenAI Image API.
- `/v1/audio/speech`: prefers connecting directly to the OpenAI realtime WebSocket with the ChatGPT/Codex bearer, collecting `response.output_audio.delta` and returning real audio; if there is no ChatGPT token but `OPENAI_API_KEY` is available, falls back to proxying to the OpenAI Speech API.
- ChatGPT realtime natively outputs 24 kHz PCM; `wav` / `pcm` can be returned directly, while `mp3` / `aac` / `flac` / `opus` require local `ffmpeg`.
- Without the corresponding real upstream credentials, media endpoints return `501`, not fake images or audio.
- `model` is required for media endpoints; a missing or empty value returns `400`. The bridge does not guess a model name for you. When images use the Codex channel, the upstream Responses model resolves as `CHATGPT_MEDIA_MODEL` → `CHATGPT_DEFAULT_MODEL` → a genuinely existing `model` from the request → the discovered default model. Speech uses `CHATGPT_REALTIME_MODEL` → the request's `model`.

Media capability discovery (why media models do not appear in `/v1/models`):

- The Codex model lists (local cache and upstream `/backend-api/codex/models`) **contain only text models**; the raw responses have no `gpt-image-*` / `tts-*` / `gpt-realtime-*` entries. Media-list routes such as `/backend-api/codex/image_generation/models` and `/v1/realtime/models` all return 404, and `api.openai.com/v1/models` rejects ChatGPT login credentials with 403.
- Upstream treats media as **capabilities/tools**, not models: ChatGPT web's `/backend-api/models` uses `image_gen_tool_enabled` / `dalle_3` in `enabled_tools` to mark which models can use image tools. Realtime speech only accepts the `?model=` parameter; omitting it immediately yields `missing_model`.
- Image capabilities are shown in the homepage model snapshot. Failed discovery or missing evidence yields `available:null/status:unknown`; only an explicit tool list without an image flag is treated as unsupported. These names identify tool hosts, not the image generator's model identity.
- In Codex image responses, `model` / `codex_model` identify the actual driving model, `requested_model` preserves the client's requested label, and `image_model:null` explicitly indicates that the underlying generator's identity is undisclosed, with an accompanying explanation.

## Request Examples

Replace `<model-id>` in the examples with an actual model name returned by `/v1/models`; this bridge does not choose a model for you.

Chat Completions:

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<model-id>",
    "messages": [{"role": "user", "content": "Reply with exactly: bridge ok"}],
    "reasoning_effort": "medium"
  }'
```

Streaming Chat Completions:

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<model-id>",
    "messages": [{"role": "user", "content": "Count to three."}],
    "stream": true,
    "stream_options": {"include_usage": true}
  }'
```

Responses + JSON Schema:

```bash
curl http://127.0.0.1:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<model-id>",
    "input": [{"role": "user", "content": [{"type": "input_text", "text": "summarize this bridge"}]}],
    "text": {
      "format": {
        "type": "json_schema",
        "name": "summary",
        "schema": {
          "type": "object",
          "properties": {"summary": {"type": "string"}},
          "required": ["summary"],
          "additionalProperties": false
        },
        "strict": true
      }
    }
  }'
```

Image generation endpoint:

```bash
curl http://127.0.0.1:8000/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"<image-model>","prompt":"a bridge diagram","size":"1024x1024","quality":"auto","response_format":"b64_json"}'
```

Audio generation endpoint:

```bash
curl http://127.0.0.1:8000/v1/audio/speech \
  -H "Content-Type: application/json" \
  -o speech.wav \
  -d '{"model":"<speech-model>","input":"bridge audio test","voice":"marin","response_format":"wav"}'
```

`<image-model>` / `<speech-model>` must be configured according to capabilities actually available to the account. The Codex list in `/v1/models` does not provide a separate image/speech model directory and cannot guarantee media model availability. See “Media Endpoints” for the distinction between image Responses driving-model resolution and the underlying generator's identity.

## Debugging Endpoints

- `/`: homepage and test forms
- `/health`: only service status, service name, deployment commit, and startup time; does not query external services or return model/account information
- `/routes`: available routes
- `/models` / `/v1/models`: model list
- `/v1`: API index and recommended `agent_base_urls`

The homepage includes:

- Switchable test forms for `chat/completions`, `responses`, `images/generations`, and `audio/speech`
- Chat Completions streaming response tests
- Reasoning effort selection (`low` / `medium` / `high` / `extra high`)
- Responses JSON Schema test input
- Image and audio response previews

## Docker Notes

The repository's current `Dockerfile` is suitable for lightweight startup, but does not additionally install a Playwright browser.

This is generally not a problem for cloud servers, because device-code now uses pure HTTP and no longer depends on browser automation.  
If you specifically need browser login inside a container, you must additionally install Chromium and its system dependencies.

## Important Notes

- This project is for technical research only; please comply with OpenAI's terms of service.
- Because it depends on upstream ChatGPT Web / Codex behavior, API fields and authentication flows may change in the future.
- High-frequency calls may trigger risk controls or temporary failures.
