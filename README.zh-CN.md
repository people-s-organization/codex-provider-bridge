# Codex Provider Bridge

[English](README.md) | **简体中文**

将 ChatGPT / Codex 登录态转为本地 OpenAI 兼容 API，供 DeepSeek Harness（DSH）、OpenClaw、Hermes 等 Agent 使用。

支持文本、流式输出、函数工具调用、图片输入和模型特有的推理强度。这是兼容桥接服务，不是完整的 OpenAI API 替代品。

## 快速开始

需要支持 `venv` 的 Python 3，以及有权使用目标模型的 ChatGPT / Codex 账号。

```bash
git clone https://github.com/people-s-organization/codex-provider-bridge.git
cd codex-provider-bridge
bash start.sh
```

脚本会创建虚拟环境、安装依赖，并在需要时创建 `.env`。优先使用 `CHATGPT_ACCESS_TOKEN` 或已有的 `~/.codex/auth.json`；没有凭据时提示登录。

- **服务器 / SSH：** `bash start.sh --auth device`，在自己的电脑上打开终端显示的授权链接。
- **浏览器登录：** `INSTALL_PLAYWRIGHT_BROWSER=1 bash start.sh --auth browser`。

默认地址为 **http://127.0.0.1:8000**。如端口变化，以启动日志为准。首页提供测试表单，`/health` 可检查服务状态。

## 接入客户端

| 配置项 | 填写内容 |
|---|---|
| API 类型 | OpenAI 兼容 Chat Completions 或 Responses |
| Base URL | `http://127.0.0.1:8000/v1`（按实际主机和端口修改） |
| API Key | `BRIDGE_API_KEY`；仅在桥未开启鉴权时可填占位值 |
| 模型 | `/v1/models` 返回的真实 ID |

**不要把 ChatGPT access token 给客户端。** 对外监听前，在 `.env` 设置 `BRIDGE_API_KEY`；远程访问应使用 TLS。

```bash
# 如已开启鉴权，先在当前 shell 中将 BRIDGE_API_KEY 设为桥的密钥。
curl http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer ${BRIDGE_API_KEY:-unused}"

curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer ${BRIDGE_API_KEY:-unused}" \
  -H 'Content-Type: application/json' \
  -d '{"model":"<model-id>","messages":[{"role":"user","content":"Hello"}]}'
```

将 `<model-id>` 替换为返回的模型 ID。需要流式输出时，增加 `"stream": true` 并使用 `curl -N`。

## DeepSeek Harness

将 [provider 模板](scripts/dsh-provider.example.yaml) 合并到现有 DSH settings，不要覆盖其他配置。

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
            # max: ultra  # 仅在模型接受 ultra 时启用。
```

- 替换地址和模型 ID，并在 DSH 环境变量或凭据服务中将 `CODEX_BRIDGE_API_KEY` 设为桥的密钥。
- **必须声明能力：** `input: [text, image]` 开启图片输入，`reasoningEfforts` 开启推理强度选项。只列出模型实际支持的能力。
- **Ultra：** 在 `reasoningEfforts` 增加 `max: ultra`，然后在 DSH 选择 **max**（或设置 `reasoning: max`），实际发送 **ultra**，不是 `xhigh`。当前 DSH 的选项键是 `max`，不是 `ultra`。
- 使用 Responses 时，将 `api` 改为 `openai-responses`，并删除 Chat 专用的 `compat` 配置块。

## 支持的能力

| 能力 | 接口 / 用法 |
|---|---|
| 文本、流式、函数调用、图片输入 | `/v1/chat/completions`、`/v1/responses` |
| 旧版文本补全 | `/v1/completions` |
| 模型列表 | `/v1/models` |
| Responses 续聊 | `store`、`previous_response_id`、GET/DELETE `/v1/responses/{id}` |
| 图片生成 | `/v1/images/generations`，需要上游具备对应能力 |
| 语音合成 | `/v1/audio/speech`，需要上游具备对应能力 |

**推理强度：** Chat 使用 `"reasoning_effort":"high"`，Responses 使用 `"reasoning":{"effort":"high"}`。Responses 也兼容顶层别名，两者同时存在时原生字段优先。`ultra`、`none`、`minimal` 等模型特有值会透传给上游校验，不代表每个模型都支持。`extra high` 仍兼容为 `xhigh`。省略参数使用上游默认值，不一定是“关闭推理”。

**图片：** Chat 使用 `image_url` 内容块，Responses 使用 `input_image`，均支持图片 URL 和 Base64 data URL。对话内不支持音频/视频输入及音频输出；语音合成使用上面的独立接口。

## 关键限制

- **工具由客户端执行。** 桥只转发调用和结果；Chat/Responses 请求只支持 `type: function` 工具。
- **不是所有 OpenAI 参数都生效。** 输出 token 上限、`temperature` 等采样设置会被忽略并返回兼容告警。设置 `BRIDGE_STRICT_COMPATIBILITY=true` 可改为拒绝这些参数。JSON/schema 输出通过指令引导，不保证严格结构化输出。
- **Responses 历史是临时的。** 存储会过期，重启即丢失，多进程不共享；需要持久续聊时请回传完整历史。
- **媒体能力取决于账号和模型。** `/v1/models` 不是独立的图片/语音模型目录。Realtime 通道输出 WAV/PCM 以外的音频格式需要本机安装 `ffmpeg`。
- **订阅登录不提供其他完整 API。** embeddings、files、转写等已识别端点可在配置 `OPENAI_API_KEY` 后代理，否则返回 `501`；未知路径返回 `404`。OpenAI API 兜底使用独立凭据，可能产生独立费用。

## 配置与排错

配置写入 `.env`，可用选项见 [.env.example](.env.example)。

| 配置项 | 用途 |
|---|---|
| `HOST` / `PORT` | 监听地址，默认 `127.0.0.1:8000` |
| `BRIDGE_API_KEY` | 客户端访问桥的密钥 |
| `CHATGPT_ACCESS_TOKEN` | 可选，用于替代 Codex 登录 |
| `CHATGPT_MODELS` / `CHATGPT_EXTRA_MODELS` | 覆盖 / 追加模型列表，不会赋予模型访问权限 |
| `OPENAI_API_KEY` | 可选，用于 OpenAI API 代理和媒体兜底 |
| `CHATGPT_MEDIA_MODEL` / `CHATGPT_REALTIME_MODEL` | 图片生成 / 语音所用的上游模型 |

- **401：** 检查桥密钥；若错误来自上游，更新 Codex 登录或 token。
- **模型列表为空 / 找不到模型：** 检查账号权限及首页的模型发现状态，不要假设模型 ID 一定存在。
- **DSH 没有推理或图片选项：** 检查上面的模型能力声明。
- **上游拒绝推理档位：** 改用该模型支持的值，桥不会自动降档。

部署、回滚和运维细节见 [OPERATIONS.md](OPERATIONS.md)，接入验证见 [DSH 探针说明](scripts/DSH_PROBE.md)。

本项目依赖 ChatGPT / Codex 上游行为，接口可能变化。请遵守 OpenAI 服务条款和账号使用限制。
