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

## Compatibility

Unsupported function shapes, non-function tools, orphan tool results,
`previous_response_id` and `store:true` are rejected explicitly. Clients must replay
complete history. Legacy function calls are supported but cannot represent multiple
parallel calls. Chat refusal currently surfaces an explicit error; Responses keeps
native refusal content. JSON schema response formatting remains instruction-based,
not a server-side guarantee of schema validity.

Explicit options the subscription endpoint cannot honor produce
`X-Bridge-Warnings`. Set `BRIDGE_STRICT_COMPATIBILITY=true` to reject instead.
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
