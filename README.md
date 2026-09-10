# Codex Provider Bridge

将 ChatGPT Web / Codex 登录态桥接为尽量兼容 OpenAI 的本地接口，供 OpenClaw、Hermes、Claude Code 等 Agent 使用。

项目会对接 ChatGPT 的 `codex/responses` 通道，并暴露常见的 OpenAI 风格路由：

- `GET /v1/models`
- `POST /v1/completions`
- `POST /v1/responses`
- `POST /v1/chat/completions`
- `POST /v1/images/generations`
- `POST /v1/audio/speech`
- 对应的非 `/v1` 别名：`/completions`、`/responses`、`/chat/completions`、`/images/generations`、`/audio/speech`
- 未内置实现的 `/v1/*` 路由：如果配置了 `OPENAI_API_KEY`，会转发到官方 OpenAI API；否则返回 OpenAI 风格 `501` 错误并说明原因
- `GET /`
- `GET /health`
- `GET /routes`

## 适用场景

- 本地桌面环境：可用浏览器登录或已有 token 直接启动
- 云主机 / SSH：推荐使用已有 token、`~/.codex/auth.json`，或纯 HTTP 的 device-code 登录
- 局域网共享：服务默认监听 `0.0.0.0`，首页会显示本机和局域网可访问地址

## 认证方式

启动时按下面顺序尝试认证：

1. `.env` 中的 `CHATGPT_ACCESS_TOKEN`
2. `~/.codex/auth.json` 中的 `access_token`
3. 浏览器登录
4. device-code 登录

你可以通过命令行参数 `--auth` 控制偏好：

- `prompt`：默认值；在 1 和 2 都不存在时，启动时询问你使用浏览器登录还是 device-code
- `auto`：自动行为；在交互式终端中，如果 1 和 2 都不存在，也会让你手动选择 3 或 4
- `browser`：优先浏览器登录
- `device`：直接走 device-code，适合云主机

如果你不传 `--auth`，默认就是 `prompt`。  
同时也保留 `CHATGPT_AUTH_METHOD` 环境变量作为兼容兜底，但命令行参数优先级更高。

在 Linux 无图形环境中，如果当前不是交互式终端，`prompt` 会自动退化为推荐方式，通常是 device-code。

## 快速开始

### 方式一：一键启动

```bash
chmod +x start.sh
./start.sh
```

`start.sh` 会自动：

1. 创建 `.venv`
2. 安装依赖
3. 启动服务

如果你还需要桌面浏览器登录，可在首次启动前安装 Playwright 浏览器：

```bash
INSTALL_PLAYWRIGHT_BROWSER=1 ./start.sh
```

如果你想显式指定认证方式：

```bash
./start.sh --auth prompt
./start.sh --auth browser
./start.sh --auth device
```

