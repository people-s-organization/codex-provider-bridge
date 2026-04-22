import argparse
import html
import socket
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from config import settings
from schemas import ChatCompletionRequest
from bridge import bridge
from auth import ensure_authenticated

app = FastAPI(title="Codex Provider Bridge")
AVAILABLE_MODELS = [
    {"id": "gpt-5.4", "object": "model", "created": 1677610602, "owned_by": "openai"},
    {"id": "gpt-5.4-mini", "object": "model", "created": 1677610602, "owned_by": "openai"},
    {"id": "gpt-5.3-codex-spark", "object": "model", "created": 1677610602, "owned_by": "openai"},
    {"id": "gpt-5.2-codex", "object": "model", "created": 1677610602, "owned_by": "openai"},
]
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
WILDCARD_HOSTS = {"0.0.0.0", "::", ""}
ACTIVE_HOST = settings.host
ACTIVE_PORT = settings.port


def parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex Provider Bridge")
    parser.add_argument(
        "--auth",
        choices=["prompt", "browser", "device", "auto"],
        default=None,
        help="Authentication flow to use when no cached token is available. Default behavior is prompt.",
    )
    return parser.parse_args()


def resolve_bind_port(host: str, preferred_port: int) -> int:
    candidates = [preferred_port] + list(range(preferred_port + 1, preferred_port + 10))

    for port in candidates:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue

    raise OSError(f"No available port found near {preferred_port}")


def add_unique_host(hosts: list[str], host: str | None) -> None:
    if not host:
        return

    normalized = host.strip().strip("[]")
    if not normalized or normalized in WILDCARD_HOSTS or normalized in hosts:
        return

    hosts.append(normalized)


def format_url(host: str, port: int, scheme: str = "http") -> str:
    if ":" in host and not host.startswith("["):
        return f"{scheme}://[{host}]:{port}"
    return f"{scheme}://{host}:{port}"


def get_local_ipv4_addresses() -> list[str]:
    hosts: list[str] = []

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            local_ip = sock.getsockname()[0]
            if not local_ip.startswith("127."):
                add_unique_host(hosts, local_ip)
    except OSError:
        pass

    try:
        hostname = socket.gethostname()
        for _, _, _, _, sockaddr in socket.getaddrinfo(hostname, None, socket.AF_INET):
            local_ip = sockaddr[0]
            if not local_ip.startswith("127."):
                add_unique_host(hosts, local_ip)
    except OSError:
        pass

    return hosts


def resolve_access_urls(
    bind_host: str,
    port: int,
    preferred_host: str | None = None,
    scheme: str = "http",
) -> list[str]:
    hosts: list[str] = []
    add_unique_host(hosts, preferred_host)

    if bind_host in WILDCARD_HOSTS:
        add_unique_host(hosts, "localhost")
        add_unique_host(hosts, "127.0.0.1")
        for local_ip in get_local_ipv4_addresses():
            add_unique_host(hosts, local_ip)
    elif bind_host in LOOPBACK_HOSTS:
        add_unique_host(hosts, "localhost")
        add_unique_host(hosts, "127.0.0.1")
    else:
        add_unique_host(hosts, bind_host)
        try:
            for _, _, _, _, sockaddr in socket.getaddrinfo(bind_host, None, socket.AF_INET):
                add_unique_host(hosts, sockaddr[0])
        except OSError:
            pass

    return [format_url(host, port, scheme) for host in hosts]


def resolve_request_port(request: Request) -> int:
    return request.url.port or ACTIVE_PORT


