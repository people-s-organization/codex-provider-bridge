# Operation and acceptance

## Boundary and authentication

Default bind is `127.0.0.1`. Set `BRIDGE_API_KEY` before public binding; clients send
`Authorization: Bearer <bridge-key>`. This is distinct from upstream credentials.
Only minimal `GET/HEAD /health` is unauthenticated. Explicitly setting
`ALLOW_UNAUTHENTICATED_PUBLIC=true` bypasses the public-binding safety check and is
not recommended. Use TLS at a trusted reverse proxy for remote access.

`MAX_REQUEST_BYTES` (16 MiB), `REQUEST_BODY_TIMEOUT_SECONDS` (30), and
`MAX_CONCURRENT_REQUESTS` (32 per process) bound incoming work including streams.
Generated `X-Request-ID` values identify requests; error logs record only request
ID and exception type, not prompts, tokens or upstream response bodies.

## Responses storage (store / previous_response_id)

The subscription channel is stateless, so the bridge keeps its own record of the
responses it produced to serve `previous_response_id` chaining and
`GET/DELETE /v1/responses/{id}`. Storage is **memory only**: bounded LRU of
`BRIDGE_RESPONSE_STORE_MAX` entries (256) with `BRIDGE_RESPONSE_STORE_TTL` seconds
(3600). Nothing is written to disk, records vanish on restart, and records are not
shared between processes - run a single worker, or accept that a chain may land on a
process that has no record of the previous response. `store` defaults to true like the
real API; `store:false` opts out. An unknown id returns
`404 previous_response_not_found` / `404 response_not_found`. If you need durable
history, replay the full conversation instead of relying on the store.

## Browser clients and stream liveness

`BRIDGE_CORS_ORIGINS` (default `*`) controls `Access-Control-Allow-Origin`; explicit
origins enable credentials, `*` does not. SSE responses emit a `: ping` comment after
`STREAM_KEEPALIVE_SECONDS` (15, 0 disables) of silence so proxies and clients do not
time out mid-generation.

## Compatibility

Unsupported function shapes, non-function tools and orphan tool results are rejected
explicitly. Legacy function calls are supported but cannot represent multiple parallel
calls. Chat refusal surfaces an explicit error; Responses keeps native refusal content.
JSON schema response formatting remains instruction-based, not a server-side guarantee
of schema validity. `store` for chat completions is ignored with a warning because the
API exposes no retrieval route for them.

Explicit options the subscription endpoint cannot honor produce
`X-Bridge-Compatibility-Warnings`. Set `BRIDGE_STRICT_COMPATIBILITY=true` to reject instead.
No character clipping is presented as a token budget.

## Upstream transport

Connection pooling is bounded by `UPSTREAM_MAX_CONNECTIONS` (32). Configure
`UPSTREAM_CONNECT_TIMEOUT` (15), `UPSTREAM_FIRST_EVENT_TIMEOUT` (120),
`UPSTREAM_IDLE_TIMEOUT` (120), `UPSTREAM_WRITE_TIMEOUT` (30), and
`UPSTREAM_POOL_TIMEOUT` (15), all in seconds. `UPSTREAM_MAX_EVENT_BYTES` defaults to
16 MiB. The transport supports multiline SSE, cancels when its consuming task is
cancelled, and closes pooled connections at shutdown. A truncated generation is an
error, not a successful empty answer.

Generation POSTs are **never automatically replayed**, including after connection
failures: the bridge cannot establish whether upstream work already happened.
Nonstream errors retain HTTP status and bounded numeric Retry-After; a failure after
stream headers were sent must be represented inside SSE and cannot change HTTP 200.

Expired JWT credentials may adopt a valid token already renewed by the user's Codex
login in `$CODEX_HOME/auth.json`. The bridge does not initiate interactive login or
refresh-token exchanges inside a request; if no renewed credential exists, upstream
401 is returned and the operator must log in again.

## Deployment and rollback

Use the existing systemd user unit; do not start a replacement server. Commit all
changes, then from this checkout:

```bash
BRIDGE_HEALTH_URL=http://127.0.0.1:8790/health bash deploy.sh <commit>
```

The script requires a clean checkout, locks deployment, verifies the unit's working
directory, runs the full test suite before restarting, and verifies the exact commit
and a changed startup timestamp at the existing URL. It does not install dependencies.
On failure it restores the commit actually reported by the previous running service.
For the first migration from a legacy health endpoint lacking commit identity, supply
`BRIDGE_ROLLBACK_COMMIT=<verified-old-commit>` explicitly. Deployment to a different
commit may intentionally leave detached HEAD; branches are never force-moved.

Rollback checks are tested with mocked service commands. A real rollback failure
injection is not performed automatically on a running user service.

## Live OpenAI-surface conformance check

```bash
.venv/bin/python scripts/openai_surface_check.py --endpoint http://127.0.0.1:8790
.venv/bin/python scripts/openai_surface_check.py --key "$BRIDGE_API_KEY" --json
```

Every check talks to the running service and asserts the status codes and shapes an
OpenAI-compatible client relies on: model listing and `model_not_found`, chat
completion shape, 400 validation envelopes with a located `param`, SSE framing and
`[DONE]`, Responses storage plus `previous_response_id` chaining, 404 for unknown
routes, honest errors for unavailable capabilities, CORS preflight and health. It
really generates (no mocks), so it costs a few upstream calls; optional checks such
as media credentials are reported as SKIP rather than failures. Exit code 0 means
every check passed.

## Real bounded agent acceptance

```bash
.venv/bin/python scripts/agent_acceptance.py --endpoint http://127.0.0.1:8790
.venv/bin/python scripts/agent_acceptance.py --endpoint http://127.0.0.1:8790 --stream
.venv/bin/python scripts/agent_acceptance.py --endpoint http://127.0.0.1:8790 --api responses
```

Use `AGENT_ACCEPTANCE_API_KEY` for bridge authentication. The client discovers actual
models, creates a temporary broken calculator, and exposes only read_file,
write_file and a fixed test command. The model must inspect files, reproduce failing
tests, modify the calculator, rerun passing tests and consume results. Reports record
actual tool execution and assertions; no weather or other synthetic tool results are
substituted. This is a bounded real client integration test, **not** proof that every
third-party Agent SDK works, and not an OS security sandbox for hostile Python code.
