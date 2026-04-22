# Codex Provider Bridge

将 ChatGPT Web / Codex 登录态桥接为标准 OpenAI 兼容接口，供 OpenClaw、Hermes、Claude Code 等 Agent 使用。

项目会对接 ChatGPT 的 `codex/responses` 通道，并暴露常见的 OpenAI 风格路由：

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /chat/completions`
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
CHATGPT_AUTH_METHOD=prompt
HOST=0.0.0.0
PORT=8000
CHATGPT_BASE_URL=https://chatgpt.com
```

说明：

- `CHATGPT_ACCESS_TOKEN`：如果你已经有 token，填上后可直接启动
- `CHATGPT_AUTH_METHOD`：兼容性兜底配置；可选 `prompt | auto | browser | device`
- `HOST` / `PORT`：服务监听地址
- `CHATGPT_BASE_URL`：默认是 `https://chatgpt.com`

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

## 调试接口

- `/`：首页和测试表单
- `/health`：服务状态、模型列表、访问地址
- `/routes`：可用路由
- `/models` / `/v1/models`：模型列表
- `/v1`：API 索引和推荐的 `agent_base_urls`

首页内置：

- 普通请求测试表单
- 流式响应测试
- reasoning effort 选择（`low` / `medium` / `high` / `extra high`）

## Docker 说明

当前仓库内的 `Dockerfile` 适合做轻量启动，但它不会额外安装 Playwright 浏览器。

这对云主机场景通常不是问题，因为 device-code 已经改成纯 HTTP，不再依赖浏览器自动化。  
如果你明确要在容器里使用浏览器登录，则还需要额外补齐 Chromium 及系统依赖。

## 注意事项

- 本项目仅用于技术研究，请遵守 OpenAI 的服务条款
- 由于依赖 ChatGPT Web / Codex 的上游行为，后续接口字段和认证流程可能变化
- 高频调用可能触发风控或临时失败
