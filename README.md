# Codex Provider Bridge

将 ChatGPT Web / Codex 登录态桥接为标准 OpenAI 兼容接口，供 OpenClaw、Hermes、Claude Code 等 Agent 使用。

项目会对接 ChatGPT 的 `codex/responses` 通道，并暴露常见的 OpenAI 风格路由：

- `GET /v1/models`
- `POST /v1/responses`
- `POST /v1/chat/completions`
- `POST /v1/images/generations`
- `POST /v1/audio/speech`
- 对应的非 `/v1` 别名：`/responses`、`/chat/completions`、`/images/generations`、`/audio/speech`
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
CHATGPT_MODEL_ALIASES=
CHATGPT_EXTRA_MODEL_ALIASES=
```

说明：

- `CHATGPT_ACCESS_TOKEN`：如果你已经有 token，填上后可直接启动
- `OPENAI_API_KEY`：可选；图片和语音都优先走 ChatGPT/Codex 能力，只有对应能力失败或缺 token 时才用它兜底
- `OPENAI_BASE_URL`：OpenAI API 地址，默认是 `https://api.openai.com`
- `CHATGPT_ACCOUNT_ID`：可选；默认会从 `~/.codex/auth.json` 读取，realtime 语音握手会带上它
- `CHATGPT_AUTH_METHOD`：兼容性兜底配置；可选 `prompt | auto | browser | device`
- `HOST` / `PORT`：服务监听地址
- `CHATGPT_BASE_URL`：默认是 `https://chatgpt.com`
- `CHATGPT_MODELS`：显式指定 `/v1/models` 返回的模型列表，支持逗号分隔或 JSON 数组
- `CHATGPT_EXTRA_MODELS`：在自动模型列表后追加模型，例如刚发布但本地缓存还没刷新的模型
- `CHATGPT_DEFAULT_MODEL`：首页测试表单默认选中的模型
- `CHATGPT_MEDIA_MODEL`：图片接口调用 Codex `image_generation` 工具时使用的上游 Responses 模型；默认沿用 `CHATGPT_DEFAULT_MODEL`，再兜底到 `gpt-5.5`
- `CHATGPT_REALTIME_MODEL`：语音接口调用 realtime WebSocket 时使用的模型，默认 `gpt-realtime-1.5`
- `CHATGPT_MODELS_FILE`：Codex 模型缓存路径，默认读取 `~/.codex/models_cache.json`
- `CHATGPT_CODEX_CONFIG_FILE`：Codex 配置路径，默认读取 `~/.codex/config.toml` 中的 `model`
- `CHATGPT_MODEL_ALIASES` / `CHATGPT_EXTRA_MODEL_ALIASES`：模型别名映射，支持 JSON 对象或 `old=new,old2=new2`

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

## 接口兼容范围

### 文本接口

- `/v1/chat/completions`：支持普通响应和 SSE 流式响应
- `/v1/responses`：支持非流式 OpenAI Responses 形状，会返回 `output_text`、`output` 和 `usage`
- `reasoning_effort` 和 `reasoning.effort` 支持 `low` / `medium` / `high` / `xhigh`，也兼容 `extra high`
- `/v1/responses` 的 `text.format.type=json_schema` 会被转换成额外 instructions，引导上游返回符合 schema 的纯 JSON

`/v1/models` 不再维护写死的主列表，读取顺序是：

1. `CHATGPT_MODELS`
2. `~/.codex/models_cache.json`
3. 内置兜底列表
4. `CHATGPT_EXTRA_MODELS` 追加

默认测试模型读取顺序是：

1. `CHATGPT_DEFAULT_MODEL`
2. `~/.codex/config.toml` 中的 `model`
3. 当前模型列表第一个

默认别名仍保留 OpenAI API 客户端常见模型名的兼容映射，也可以通过 `CHATGPT_MODEL_ALIASES` 完全覆盖：

- `gpt-4.1` 会映射到当前默认模型
- `gpt-4.1-mini` 会映射到当前模型列表里的第一个 `mini` 模型

### 媒体接口

- `/v1/images/generations`：优先使用 ChatGPT/Codex backend 的 Responses `image_generation` 工具，返回 `b64_json`；如果没有 ChatGPT token 但有 `OPENAI_API_KEY`，会兜底代理到 OpenAI Image API
- `/v1/audio/speech`：优先使用 ChatGPT/Codex bearer 直连 OpenAI realtime WebSocket，收集 `response.output_audio.delta` 后返回真实音频；如果没有 ChatGPT token 但有 `OPENAI_API_KEY`，会兜底代理到 OpenAI Speech API
- ChatGPT realtime 输出原生是 24 kHz PCM；`wav` / `pcm` 可直接返回，`mp3` / `aac` / `flac` / `opus` 需要本机 `ffmpeg`
- 如果缺少对应真实上游凭据，媒体接口会返回 `501`，不会返回假图片或假音频

## 请求示例

Chat Completions：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.5",
    "messages": [{"role": "user", "content": "Reply with exactly: bridge ok"}],
    "reasoning_effort": "medium"
  }'
```

Responses + JSON Schema：

```bash
curl http://127.0.0.1:8000/v1/responses \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-5.5",
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
  -d '{"model":"gpt-image-2","prompt":"a bridge diagram","size":"1024x1024","quality":"auto","response_format":"b64_json"}'
```

音频生成接口：

```bash
curl http://127.0.0.1:8000/v1/audio/speech \
  -H "Content-Type: application/json" \
  -o speech.wav \
  -d '{"model":"gpt-4o-mini-tts","input":"bridge audio test","voice":"marin","response_format":"wav"}'
```

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