### 方式二：手动启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py --auth prompt
```

## 云主机部署建议

云主机上最推荐的顺序是：

1. 直接写入 `CHATGPT_ACCESS_TOKEN`
2. 或复制 `~/.codex/auth.json`
3. 或显式使用 `--auth device`

示例：

```bash
cp .env.example .env
./start.sh --auth device
```

启动后终端会输出授权链接和授权码。你在自己的本地浏览器中打开链接、输入授权码，云主机这边会持续轮询并保存 token 到 `.env`。

如果你是通过 SSH 交互式启动，并且 `.env` / `~/.codex/auth.json` 都没有可用 token，那么默认的 `--auth prompt` 会先问你：

- 3. 浏览器登录
- 4. device-code 登录

你可以当场手动选择。

## 本地桌面部署建议

如果你本机有浏览器环境，可以直接：

```bash
./start.sh
```

没有现成 token 时，程序会尝试弹出浏览器登录。若浏览器登录失败，也会自动回退到 device-code。

## 配置项

可参考 [.env.example](./.env.example)：

```env
CHATGPT_ACCESS_TOKEN=
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.openai.com
CHATGPT_ACCOUNT_ID=
CHATGPT_AUTH_METHOD=prompt
HOST=0.0.0.0
PORT=8000
CHATGPT_BASE_URL=https://chatgpt.com
CHATGPT_MODELS=
CHATGPT_EXTRA_MODELS=
CHATGPT_DEFAULT_MODEL=
CHATGPT_MEDIA_MODEL=
CHATGPT_REALTIME_MODEL=
CHATGPT_MODELS_FILE=~/.codex/models_cache.json
CHATGPT_CODEX_CONFIG_FILE=~/.codex/config.toml
CHATGPT_MODELS_URL=
CHATGPT_CAPABILITIES_URL=
CHATGPT_MODEL_ALIASES=
CHATGPT_EXTRA_MODEL_ALIASES=
BRIDGE_RESPONSE_STORE_MAX=256
BRIDGE_RESPONSE_STORE_TTL=3600
BRIDGE_CORS_ORIGINS=*
STREAM_KEEPALIVE_SECONDS=15
```

说明：

- `CHATGPT_ACCESS_TOKEN`：如果你已经有 token，填上后可直接启动
- `OPENAI_API_KEY`：可选；图片和语音都优先走 ChatGPT/Codex 能力，只有对应能力失败或缺 token 时才用它兜底；未内置实现的 `/v1/*` 路由也会用它代理到官方 OpenAI API
- `OPENAI_BASE_URL`：OpenAI API 地址，默认是 `https://api.openai.com`
- `CHATGPT_ACCOUNT_ID`：可选；默认会从 `~/.codex/auth.json` 读取，realtime 语音握手会带上它
- `CHATGPT_AUTH_METHOD`：兼容性兜底配置；可选 `prompt | auto | browser | device`
- `HOST` / `PORT`：服务监听地址
- `CHATGPT_BASE_URL`：默认是 `https://chatgpt.com`
- `CHATGPT_MODELS`：显式指定 `/v1/models` 返回的模型列表，支持逗号分隔或 JSON 数组；设置后不再读取缓存和上游
- `CHATGPT_EXTRA_MODELS`：在自动模型列表后追加模型，例如刚发布但本地缓存还没刷新的模型
- `CHATGPT_DEFAULT_MODEL`：首页测试表单默认选中的模型；不设置时用 `~/.codex/config.toml` 里的 `model`，再退到当前列表第一个
- `CHATGPT_MEDIA_MODEL`：图片接口调用 Codex `image_generation` 工具时使用的上游 Responses 模型；不设置时按 `CHATGPT_DEFAULT_MODEL` → 请求里真实存在的 `model` → 探测到的默认模型解析（`gpt-image-*` 这类图片模型名不会被当成 Responses 模型，上游会直接拒绝）
- `CHATGPT_REALTIME_MODEL`：语音接口调用 realtime WebSocket 时使用的模型；默认用请求里的 `model`
- `CHATGPT_MODELS_FILE`：Codex 模型缓存路径，默认读取 `~/.codex/models_cache.json`
- `CHATGPT_CODEX_CONFIG_FILE`：Codex 配置路径，默认读取 `~/.codex/config.toml` 中的 `model`
- `CHATGPT_MODELS_URL`：缓存为空时向上游查询模型列表的地址，默认 `$CHATGPT_BASE_URL/backend-api/codex/models`；设为空值即关闭上游查询
- `CHATGPT_CAPABILITIES_URL`：能力探测地址（ChatGPT web 模型列表，用 `enabled_tools` 标记图片工具），默认 `$CHATGPT_BASE_URL/backend-api/models`；设为空值即关闭能力探测
- `CHATGPT_MODEL_ALIASES` / `CHATGPT_EXTRA_MODEL_ALIASES`：模型别名映射，支持 JSON 对象或 `old=new,old2=new2`；默认不含任何别名
- `BRIDGE_RESPONSE_STORE_MAX` / `BRIDGE_RESPONSE_STORE_TTL`：桥自存的 Responses 数量上限与过期秒数，仅进程内存储
- `BRIDGE_CORS_ORIGINS`：允许的浏览器来源，逗号分隔；`*` 表示任意来源（此时不允许携带凭据）
- `STREAM_KEEPALIVE_SECONDS`：SSE 空闲多少秒后发 `: ping` 注释行，0 关闭

## 使用方式

启动后打开首页：

- `http://127.0.0.1:8000/`
- 或首页展示的实际地址

如果默认端口被占用，程序会自动切到附近空闲端口，首页和 `/health` 都会展示最终监听地址与可访问 URL。

给 Agent 配置时，将 Base URL 指向：

```text
http://<your-host>:<port>/v1
```

API Key 一般可以随便填一个占位值，是否必须填写取决于你的 Agent 客户端。

运维参数、严格兼容模式、回滚部署与真实 Agent 验收见 [OPERATIONS.md](OPERATIONS.md)。

## 接口兼容范围

### OpenAI API 端点对照

| 端点 | 状态 | 说明 |
|---|---|---|
| `GET /v1/models` | ✅ | 真实发现结果，无写死列表、无兜底 |
| `GET /v1/models/{id}` | ✅ | 命中返回 model 对象，未命中 `404 model_not_found` |
| `POST /v1/chat/completions` | ✅ | 文本、流式、工具调用、图片输入 |
| `POST /v1/responses` | ✅ | 文本、流式、工具调用、`store`、`previous_response_id` |
| `GET /v1/responses/{id}` | ✅ | 读取桥自存的响应（进程内，见下） |
| `DELETE /v1/responses/{id}` | ✅ | 删除桥自存的响应 |
| `POST /v1/completions` | ✅ | 旧 Completions 形状，映射到一次文本调用 |
| `POST /v1/images/generations` | ✅ | Codex `image_generation` 工具；无凭据时 `501` |
| `POST /v1/audio/speech` | ✅ | Codex realtime；无凭据时 `501` |
| `POST /v1/responses/input_tokens` | ❌ | 订阅通道不提供 tokenizer，不返回编造的数字 |
| `POST /v1/embeddings` / `moderations` | ❌ | 订阅通道不暴露这两个能力；配 `OPENAI_API_KEY` 时按原样代理 |
| `files` / `batches` / `fine_tuning` / `vector_stores` / `assistants` | ❌ | 同上：无凭据时 `501 unsupported_endpoint`，有凭据时代理 |
| `audio/transcriptions`、`audio/translations`、`images/edits`、`images/variations` | ❌ | 同上 |

❌ 的端点不会返回假数据：没有真实能力就返回带原因的 `501`，配置 `OPENAI_API_KEY` 后原样代理到官方 API。未知路径返回 `404 Invalid URL (...)`，与官方行为一致。

### 协议层一致性

- 请求校验失败返回 **`400`** + `{"error":{"message","type","param","code"}}`，`param` 指向出错字段；错误信息只包含桥自己写的静态文本，不回显请求内容。
- 未知模型返回 **`404 model_not_found`**（仅当模型列表确实可用时判定；发现源为空时交给上游判断）。
- `store` / `previous_response_id`：上游是无状态且拒绝 `store`，所以**桥自己**维护一个有界、过期、仅进程内的响应存储（`BRIDGE_RESPONSE_STORE_MAX`、`BRIDGE_RESPONSE_STORE_TTL`）。`store` 默认 `true`（与官方一致），`store:false` 时不存，之后用该 id 续链会得到 `404 previous_response_not_found`。重启即失效，多进程不共享；需要跨重启持久化就不要依赖它。
- 流式响应是 `text/event-stream`，空闲超过 `STREAM_KEEPALIVE_SECONDS`（默认 15，0 关闭）会发 `: ping` 注释行，避免代理断流；结束时发 `data: [DONE]`。
- 浏览器直连可用：`BRIDGE_CORS_ORIGINS` 控制允许的来源（默认 `*`，配置具体来源时才允许凭据）。
- 401 使用 `{"error":{"code":"invalid_api_key",...}}`，限流类上游错误保留状态码与 `Retry-After`。

### 能力边界（先说清楚）

- ✅ **文本问答**：单轮、多轮、SSE 流式都可用；`reasoning_effort`、JSON schema 约束、旧 Completions 形状都走得通。
- ✅ **函数工具调用（function tool calling）**：`tools` / 旧版 `functions` 会转发给上游，上游流式返回的 `function_call` 会被转换成 OpenAI 形状的 `tool_calls`（chat）或 `function_call` item（responses）；非流式与 SSE 流式、`tool_choice` 的 `auto`/`none`/`required`/指定函数、`parallel_tool_calls` 都支持。
- ⚠️ **桥不执行工具**：它只负责"把模型的调用请求交给客户端、再把客户端的结果带回去"。真正读写文件、跑命令的是你的客户端；historically 这一点最容易误解，所以单独写出来。
- ⚠️ **只支持 `type: "function"` 工具**：其它工具类型（包括混合工具列表里的非函数工具）明确拒绝，不再静默丢弃。
- ⚠️ **输出上限不支持**：默认兼容模式接受这些参数但通过 `X-Bridge-Warnings` 告知未兑现；`BRIDGE_STRICT_COMPATIBILITY=true` 时拒绝。不会截断字符来冒充 token 预算。

验证方式（2026-09-10 实测，都是打真实上游）：

| 场景 | 结果 |
|---|---|
| 非流式 chat + `tools` | `finish_reason: "tool_calls"`，`tool_calls[0].function = {"name":"get_weather","arguments":"{\"city\":\"Paris\"}"}`，`content: null` |
| 流式 chat | 先发 `{"index":0,"id":"call_…","type":"function","function":{"name":…,"arguments":""}}`，再发参数增量分片，最后 `finish_reason: "tool_calls"` |
| 并行调用 | 两个工具各返回一条独立 `tool_calls`，`index` 分别为 0/1 |
| 回填闭环 | 回放 `tool_calls` + `role:"tool"` 结果后，模型给出 `It's currently 18°C and sunny in Paris.` |
| `/v1/responses` | 非流式输出 `function_call` item（`call_id`/`name`/`arguments`），流式透传 `response.function_call_arguments.delta` 等事件 |
| `strict: true` 工具 schema | 上游接受（HTTP 200） |

`/health` 只返回最小健康状态、启动时间和部署 commit，不再触发外网能力探测；详细能力展示在首页（配置密钥后需要鉴权）。函数调用能力标识只表示桥的协议支持，不宣称所有上游模型均实测可用。

### 文本接口

- `/v1/chat/completions`：支持普通响应和 SSE 流式响应；`messages[].content` 支持字符串，也支持常见 text / image content parts
- `/v1/responses`：支持非流式和 SSE 流式；非流式会返回 `output_text`、`output` 和 `usage`
- `/v1/completions`：兼容旧 Completions 形状，会映射成一次 Chat/Responses 文本调用
- `reasoning_effort` 和 `reasoning.effort` 支持 `low` / `medium` / `high` / `xhigh`，也兼容 `extra high`
- `response_format={"type":"json_object"}` 和常见 `json_schema` 会被转换成额外 instructions，引导上游返回纯 JSON
- `/v1/responses` 的 `text.format.type=json_schema` 会被转换成额外 instructions，引导上游返回符合 schema 的纯 JSON
- 多轮对话的 content part 会按角色重新定型：assistant 轮次只发 `output_text` / `refusal`，user 轮次只发 `input_text` / `input_image`。上游对 assistant 轮次收到 `input_text` 会整包报 `Invalid value: 'input_text'`，所以 `/v1/responses` 的 `input` 里客户端自己传的 message item 也会做同样归一化（其他 item 类型原样透传）
- 工具历史严格配对：assistant `tool_calls` → `function_call`，tool 结果 → `function_call_output`。缺失 ID、孤儿结果或重复结果在请求校验阶段报错，不再降级为 user 文本。旧版 `function_call` / `role:"function"` 也按配对语义重放。
- system/developer 保留角色与顺序；Responses reasoning 与原生 item 元数据保留，不把高优先级消息变成 user。
- `previous_response_id` 和 `store:true` 明确拒绝：订阅通道无桥端持久化续链，请客户端回传完整历史。
- 错误响应统一成 OpenAI 风格的 `{ "error": { "message", "type", "param", "code" } }`

尽量兼容但不能完全等价的地方：

- `tool_choice="required"`、指定函数、`parallel_tool_calls` 都已支持（见「能力边界」）；桥只转发调用、不执行工具，工具的真正执行方是客户端。`type` 不是 `function` 的工具（如 `web_search`）会被明确拒绝。
- `n > 1`、`best_of`、`logprobs` 会返回明确错误。原因是上游 Codex responses 通道按 turn 返回单个回答，也不返回 token 级 logprobs。
- `max_tokens` / `max_completion_tokens` / `max_output_tokens` / `truncation` 不转发给上游：实测 Codex responses 通道对这四个参数一律返回 `400 Unsupported parameter`，因此兼容模式不转发并在响应头告警，严格模式返回校验错误。
- Chat Completions 的音频输出 modality 不走这里；请用 `/v1/audio/speech`。音频输入、转写、翻译等端点如果需要完整 OpenAI 行为，请配置 `OPENAI_API_KEY` 走代理。
- Assistants、Files、Batches、Vector Stores、Fine-tuning、Embeddings、Moderations 等未内置端点：有 `OPENAI_API_KEY` 时代理到官方 API；没有时返回 `501 unsupported_endpoint` 并说明 ChatGPT/Codex subscription auth 没有向桥接层暴露对应 REST 能力。

`/v1/models` 不维护任何写死的主列表或兜底列表，读取顺序是：

1. `CHATGPT_MODELS`
2. 未过期的 `$CODEX_HOME/models_cache.json`（默认 `~/.codex`，按文件 mtime，60 秒）
3. 缓存为空或过期时查询上游（`CHATGPT_MODELS_URL`）；按 URL/账号/凭据/真实 client version 隔离缓存并合并并发请求
4. `CHATGPT_EXTRA_MODELS` 追加

如果以上都没有来源，`/v1/models` 返回空列表；首页的模型快照包含来源和失败原因。过期缓存不在刷新失败后冒充可用模型。显式环境配置是用户声明，不等于已调用验证。上游要求的 client_version 从 `CHATGPT_CLIENT_VERSION`、真实缓存或已安装的 `codex --version` 获取，不写死版本。

默认测试模型仅能在实际发现集合中选择（配置不存在的模型不会被加入列表），读取顺序是：

1. `CHATGPT_DEFAULT_MODEL`
2. `~/.codex/config.toml` 中的 `model`
3. 当前模型列表第一个（列表为空时首页不预填模型）

默认不带任何内置别名，别名完全由 `CHATGPT_MODEL_ALIASES` / `CHATGPT_EXTRA_MODEL_ALIASES` 决定。

### 媒体接口

- `/v1/images/generations`：优先使用 ChatGPT/Codex backend 的 Responses `image_generation` 工具，返回 `b64_json`；如果没有 ChatGPT token 但有 `OPENAI_API_KEY`，会兜底代理到 OpenAI Image API
- `/v1/audio/speech`：优先使用 ChatGPT/Codex bearer 直连 OpenAI realtime WebSocket，收集 `response.output_audio.delta` 后返回真实音频；如果没有 ChatGPT token 但有 `OPENAI_API_KEY`，会兜底代理到 OpenAI Speech API
- ChatGPT realtime 输出原生是 24 kHz PCM；`wav` / `pcm` 可直接返回，`mp3` / `aac` / `flac` / `opus` 需要本机 `ffmpeg`
- 如果缺少对应真实上游凭据，媒体接口会返回 `501`，不会返回假图片或假音频
- 媒体接口的 `model` 是必填项，缺失或为空会返回 `422`；bridge 不会替你猜一个模型名。图片走 Codex 通道时，上游 Responses 模型按 `CHATGPT_MEDIA_MODEL` → `CHATGPT_DEFAULT_MODEL` → 请求里真实存在的 `model` → 探测到的默认模型解析，语音走 `CHATGPT_REALTIME_MODEL` → 请求里的 `model`

媒体能力探测（为什么媒体模型不会出现在 `/v1/models` 里）：

- Codex 的模型列表（本地缓存和上游 `/backend-api/codex/models`）**只包含文本模型**，原始响应里没有任何 `gpt-image-*` / `tts-*` / `gpt-realtime-*` 条目；`/backend-api/codex/image_generation/models`、`/v1/realtime/models` 等媒体列表路由全部 404，`api.openai.com/v1/models` 用 ChatGPT 登录态会被 403 拒绝。
- 媒体在上游是**能力/工具**而不是模型：ChatGPT web 的 `/backend-api/models` 用 `enabled_tools` 里的 `image_gen_tool_enabled` / `dalle_3` 标记哪些模型能用图片工具；realtime 语音只接受 `?model=` 参数，省略会直接 `missing_model`。
- 图片能力通过首页的模型快照展示；探测失败或缺少证据时 `available:null/status:unknown`，只有明确工具列表没有图片标记时才是 unsupported。这些名称是工具宿主，不是图片生成器的模型身份。
- Codex 图片响应的 `model` / `codex_model` 表示实际驱动模型，`requested_model` 保存客户端请求标签，`image_model:null` 明确底层生成器身份未披露，并附带说明。

## 请求示例

示例里的 `<model-id>` 换成 `/v1/models` 返回的真实模型名；这个桥不会替你选模型。

Chat Completions：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "<model-id>",
    "messages": [{"role": "user", "content": "Reply with exactly: bridge ok"}],
    "reasoning_effort": "medium"
  }'
```

Chat Completions 流式：

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

Responses + JSON Schema：

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

图片生成接口：

```bash
curl http://127.0.0.1:8000/v1/images/generations \
  -H "Content-Type: application/json" \
  -d '{"model":"<image-model>","prompt":"a bridge diagram","size":"1024x1024","quality":"auto","response_format":"b64_json"}'
```

音频生成接口：

```bash
curl http://127.0.0.1:8000/v1/audio/speech \
  -H "Content-Type: application/json" \
  -o speech.wav \
  -d '{"model":"<speech-model>","input":"bridge audio test","voice":"marin","response_format":"wav"}'
```

`<image-model>` / `<speech-model>` 用该账号实际可用的模型名，可先查 `/v1/models`。

## 调试接口

- `/`：首页和测试表单
- `/health`：服务状态、模型列表、访问地址
- `/routes`：可用路由
- `/models` / `/v1/models`：模型列表
- `/v1`：API 索引和推荐的 `agent_base_urls`

首页内置：

- 可切换的 `chat/completions`、`responses`、`images/generations`、`audio/speech` 测试表单
- Chat Completions 流式响应测试
- reasoning effort 选择（`low` / `medium` / `high` / `extra high`）
- Responses JSON Schema 测试输入
- 图片和音频响应预览

## Docker 说明

当前仓库内的 `Dockerfile` 适合做轻量启动，但它不会额外安装 Playwright 浏览器。

这对云主机场景通常不是问题，因为 device-code 已经改成纯 HTTP，不再依赖浏览器自动化。  
如果你明确要在容器里使用浏览器登录，则还需要额外补齐 Chromium 及系统依赖。

## 注意事项

- 本项目仅用于技术研究，请遵守 OpenAI 的服务条款
- 由于依赖 ChatGPT Web / Codex 的上游行为，后续接口字段和认证流程可能变化
- 高频调用可能触发风控或临时失败
