# Installed DSH → pi-ai → bridge probe

Run against an **already running** bridge (Node version supported by installed pi-ai; currently Node >=22.19):

```sh
node scripts/dsh_tool_probe.mjs
```

Defaults: installed DSH at `/home/arjenzhou/src/nova/config/dsh-runtime`, bridge `http://127.0.0.1:8790/v1`, first model advertised by `/models`, total timeout 120 seconds. Overrides: `DSH_RUNTIME`, `BRIDGE_BASE_URL` (loopback only), `BRIDGE_MODEL`, `PROBE_TIMEOUT_MS`, and optional `BRIDGE_API_KEY`. No config writes, dependency installation, server startup, commits, or deployment. JSON evidence is printed to stdout; the script writes no files.

## What this actually exercises

- Imports installed `@deepseek-ai/dsh-tool-todo` and calls its `apply` with fake registration services and explicit `allowParallelInProgress: true`. Captures its actual registered `todo_write`, including description and schema. The installed plugin calls installed `defineTool`, which internally calls `parameterSchemaSpecToJsonSchema`; an additional unsent helper smoke check directly exercises both public exports.
- Sends that definition through installed `@earendil-works/pi-ai/api/openai-completions` **stream**, including its real OpenAI SDK serialization and SSE response parsing. It does not hand-build the chat request. A fetch wrapper records the serialized HTTP JSON body after SDK processing, never headers. Requests are forwarded unchanged to the existing loopback bridge (redirects refused).
- Asks the live model for exactly one known `todo_write` call. Validates the exact fixed arguments before executing the real definition with a fake session that only captures `todo/write` in memory. No arbitrary code execution or persistent todo changes.
- Renders the actual deterministic result and replays the live assistant tool-call message plus a pi-ai `toolResult` through the same stream pipeline. Checks serialized assistant/tool ID linkage, JSON arguments, tool content, unchanged schema, HTTP 200, and final `DSH_PROBE_OK`.

Captured bodies contain only this synthetic probe conversation, installed tool metadata, and model-generated replies. Authentication headers are not captured; a provided key is also redacted from final JSON. Avoid placing secrets in model names or installed tool descriptions. Failures exit nonzero; raw SDK exceptions are intentionally not dumped because they may contain credentials. A model that does not follow the fixed arguments/final marker fails rather than being treated as success.

## Evidence boundary

This is **one actual installed plugin definition**, activated with a declared fake context/configuration. It is **not** the exact all-tool runtime list, not an inspection of the GUI's active session registry, and not a full DSH agent-loop/permission/persistence integration. Model compatibility settings are the pi-ai defaults for the script's declared `openai`/`openai-completions` model, not a claim about active DSH provider settings. The fetch capture establishes the client request sent to the bridge, not the bridge's internal upstream request.

## Observed run

Initial live verification passed against the already-running `http://127.0.0.1:8790/v1` using advertised model `gpt-6-astra` and installed pi-ai 0.85.1: exit 0, `ok: true`, two streamed `POST /v1/chat/completions` requests, both HTTP 200. First stop reason was `toolUse`; second was `stop`, with final text `DSH_PROBE_OK`. The serialized tool had `strict: false`; its root schema retained the installed compiler's implicit openness, while todo items retained `additionalProperties: false`. The exact deterministic rendered result was `Updated todo list: 0 pending, 0 in progress, 1 completed.` and replay retained matching tool-call IDs. This observation does not establish which bridge revision was running; rerun after deployment to verify that deployment.

An initial pre-HTTP attempt failed because pi-ai exports are import-only and `createRequire.resolve` cannot resolve that conditional subpath. The script now imports its inspected installed distribution entry explicitly. No dependency/config changes were needed.
