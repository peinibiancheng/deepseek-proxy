<p align="center">
  <a href="#english">English</a> | <a href="#chinese">中文</a>
</p>

---

<h1 align="center">DeepSeek Proxy</h1>

<p align="center">
  A lightweight proxy that translates <strong>OpenAI Responses API</strong> requests into <strong>DeepSeek Chat Completions API</strong> calls.
  <br/>
  Designed for use with <a href="https://claude.ai/code">Claude Code</a> as a backend model provider.
</p>

---

<a name="english"></a>

# English

## Overview

DeepSeek Proxy is a single-file Flask application that sits between a Responses API client (e.g., Claude Code) and the DeepSeek API. It accepts requests in the OpenAI Responses API streaming format, translates them into DeepSeek's chat completions format, and converts the streaming SSE output back into the Responses API event protocol.

**Why?** Claude Code uses the Responses API (`/v1/responses`) natively. DeepSeek provides a Chat Completions API (`/v1/chat/completions`). This proxy bridges the gap so you can use DeepSeek as the model provider for Claude Code.

## Architecture

```
┌─────────────┐     Responses API SSE      ┌────────────────┐     DeepSeek Chat API      ┌────────────┐
│  Claude Code │  ───────────────────────→  │  ds_proxy.py   │  ──────────────────────→  │  DeepSeek  │
│  (Client)    │  ←───────────────────────  │  (Flask Proxy) │  ←──────────────────────  │   API      │
└─────────────┘     SSE Events (7 types)    └────────────────┘     SSE stream chunks      └────────────┘
```

### Components

| Layer | Technology | Role |
|---|---|---|
| **Server** | Flask + uvicorn (ASGI) | HTTP server, SSE streaming |
| **Adapter** | `asgiref.wsgi.WsgiToAsgi` | WSGI → ASGI wrapper for uvicorn |
| **Translator** | Custom logic in `ds_proxy.py` | Request/response format conversion |
| **Client** | `requests` (streaming) | HTTP client to DeepSeek API |

### Endpoints

| Route | Method | Purpose |
|---|---|---|
| `/v1/responses` | POST | Accept Responses API request, return SSE stream |
| `/v1/models` | GET | List available model (`deepseek-v4-flash`) |
| `/v1/models/<id>` | GET | Get model capabilities |

## Design Approach

The proxy follows a **stream-through** architecture:

1. **No buffering** — as DeepSeek streams tokens, the proxy immediately forwards them as SSE events
2. **Minimal transformation** — only the necessary field mappings are applied, keeping latency low
3. **Fail-fast** — errors from DeepSeek are propagated back as SSE error events
4. **Single-file** — the entire proxy is one Python file for easy deployment and modification

## Processing Flow

### Request Translation

When a client sends a request to `/v1/responses`, the proxy:

