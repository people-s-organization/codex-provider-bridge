import argparse
import html
import json
import socket
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from config import settings
from model_registry import available_model_ids, available_models, default_model_id
from schemas import AudioSpeechRequest, ChatCompletionRequest, ImageGenerationRequest, ResponsesRequest
from bridge import bridge
from auth import ensure_authenticated

app = FastAPI(title="Codex Provider Bridge")
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
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
    models = available_models()
    default_model = default_model_id()
    default_model_json = json.dumps(default_model).replace("</", "<\\/")

    model_links = "".join(
        f"<li><code>{html.escape(model['id'])}</code></li>"
        for model in models
    )
    model_options = "".join(
        f"<option value=\"{html.escape(model['id'], quote=True)}\"></option>"
        for model in models
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
            --bg: #f6f4ef;
            --panel: #ffffff;
            --panel-subtle: #f8fafc;
            --panel-border: #d7d2c8;
            --text: #172026;
            --muted: #64748b;
            --accent: #0f766e;
            --accent-2: #9a3412;
            --danger: #b42318;
          }}
          * {{
            box-sizing: border-box;
          }}
          body {{
            margin: 0;
            min-height: 100vh;
            font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: var(--bg);
            color: var(--text);
          }}
          .wrap {{
            max-width: 1120px;
            margin: 0 auto;
            padding: 32px 20px 56px;
          }}
          .header {{
            border-bottom: 1px solid var(--panel-border);
            padding-bottom: 20px;
          }}
          h1 {{
            margin: 0 0 10px;
            font-size: 2rem;
            line-height: 1.15;
            letter-spacing: 0;
          }}
          p {{
            color: var(--muted);
            font-size: 0.98rem;
            line-height: 1.6;
          }}
          .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
            gap: 12px;
            margin-top: 18px;
          }}
          .card {{
            background: var(--panel);
            border: 1px solid var(--panel-border);
            border-radius: 8px;
            padding: 16px;
          }}
          .card h2 {{
            margin: 0 0 10px;
            font-size: 1rem;
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
            margin-top: 18px;
            padding: 14px 16px;
            border-radius: 8px;
            border: 1px solid color-mix(in srgb, var(--accent) 32%, var(--panel-border));
            background: color-mix(in srgb, var(--accent) 8%, var(--panel));
          }}
          .playground {{
            margin-top: 22px;
            background: var(--panel);
          }}
          .playground-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
            gap: 12px;
            margin-top: 14px;
          }}
          .full-width {{
            grid-column: 1 / -1;
          }}
          label {{
            display: block;
            margin-bottom: 6px;
            color: var(--muted);
            font-size: 0.9rem;
            font-weight: 600;
          }}
          select, input, textarea, button {{
            width: 100%;
            border-radius: 8px;
            border: 1px solid var(--panel-border);
            background: var(--panel-subtle);
            color: var(--text);
            padding: 10px 12px;
            font: inherit;
          }}
          textarea {{
            min-height: 120px;
            resize: vertical;
          }}
          button {{
            cursor: pointer;
            font-weight: 600;
            background: var(--accent);
            border-color: var(--accent);
            color: white;
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
            min-width: 160px;
          }}
          .inline-field {{
            align-items: end;
            display: flex;
            gap: 10px;
          }}
          .inline-field input {{
            flex: 1;
          }}
          .inline-field output {{
            color: var(--muted);
            min-width: 44px;
            text-align: right;
          }}
          .output-panel {{
            margin-top: 16px;
            padding: 14px;
            border-radius: 8px;
            border: 1px solid var(--panel-border);
            background: var(--panel-subtle);
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
          .preview {{
            display: none;
            margin-top: 14px;
          }}
          .preview img {{
            image-rendering: pixelated;
            max-width: 180px;
            width: 100%;
            border: 1px solid var(--panel-border);
            border-radius: 8px;
            background: repeating-conic-gradient(#e2e8f0 0 25%, #fff 0 50%) 50% / 18px 18px;
          }}
          .preview audio {{
            width: 100%;
          }}
          [hidden] {{
            display: none !important;
          }}
          @media (prefers-color-scheme: dark) {{
            :root {{
              --bg: #11100e;
              --panel: #1c1b18;
              --panel-subtle: #151413;
              --panel-border: #3f3a34;
              --text: #f5f2ec;
              --muted: #b7afa4;
              --accent: #2dd4bf;
              --accent-2: #f59e0b;
              --danger: #f87171;
            }}
          }}
        </style>
      </head>
      <body>
        <main class="wrap">
          <header class="header">
            <h1>Codex Provider Bridge</h1>
            <p>
              OpenAI-compatible routes backed by the ChatGPT/Codex response stream.
              Point agents to a base URL ending in <code>/v1</code>.
            </p>
          </header>

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
              <h2>Text</h2>
              <p><code>POST /v1/chat/completions</code></p>
              <p><code>POST /chat/completions</code></p>
              <p><code>POST /v1/responses</code></p>
              <p><code>POST /responses</code></p>
            </article>

            <article class="card">
              <h2>Media</h2>
              <p><code>POST /v1/images/generations</code></p>
              <p><code>POST /images/generations</code></p>
              <p><code>POST /v1/audio/speech</code></p>
              <p><code>POST /audio/speech</code></p>
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
            <h2>Endpoint Test</h2>
            <p>Selected route: <code id="endpoint-path">/v1/chat/completions</code></p>

            <form id="playground-form">
              <div class="playground-grid">
                <div>
                  <label for="endpoint">Endpoint</label>
                  <select id="endpoint" name="endpoint">
                    <option value="chat">Chat Completions</option>
                    <option value="responses">Responses</option>
                    <option value="images">Images Generations</option>
                    <option value="audio">Audio Speech</option>
                  </select>
                </div>
                <div>
                  <label for="model">Model</label>
                  <input id="model" name="model" list="available-models" value="{html.escape(default_model, quote=True)}" />
                  <datalist id="available-models">{model_options}</datalist>
                </div>
                <div id="reasoning-field">
                  <label for="reasoning-effort">Reasoning Effort</label>
                  <select id="reasoning-effort" name="reasoning_effort">
                    <option value="low">low</option>
                    <option value="medium" selected>medium</option>
                    <option value="high">high</option>
                    <option value="extra high">extra high</option>
                  </select>
                </div>
                <div id="image-field" hidden>
                  <label for="image-size">Image Size</label>
                  <select id="image-size" name="image_size">
                    <option value="1024x1024">1024x1024</option>
                    <option value="1024x1536">1024x1536</option>
                    <option value="1536x1024">1536x1024</option>
                    <option value="auto">auto</option>
                  </select>
                </div>
                <div id="image-quality-field" hidden>
                  <label for="image-quality">Image Quality</label>
                  <select id="image-quality" name="image_quality">
                    <option value="auto">auto</option>
                    <option value="low">low</option>
                    <option value="medium">medium</option>
                    <option value="high">high</option>
                  </select>
                </div>
                <div id="audio-format-field" hidden>
                  <label for="audio-format">Audio Format</label>
                  <select id="audio-format" name="audio_format">
                    <option value="mp3">mp3</option>
                    <option value="wav">wav</option>
                    <option value="aac">aac</option>
                  </select>
                </div>
                <div id="audio-speed-field" hidden>
                  <label for="audio-speed">Audio Speed</label>
                  <div class="inline-field">
                    <input id="audio-speed" name="audio_speed" type="range" min="0.5" max="2" step="0.1" value="1" />
                    <output id="audio-speed-value" for="audio-speed">1.0x</output>
                  </div>
                </div>
                <div id="system-field" class="full-width">
                  <label for="system-prompt">System Prompt</label>
                  <textarea id="system-prompt" name="system_prompt" placeholder="Optional system prompt">You are a helpful assistant.</textarea>
                </div>
                <div id="schema-field" class="full-width" hidden>
                  <label for="json-schema">JSON Schema</label>
                  <textarea id="json-schema" name="json_schema">{{
  "type": "object",
  "properties": {{
    "summary": {{"type": "string"}}
  }},
  "required": ["summary"],
  "additionalProperties": false
}}</textarea>
                </div>
                <div class="full-width">
                  <label for="user-prompt" id="prompt-label">User Prompt</label>
                  <textarea id="user-prompt" name="user_prompt" placeholder="Type a prompt here">Reply with exactly: bridge ok</textarea>
                </div>
              </div>

              <div class="playground-actions">
                <button id="submit-btn" type="submit">Send Request</button>
                <button id="stream-btn" type="button">Stream Test</button>
              </div>
            </form>

            <div id="image-preview" class="preview">
              <img id="image-output" alt="Generated image preview" />
            </div>
            <div id="audio-preview" class="preview">
              <audio id="audio-output" controls></audio>
            </div>
            <div class="output-panel">
              <p id="result-status" class="status">Ready.</p>
              <pre id="result-body">Submit the form to see the response here.</pre>
            </div>
          </section>
        </main>

        <script>
          const form = document.getElementById("playground-form");
          const endpointSelect = document.getElementById("endpoint");
          const endpointPath = document.getElementById("endpoint-path");
          const modelInput = document.getElementById("model");
          const reasoningField = document.getElementById("reasoning-field");
          const imageField = document.getElementById("image-field");
          const imageQualityField = document.getElementById("image-quality-field");
          const audioFormatField = document.getElementById("audio-format-field");
          const audioSpeedField = document.getElementById("audio-speed-field");
          const systemField = document.getElementById("system-field");
          const schemaField = document.getElementById("schema-field");
          const promptLabel = document.getElementById("prompt-label");
          const imagePreview = document.getElementById("image-preview");
          const imageOutput = document.getElementById("image-output");
          const audioPreview = document.getElementById("audio-preview");
          const audioOutput = document.getElementById("audio-output");
          const audioSpeed = document.getElementById("audio-speed");
          const audioSpeedValue = document.getElementById("audio-speed-value");
          const submitBtn = document.getElementById("submit-btn");
          const streamBtn = document.getElementById("stream-btn");
          const resultStatus = document.getElementById("result-status");
          const resultBody = document.getElementById("result-body");
          let audioObjectUrl = null;

          const endpointConfigs = {{
            chat: {{
              path: "/v1/chat/completions",
              model: {default_model_json},
              prompt: "Reply with exactly: bridge ok",
              promptLabel: "User Prompt",
              reasoning: true,
              system: true,
              schema: false,
              image: false,
              audio: false,
              stream: true,
            }},
            responses: {{
              path: "/v1/responses",
              model: {default_model_json},
              prompt: "Return a JSON object with a short summary of this bridge.",
              promptLabel: "Input Text",
              reasoning: true,
              system: true,
              schema: true,
              image: false,
              audio: false,
              stream: false,
            }},
            images: {{
              path: "/v1/images/generations",
              model: "gpt-image-2",
              prompt: "A clean product-style diagram of a local API bridge",
              promptLabel: "Image Prompt",
              reasoning: false,
              system: false,
              schema: false,
              image: true,
              audio: false,
              stream: false,
            }},
            audio: {{
              path: "/v1/audio/speech",
              model: "gpt-4o-mini-tts",
              prompt: "Bridge audio test.",
              promptLabel: "Speech Input",
              reasoning: false,
              system: false,
              schema: false,
              image: false,
              audio: true,
              stream: false,
            }},
          }};

          function clearPreview() {{
            imagePreview.style.display = "none";
            imageOutput.removeAttribute("src");
            audioPreview.style.display = "none";
            audioOutput.removeAttribute("src");
            if (audioObjectUrl) {{
              URL.revokeObjectURL(audioObjectUrl);
              audioObjectUrl = null;
            }}
          }}

          function syncEndpoint() {{
            const config = endpointConfigs[endpointSelect.value];
            endpointPath.textContent = config.path;
            modelInput.value = config.model;
            document.getElementById("user-prompt").value = config.prompt;
            promptLabel.textContent = config.promptLabel;
            reasoningField.hidden = !config.reasoning;
            systemField.hidden = !config.system;
            schemaField.hidden = !config.schema;
            imageField.hidden = !config.image;
            imageQualityField.hidden = !config.image;
            audioFormatField.hidden = !config.audio;
            audioSpeedField.hidden = !config.audio;
            streamBtn.hidden = !config.stream;
            clearPreview();
            resultStatus.textContent = "Ready.";
            resultBody.textContent = "Submit the form to see the response here.";
          }}

          function buildRequest(stream) {{
            const endpoint = endpointSelect.value;
            const config = endpointConfigs[endpoint];
            const model = modelInput.value.trim() || config.model;
            const reasoningEffort = document.getElementById("reasoning-effort").value;
            const systemPrompt = document.getElementById("system-prompt").value.trim();
            const userPrompt = document.getElementById("user-prompt").value.trim();

            if (!userPrompt) {{
              resultStatus.textContent = "Please enter a prompt.";
              resultBody.textContent = "";
              return null;
            }}

            if (endpoint === "images") {{
              return {{
                path: config.path,
                responseType: "json",
                payload: {{
                  model,
                  prompt: userPrompt,
                  size: document.getElementById("image-size").value,
                  quality: document.getElementById("image-quality").value,
                  response_format: "b64_json",
                }},
              }};
            }}

            if (endpoint === "audio") {{
              return {{
                path: config.path,
                responseType: "audio",
                payload: {{
                  model,
                  input: userPrompt,
                  voice: "marin",
                  response_format: document.getElementById("audio-format").value,
                  speed: Number(audioSpeed.value),
                }},
              }};
            }}

            if (endpoint === "responses") {{
              const payload = {{
                model,
                input: [
                  {{
                    role: "user",
                    content: [{{ type: "input_text", text: userPrompt }}],
                  }},
                ],
              }};

              if (systemPrompt) {{
                payload.instructions = systemPrompt;
              }}
              if (reasoningEffort) {{
                payload.reasoning = {{ effort: reasoningEffort }};
              }}

              const schemaText = document.getElementById("json-schema").value.trim();
              if (schemaText) {{
                try {{
                  payload.text = {{
                    format: {{
                      type: "json_schema",
                      name: "playground_response",
                      schema: JSON.parse(schemaText),
                      strict: true,
                    }},
                  }};
                }} catch (error) {{
                  resultStatus.textContent = "JSON schema is invalid";
                  resultBody.textContent = String(error);
                  return null;
                }}
              }}

              return {{ path: config.path, responseType: "json", payload }};
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

            return {{ path: config.path, responseType: "json", payload }};
          }}

          function setBusy(isBusy) {{
            submitBtn.disabled = isBusy;
            streamBtn.disabled = isBusy;
          }}

          async function runJsonTest(event) {{
            event.preventDefault();

            const request = buildRequest(false);
            if (!request) {{
              return;
            }}

            setBusy(true);
            clearPreview();
            resultStatus.textContent = "Sending request...";
            resultBody.textContent = JSON.stringify(request.payload, null, 2);

            const startedAt = performance.now();

            try {{
              const response = await fetch(request.path, {{
                method: "POST",
                headers: {{
                  "Content-Type": "application/json",
                }},
                body: JSON.stringify(request.payload),
              }});

              const elapsed = ((performance.now() - startedAt) / 1000).toFixed(2);
              if (request.responseType === "audio") {{
                const blob = await response.blob();
                if (response.ok) {{
                  audioObjectUrl = URL.createObjectURL(blob);
                  audioOutput.src = audioObjectUrl;
                  audioPreview.style.display = "block";
                }}
                resultStatus.textContent = `HTTP ${{response.status}} in ${{elapsed}}s`;
                resultBody.textContent = JSON.stringify({{
                  content_type: response.headers.get("content-type"),
                  bytes: blob.size,
                }}, null, 2);
                return;
              }}

              const text = await response.text();
              let parsed;
              try {{
                parsed = JSON.parse(text);
              }} catch {{
                parsed = text;
              }}

              resultStatus.textContent = `HTTP ${{response.status}} in ${{elapsed}}s`;
              if (endpointSelect.value === "images" && parsed?.data?.[0]?.b64_json) {{
                imageOutput.src = `data:image/png;base64,${{parsed.data[0].b64_json}}`;
                imagePreview.style.display = "block";
              }}
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
            const request = buildRequest(true);
            if (!request) {{
              return;
            }}

            setBusy(true);
            clearPreview();
            resultStatus.textContent = "Opening stream...";
            resultBody.textContent = "";

            const startedAt = performance.now();

            try {{
              const response = await fetch(request.path, {{
                method: "POST",
                headers: {{
                  "Content-Type": "application/json",
                }},
                body: JSON.stringify(request.payload),
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
          endpointSelect.addEventListener("change", syncEndpoint);
          audioSpeed.addEventListener("input", () => {{
            audioSpeedValue.textContent = `${{Number(audioSpeed.value).toFixed(1)}}x`;
          }});
          streamBtn.addEventListener("click", runStreamTest);
          syncEndpoint();
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
        "models": available_model_ids(),
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
            {"method": "POST", "path": "/responses"},
            {"method": "POST", "path": "/v1/responses"},
            {"method": "POST", "path": "/chat/completions"},
            {"method": "POST", "path": "/v1/chat/completions"},
            {"method": "POST", "path": "/images/generations"},
            {"method": "POST", "path": "/v1/images/generations"},
            {"method": "POST", "path": "/audio/speech"},
            {"method": "POST", "path": "/v1/audio/speech"},
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
            "responses": "/v1/responses",
            "chat_completions": "/v1/chat/completions",
            "images_generations": "/v1/images/generations",
            "audio_speech": "/v1/audio/speech",
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


@app.post("/responses")
async def responses_alias(request: ResponsesRequest):
    return await responses(request)


@app.post("/chat/completions")
async def chat_completions_alias(request: ChatCompletionRequest):
    return await chat_completions(request)


@app.post("/images/generations")
async def image_generations_alias(request: ImageGenerationRequest):
    return await image_generations(request)


@app.post("/audio/speech")
async def audio_speech_alias(request: AudioSpeechRequest):
    return await audio_speech(request)


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": available_models()
    }


@app.post("/v1/responses")
async def responses(request: ResponsesRequest):
    if request.stream:
        raise HTTPException(status_code=501, detail="Streaming /v1/responses is not implemented yet")

    result, upstream_error = await bridge.responses(request)
    if upstream_error:
        raise HTTPException(status_code=502, detail=upstream_error)
    return result

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


@app.post("/v1/images/generations")
async def image_generations(request: ImageGenerationRequest):
    result, upstream_error = await bridge.image_generation(request)
    if upstream_error:
        raise HTTPException(status_code=upstream_error.get("status", 502), detail=upstream_error)
    return result


@app.post("/v1/audio/speech")
async def audio_speech(request: AudioSpeechRequest):
    audio_bytes, media_type, upstream_error = await bridge.synthesize_speech(request)
    if upstream_error:
        raise HTTPException(status_code=upstream_error.get("status", 502), detail=upstream_error)
    return Response(content=audio_bytes, media_type=media_type)

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