def resolve_agent_base_urls(request: Request) -> list[str]:
    access_urls = resolve_access_urls(
        bind_host=ACTIVE_HOST,
        port=resolve_request_port(request),
        preferred_host=request.url.hostname,
        scheme=request.url.scheme or "http",
    )
    return [f"{url}/v1" for url in access_urls]


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    base_url = str(request.base_url).rstrip("/")
    bind_address = f"{ACTIVE_HOST}:{resolve_request_port(request)}"
    agent_base_urls = resolve_agent_base_urls(request)
    escaped_base_url = html.escape(base_url)
    escaped_bind_address = html.escape(bind_address)

    model_links = "".join(
        f"<li><code>{model['id']}</code></li>"
        for model in AVAILABLE_MODELS
    )
    model_options = "".join(
        f"<option value=\"{model['id']}\">{model['id']}</option>"
        for model in AVAILABLE_MODELS
    )
    agent_base_link_items = "".join(
        f"<li><a href=\"{html.escape(url.rsplit('/v1', 1)[0], quote=True)}\"><code>{html.escape(url)}</code></a></li>"
        for url in agent_base_urls
    )

    return f"""
    <!DOCTYPE html>
    <html lang="en">
      <head>
        <meta charset="UTF-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>Codex Provider Bridge</title>
        <style>
          :root {{
            color-scheme: light dark;
            --bg: #0b1020;
            --panel: rgba(255, 255, 255, 0.08);
            --panel-border: rgba(255, 255, 255, 0.12);
            --text: #eff6ff;
            --muted: #b8c4d9;
            --accent: #7dd3fc;
            --accent-2: #86efac;
          }}
          * {{
            box-sizing: border-box;
          }}
          body {{
            margin: 0;
            min-height: 100vh;
            font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background:
              radial-gradient(circle at top left, rgba(125, 211, 252, 0.22), transparent 35%),
              radial-gradient(circle at top right, rgba(134, 239, 172, 0.18), transparent 30%),
              linear-gradient(160deg, #0b1020 0%, #121a2d 50%, #0f172a 100%);
            color: var(--text);
          }}
          .wrap {{
            max-width: 960px;
            margin: 0 auto;
            padding: 48px 20px 64px;
          }}
          h1 {{
            margin: 0 0 12px;
            font-size: clamp(2rem, 5vw, 3.3rem);
            line-height: 1.05;
          }}
          p {{
            color: var(--muted);
            font-size: 1.02rem;
            line-height: 1.6;
          }}
          .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 16px;
            margin-top: 28px;
          }}
          .card {{
            background: var(--panel);
            border: 1px solid var(--panel-border);
            border-radius: 18px;
            padding: 18px;
            backdrop-filter: blur(10px);
          }}
          .card h2 {{
            margin: 0 0 12px;
            font-size: 1.1rem;
          }}
          .card a {{
            color: var(--accent);
            text-decoration: none;
          }}
          .card a:hover {{
            text-decoration: underline;
          }}
          code {{
            color: var(--accent-2);
            font-family: ui-monospace, SFMono-Regular, SFMono-Regular, Menlo, Consolas, monospace;
            word-break: break-all;
          }}
          ul {{
            margin: 10px 0 0;
            padding-left: 18px;
            color: var(--muted);
          }}
          .hint {{
            margin-top: 26px;
            padding: 16px 18px;
            border-radius: 16px;
            border: 1px solid rgba(125, 211, 252, 0.24);
            background: rgba(125, 211, 252, 0.08);
          }}
          .playground {{
            margin-top: 22px;
            background: rgba(15, 23, 42, 0.72);
          }}
          .playground-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 14px;
            margin-top: 14px;
          }}
          label {{
            display: block;
            margin-bottom: 8px;
            color: var(--muted);
            font-size: 0.94rem;
          }}
          select, textarea, button {{
            width: 100%;
            border-radius: 14px;
            border: 1px solid rgba(255, 255, 255, 0.16);
            background: rgba(15, 23, 42, 0.88);
            color: var(--text);
            padding: 12px 14px;
            font: inherit;
          }}
          textarea {{
            min-height: 120px;
            resize: vertical;
          }}
          button {{
            cursor: pointer;
            font-weight: 600;
            background: linear-gradient(135deg, rgba(125, 211, 252, 0.22), rgba(134, 239, 172, 0.22));
          }}
          button:disabled {{
            cursor: wait;
            opacity: 0.7;
          }}
          .playground-actions {{
            margin-top: 14px;
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
          }}
          .playground-actions button {{
            width: auto;
            min-width: 180px;
          }}
          .output-panel {{
            margin-top: 16px;
            padding: 16px;
            border-radius: 16px;
            border: 1px solid rgba(255, 255, 255, 0.12);
            background: rgba(2, 6, 23, 0.62);
          }}
          .status {{
            margin: 0 0 10px;
            color: var(--muted);
            font-size: 0.92rem;
          }}
          pre {{
            margin: 0;
            white-space: pre-wrap;
            word-break: break-word;
            color: var(--text);
            font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
          }}
        </style>
      </head>
      <body>
        <main class="wrap">
          <h1>Codex Provider Bridge</h1>
          <p>
            This service exposes a small OpenAI-compatible API on top of the
            ChatGPT/Codex token flow. Use the routes below in your browser or
            point an agent to the base URL ending in <code>/v1</code>.
          </p>

          <section class="grid">
            <article class="card">
              <h2>Service</h2>
              <p><a href="/health">/health</a></p>
              <p><a href="/routes">/routes</a></p>
              <p><a href="/docs">/docs</a></p>
            </article>

            <article class="card">
              <h2>Models</h2>
              <p><a href="/models">/models</a></p>
              <p><a href="/v1/models">/v1/models</a></p>
              <ul>{model_links}</ul>
            </article>

            <article class="card">
              <h2>Completions</h2>
              <p><code>POST /v1/chat/completions</code></p>
              <p><code>POST /chat/completions</code></p>
            </article>
          </section>

          <section class="hint">
            Bound address:
            <code>{escaped_bind_address}</code>
            <br />
            Current entry:
            <code>{escaped_base_url}</code>
            <br />
            Agent base URLs:
            <ul>{agent_base_link_items}</ul>
            If the default port is occupied, the server automatically moves to the next free port.
          </section>

          <section class="card playground">
            <h2>Quick Test</h2>
            <p>
              Send a browser-side test request to
              <code>/v1/chat/completions</code>, including selectable reasoning effort,
              and inspect the response below.
            </p>

            <form id="playground-form">
              <div class="playground-grid">
                <div>
                  <label for="model">Model</label>
                  <select id="model" name="model">{model_options}</select>
                </div>
                <div>
                  <label for="reasoning-effort">Reasoning Effort</label>
                  <select id="reasoning-effort" name="reasoning_effort">
                    <option value="low">low</option>
                    <option value="medium" selected>medium</option>
                    <option value="high">high</option>
                    <option value="extra high">extra high</option>
                  </select>
                </div>
                <div>
                  <label for="system-prompt">System Prompt</label>
                  <textarea id="system-prompt" name="system_prompt" placeholder="Optional system prompt">You are a helpful assistant.</textarea>
                </div>
              </div>

              <div class="playground-grid">
                <div style="grid-column: 1 / -1;">
                  <label for="user-prompt">User Prompt</label>
                  <textarea id="user-prompt" name="user_prompt" placeholder="Type a prompt here">Reply with exactly: bridge ok</textarea>
                </div>
              </div>

              <div class="playground-actions">
                <button id="submit-btn" type="submit">Send Test Request</button>
                <button id="stream-btn" type="button">Stream Test</button>
              </div>
            </form>

            <div class="output-panel">
              <p id="result-status" class="status">Ready.</p>
              <pre id="result-body">Submit the form to see the response here.</pre>
            </div>
          </section>
        </main>

        <script>
          const form = document.getElementById("playground-form");
          const submitBtn = document.getElementById("submit-btn");
          const streamBtn = document.getElementById("stream-btn");
          const resultStatus = document.getElementById("result-status");
          const resultBody = document.getElementById("result-body");

          function buildPayload(stream) {{
            const model = document.getElementById("model").value;
            const reasoningEffort = document.getElementById("reasoning-effort").value;
            const systemPrompt = document.getElementById("system-prompt").value.trim();
            const userPrompt = document.getElementById("user-prompt").value.trim();

            if (!userPrompt) {{
              resultStatus.textContent = "Please enter a user prompt.";
              resultBody.textContent = "";
              return null;
            }}

            const messages = [];
            if (systemPrompt) {{
              messages.push({{ role: "system", content: systemPrompt }});
            }}
            messages.push({{ role: "user", content: userPrompt }});

            const payload = {{
              model,
              stream,
              messages,
            }};

            if (reasoningEffort) {{
              payload.reasoning_effort = reasoningEffort;
            }}

            return payload;
          }}

          function setBusy(isBusy) {{
            submitBtn.disabled = isBusy;
            streamBtn.disabled = isBusy;
          }}

          async function runJsonTest(event) {{
            event.preventDefault();

            const payload = buildPayload(false);
            if (!payload) {{
              return;
            }}

            setBusy(true);
            resultStatus.textContent = "Sending request...";
            resultBody.textContent = JSON.stringify(payload, null, 2);

            const startedAt = performance.now();

            try {{
              const response = await fetch("/v1/chat/completions", {{
                method: "POST",
                headers: {{
                  "Content-Type": "application/json",
                }},
                body: JSON.stringify(payload),
              }});

              const text = await response.text();
              let parsed;
              try {{
                parsed = JSON.parse(text);
              }} catch {{
                parsed = text;
              }}

              const elapsed = ((performance.now() - startedAt) / 1000).toFixed(2);
              resultStatus.textContent = `HTTP ${{response.status}} in ${{elapsed}}s`;
              resultBody.textContent = typeof parsed === "string"
                ? parsed
                : JSON.stringify(parsed, null, 2);
            }} catch (error) {{
              resultStatus.textContent = "Request failed";
              resultBody.textContent = String(error);
            }} finally {{
              setBusy(false);
            }}
          }}

          async function runStreamTest() {{
            const payload = buildPayload(true);
            if (!payload) {{
              return;
            }}

            setBusy(true);
            resultStatus.textContent = "Opening stream...";
            resultBody.textContent = "";

            const startedAt = performance.now();

            try {{
              const response = await fetch("/v1/chat/completions", {{
                method: "POST",
                headers: {{
                  "Content-Type": "application/json",
                }},
                body: JSON.stringify(payload),
              }});

              if (!response.ok || !response.body) {{
                const text = await response.text();
                resultStatus.textContent = `HTTP ${{response.status}}`;
                resultBody.textContent = text;
                return;
              }}

              resultStatus.textContent = `HTTP ${{response.status}} stream opened`;

              const reader = response.body.getReader();
              const decoder = new TextDecoder();
              let buffer = "";
              let streamedText = "";

              while (true) {{
                const {{ value, done }} = await reader.read();
                if (done) {{
                  break;
                }}

                buffer += decoder.decode(value, {{ stream: true }});
                const events = buffer.split("\\n\\n");
                buffer = events.pop() || "";

                for (const eventBlock of events) {{
                  const lines = eventBlock.split("\\n");
                  let dataPayload = "";

                  for (const line of lines) {{
                    if (line.startsWith("data: ")) {{
                      dataPayload += line.slice(6);
                    }}
                  }}

                  if (!dataPayload) {{
                    continue;
                  }}

                  if (dataPayload === "[DONE]") {{
                    const elapsed = ((performance.now() - startedAt) / 1000).toFixed(2);
                    resultStatus.textContent = `Stream completed in ${{elapsed}}s`;
                    continue;
                  }}

                  let parsed;
                  try {{
                    parsed = JSON.parse(dataPayload);
                  }} catch {{
                    resultBody.textContent += dataPayload;
                    continue;
                  }}

                  if (parsed.error) {{
                    resultStatus.textContent = "Stream failed";
                    resultBody.textContent = JSON.stringify(parsed, null, 2);
                    continue;
                  }}

                  const delta = parsed.choices?.[0]?.delta?.content || "";
                  if (delta) {{
                    streamedText += delta;
                    resultBody.textContent = streamedText;
                  }}
                }}
              }}

              if (!resultBody.textContent) {{
                resultBody.textContent = "(stream ended with no visible text)";
              }}
            }} catch (error) {{
              resultStatus.textContent = "Stream request failed";
              resultBody.textContent = String(error);
            }} finally {{
              setBusy(false);
            }}
          }}

          form.addEventListener("submit", runJsonTest);
          streamBtn.addEventListener("click", runStreamTest);
        </script>
      </body>
    </html>
    """