1. Extracts the `input` array from the Responses API payload
2. For each message:
   - Maps `role: "developer"` → `role: "system"` (DeepSeek doesn't support "developer" role)
   - Flattens content arrays into a single string (concatenates `input_text` parts)
   - Passes through `role: "user"` and `role: "assistant"` unchanged
3. Constructs a DeepSeek Chat Completions payload with `stream: true`
4. Streams the request to DeepSeek's API

### SSE Event Lifecycle

The proxy emits exactly these 8 events in order during a successful stream:

```
 1. response.created        → Stream begins, response metadata
 2. response.in_progress    → Response status confirmed
 3. response.output_item.added   → Output slot opened (type: "message")
 4. response.content_part.added  → Text part initialized
 5. response.output_text.delta   → Incremental content (one per token)
 6. response.output_text.done    → Full text with aggregated content
 7. response.output_item.done    → Output item completed
 8. response.completed           → Full response with usage info
```

On error, the proxy emits a single `response.error` event and stops.

### Field Mapping

| OpenAI Responses API | DeepSeek Chat API | Direction |
|---|---|---|
| `input[].role: "developer"` | `messages[].role: "system"` | → |
| `input[].content[].input_text` | `messages[].content` (string) | → |
| `choices[0].delta.content` | `response.output_text.delta.delta` | ← |
| `usage.prompt_tokens` | `usage.input_tokens` | ← |
| `usage.completion_tokens` | `usage.output_tokens` | ← |
| `prompt_tokens_details.cached_tokens` | `input_tokens_details.cached_tokens` | ← |
| `completion_tokens_details.reasoning_tokens` | `output_tokens_details.reasoning_tokens` | ← |

## Quick Start

### Prerequisites

- Python 3.8+
- A DeepSeek API key

### Installation

```bash
git clone <repo-url>
cd deepseek-proxy
pip install flask requests uvicorn asgiref
```

### Run

```bash
export DEEPSEEK_API_KEY=sk-your-key-here
python ds_proxy.py
```

The proxy starts on `http://127.0.0.1:8787`.

### Verify

```bash
curl -X POST http://127.0.0.1:8787/v1/responses \
  -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":[{"role":"user","content":[{"type":"input_text","text":"hello"}]}],"model":"deepseek-v4-flash"}'
```

## Configure Claude Code

Claude Code can be configured to use the proxy in two ways.

### Option A: Proxy config (simpler)

Add to `~/.claude/settings.local.json`:

```json
{
  "proxy": {
    "url": "http://127.0.0.1:8787/v1/responses",
    "model": "deepseek-v4-flash"
  }
}
```

This tells Claude Code to route all requests through the proxy URL and use the specified model.

### Option B: Model provider config

Add to `~/.codex/config.toml`:

```toml
model = "deepseek-v4-flash"
model_provider = "deepseek"

[model_providers.deepseek]
name = "DeepSeek"
base_url = "http://127.0.0.1:8787/v1"
env_key = "DEEPSEEK_API_KEY"
wire_api = "responses"
```

This registers DeepSeek as a custom model provider, making it selectable alongside other providers.

### Environment Variable

Regardless of which option you choose, set the API key:

```bash
export DEEPSEEK_API_KEY=sk-your-actual-key-here
```

The proxy reads this from the environment and passes it as the `Authorization` header to DeepSeek's API.

### Verification

Run Claude Code and send a message:

```bash
codex "hello"
```

You should see a response from the DeepSeek model. Check the proxy logs for details:

```bash
tail -f /tmp/ds_proxy.log
```

---

<a name="chinese"></a>

# 中文

## 概述

DeepSeek Proxy 是一个单文件 Flask 应用，充当 Responses API 客户端（如 Claude Code）和 DeepSeek API 之间的桥梁。它将 OpenAI Responses API 流式格式的请求转换为 DeepSeek 的对话补全格式，再将 DeepSeek 的流式输出转换回 Responses API 事件协议。

**为什么需要它？** Claude Code 原生使用 Responses API（`/v1/responses`），而 DeepSeek 提供的是 Chat Completions API（`/v1/chat/completions`）。这个代理填补了协议差异，让你可以在 Claude Code 中使用 DeepSeek 作为模型提供商。

## 架构

```
┌─────────────┐     Responses API SSE      ┌────────────────┐     DeepSeek Chat API      ┌────────────┐
│  Claude Code │  ───────────────────────→  │  ds_proxy.py   │  ──────────────────────→  │  DeepSeek  │
│  (客户端)    │  ←───────────────────────  │  (Flask 代理)  │  ←──────────────────────  │   API      │
└─────────────┘     SSE 事件 (7 种类型)     └────────────────┘     SSE 流式数据块         └────────────┘
```

### 组件

| 层级 | 技术 | 职责 |
|---|---|---|
| **服务器** | Flask + uvicorn (ASGI) | HTTP 服务、SSE 流式传输 |
| **适配器** | `asgiref.wsgi.WsgiToAsgi` | WSGI → ASGI 包装，用于 uvicorn |
| **转换器** | `ds_proxy.py` 中的自定义逻辑 | 请求/响应格式转换 |
| **客户端** | `requests` (流式) | 向 DeepSeek API 发送 HTTP 请求 |

### 接口

| 路由 | 方法 | 用途 |
|---|---|---|
| `/v1/responses` | POST | 接收 Responses API 请求，返回 SSE 流 |
| `/v1/models` | GET | 列出可用模型 (`deepseek-v4-flash`) |
| `/v1/models/<id>` | GET | 获取模型能力信息 |

## 设计思路

代理采用**流式直通**架构：

1. **不缓冲** — DeepSeek 逐 token 返回时，代理立即转发为 SSE 事件
2. **最小转换** — 只应用必要的字段映射，保持低延迟
3. **快速失败** — DeepSeek 的错误通过 SSE 错误事件传播回客户端
4. **单文件** — 整个代理只有一个 Python 文件，便于部署和修改

## 处理流程

### 请求转换

客户端向 `/v1/responses` 发送请求时，代理执行以下操作：

1. 从 Responses API 请求体中提取 `input` 数组
2. 对每条消息：
   - 将 `role: "developer"` 映射为 `role: "system"`（DeepSeek 不支持 "developer" 角色）
   - 将 content 数组合并为一个字符串（拼接所有 `input_text` 片段）
   - `role: "user"` 和 `role: "assistant"` 保持不变
3. 构造 DeepSeek Chat Completions 请求体，设置 `stream: true`
4. 以流式方式向 DeepSeek API 发送请求

### SSE 事件生命周期

一次成功的流式响应会按顺序发送以下 8 个事件：

```
 1. response.created        → 流开始，响应元数据
 2. response.in_progress    → 确认响应进行中
 3. response.output_item.added   → 开启输出槽 (类型: "message")
 4. response.content_part.added  → 初始化文本部分
 5. response.output_text.delta   → 增量内容（每个 token 一次）
 6. response.output_text.done    → 完整文本内容
 7. response.output_item.done    → 输出项完成
 8. response.completed           → 完整响应，含 usage 信息
```

发生错误时，代理发送一个 `response.error` 事件并停止。

### 字段映射

| OpenAI Responses API | DeepSeek Chat API | 方向 |
|---|---|---|
| `input[].role: "developer"` | `messages[].role: "system"` | → |
| `input[].content[].input_text` | `messages[].content` (字符串) | → |
| `choices[0].delta.content` | `response.output_text.delta.delta` | ← |
| `usage.prompt_tokens` | `usage.input_tokens` | ← |
| `usage.completion_tokens` | `usage.output_tokens` | ← |
| `prompt_tokens_details.cached_tokens` | `input_tokens_details.cached_tokens` | ← |
| `completion_tokens_details.reasoning_tokens` | `output_tokens_details.reasoning_tokens` | ← |

## 快速开始

### 前置条件

- Python 3.8+
- DeepSeek API 密钥

### 安装

```bash
git clone <repo-url>
cd deepseek-proxy
pip install flask requests uvicorn asgiref
```

### 运行

```bash
export DEEPSEEK_API_KEY=sk-your-key-here
python ds_proxy.py
```

代理启动在 `http://127.0.0.1:8787`。

### 验证

```bash
curl -X POST http://127.0.0.1:8787/v1/responses \
  -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":[{"role":"user","content":[{"type":"input_text","text":"你好"}]}],"model":"deepseek-v4-flash"}'
```

## 配置 Claude Code

有两种方式让 Claude Code 使用代理。

### 方式 A：代理配置（更简单）

添加到 `~/.claude/settings.local.json`：

```json
{
  "proxy": {
    "url": "http://127.0.0.1:8787/v1/responses",
    "model": "deepseek-v4-flash"
  }
}
```

这告诉 Claude Code 将所有请求通过代理 URL 路由，并使用指定模型。

### 方式 B：模型提供商配置

添加到 `~/.codex/config.toml`：

```toml
model = "deepseek-v4-flash"
model_provider = "deepseek"

[model_providers.deepseek]
name = "DeepSeek"
base_url = "http://127.0.0.1:8787/v1"
env_key = "DEEPSEEK_API_KEY"
wire_api = "responses"
```

这将 DeepSeek 注册为自定义模型提供商，可以在多个提供商之间切换选择。

### 环境变量

无论选择哪种方式，都需要设置 API 密钥：

```bash
export DEEPSEEK_API_KEY=sk-your-actual-key-here
```

代理从环境变量读取密钥，并将其作为 `Authorization` 头传递给 DeepSeek API。

### 验证

运行 Claude Code 发送消息：

```bash
codex "hello"
```

你应该能看到来自 DeepSeek 模型的响应。查看代理日志获取详情：

```bash
tail -f /tmp/ds_proxy.log
```

---

<p align="center">
  <a href="#english">English</a> | <a href="#chinese">中文</a>
</p>
