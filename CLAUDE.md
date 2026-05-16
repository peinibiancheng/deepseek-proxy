# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Single-file Flask proxy (`ds_proxy.py`) that translates OpenAI Responses API requests into DeepSeek Chat Completions API calls, then converts the streaming SSE output back into the Responses API event format. Used by Claude Code to route through a DeepSeek backend via proxy.

## Architecture

- **`ds_proxy.py`** — The entire application. One Flask app, two endpoints, one SSE generator.

### Endpoints

| Route | Method | Purpose |
|---|---|---|
| `/v1/responses` | POST | Accepts Responses API format, proxies to `api.deepseek.com/v1/chat/completions`, returns SSE stream |
| `/v1/models` | GET | Lists available model (`deepseek-v4-flash`) |
| `/v1/models/<id>` | GET | Returns model capabilities |

### SSE Events (Responses API protocol)

The proxy emits these events in order during streaming:

1. `response.created` — initial response metadata
2. `response.output.item.added` — output slot opened
3. `response.content_part.added` — text part initialized
4. `response.output_text.delta` — incremental content (one per token)
5. `response.content_part.done` — final aggregated text
6. `response.output.item.done` — output item completed
7. `response.completed` — full response with usage info

### Key Translation Logic

- `role: "developer"` in input is remapped to `role: "system"` for DeepSeek
- Content can be either a string or an array of `input_text` parts
- DeepSeek's streaming `choices[0].delta.content` is forwarded as `response.output_text.delta` events
- DeepSeek's `usage` from the final chunk is embedded in `response.completed`

## Running

**推荐（自动热重载）：**

```bash
pip install uvicorn asgiref
python ds_proxy.py
```

如果安装了 uvicorn + asgiref，入口自动用 `uvicorn.run()` 启动，带热重载。否则回退到 Flask 开发服务器。

**直接命令行启动 uvicorn：**

```bash
uvicorn ds_proxy:app --host 127.0.0.1 --port 8787
```

**使用 gunicorn：**

```bash
gunicorn -w 1 -b 127.0.0.1:8787 ds_proxy:app
```

> 注意：gunicorn 在 Windows 上需要 `--worker-class waitress` 或使用 WSL。uvicorn 在跨平台上更稳定。

所有方式都监听 `127.0.0.1:8787`。

## Claude Code Usage

Configure via `~/.claude/settings.local.json`:

```json
{
  "proxy": {
    "url": "http://127.0.0.1:8787/v1/responses",
    "model": "deepseek-v4-flash"
  }
}
```

Set `DEEPSEEK_API_KEY` as environment variable for the proxy to pick up (passed via `Authorization` header from Claude Code).