@app.get("/health")
async def health(request: Request):
    access_urls = resolve_access_urls(
        bind_host=ACTIVE_HOST,
        port=resolve_request_port(request),
        preferred_host=request.url.hostname,
        scheme=request.url.scheme or "http",
    )
    return {
        "status": "ok",
        "service": app.title,
        "listen": {
            "host": ACTIVE_HOST,
            "port": resolve_request_port(request),
            "access_urls": access_urls,
        },
        "models": [model["id"] for model in AVAILABLE_MODELS],
    }


@app.get("/routes")
async def routes():
    return {
        "routes": [
            {"method": "GET", "path": "/"},
            {"method": "GET", "path": "/v1"},
            {"method": "GET", "path": "/health"},
            {"method": "GET", "path": "/routes"},
            {"method": "GET", "path": "/models"},
            {"method": "GET", "path": "/v1/models"},
            {"method": "POST", "path": "/chat/completions"},
            {"method": "POST", "path": "/v1/chat/completions"},
        ]
    }


@app.get("/v1")
async def api_index(request: Request):
    return {
        "service": app.title,
        "base_url": "/v1",
        "agent_base_urls": resolve_agent_base_urls(request),
        "reasoning_efforts": ["low", "medium", "high", "xhigh"],
        "listen": {
            "host": ACTIVE_HOST,
            "port": resolve_request_port(request),
        },
        "routes": {
            "models": "/v1/models",
            "chat_completions": "/v1/chat/completions",
        },
        "browser_routes": {
            "home": "/",
            "health": "/health",
            "routes": "/routes",
            "models": "/models",
        },
    }


@app.get("/models")
async def models_alias():
    return await list_models()


@app.post("/chat/completions")
async def chat_completions_alias(request: ChatCompletionRequest):
    return await chat_completions(request)


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": AVAILABLE_MODELS
    }

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    if request.stream:
        return StreamingResponse(
            bridge.chat_completion_stream(request), 
            media_type="text/event-stream"
        )
    else:
        result, upstream_error = await bridge.chat_completion(request)
        if upstream_error:
            raise HTTPException(status_code=502, detail=upstream_error)
        
        return {
            "id": result["id"],
            "object": "chat.completion",
            "created": result["created"],
            "model": result["model"],
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": result["content"],
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": result["usage"]
        }

if __name__ == "__main__":
    args = parse_cli_args()
    # Ensure token exists or prompt user before starting the async loop
    ensure_authenticated(auth_method_override=args.auth)
    port = resolve_bind_port(settings.host, settings.port)
    ACTIVE_PORT = port
    if port != settings.port:
        print(f"[*] Port {settings.port} 已被占用，自动切换到 http://{settings.host}:{port}")
    else:
        print(f"[*] Service starting on http://{settings.host}:{port}")
    uvicorn.run(app, host=settings.host, port=port)
