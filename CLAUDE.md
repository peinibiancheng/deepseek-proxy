# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Single-file Flask proxy (`ds_proxy.py`) that translates OpenAI Responses API requests into DeepSeek Chat Completions API calls, then converts the streaming SSE output back into the Responses API event format. Designed for Claude Code to use DeepSeek as a backend model via proxy configuration.

## File Structure

| File | Purpose |
|---|---|
| `ds_proxy.py` | Entire application — Flask app, endpoints, SSE generator, CLI commands |
| `pyproject.toml` | Package metadata, dependencies, entry point |
| `Makefile` | Build, publish, version bump, dev commands |
| `docs/debugging-retrospective.md` | Debugging journey and lessons learned |

## Commands

### Running the proxy

```bash
# From source (auto-detects uvicorn for hot reload)
python ds_proxy.py

# Daemon mode (background)
python ds_proxy.py start --daemon

# Stop daemon
python ds_proxy.py stop

# Show installation info
python ds_proxy.py info
```

### Development

```bash
make dev              # Run from source
make dev-install      # pip install -e .
```

### Make targets

```bash
make info             # Show installation and config info
make pypi-release     # bump-patch + publish to PyPI
make bump-patch       # 0.1.0 → 0.1.1
make bump-minor       # 0.1.0 → 0.2.0
make bump-major       # 0.1.0 → 1.0.0
make pypi-build       # Build wheel + sdist
make pypi-publish     # Build + upload to PyPI
make clean            # Remove build artifacts
```

### Claude Code configuration

```json
// ~/.claude/settings.local.json
{
  "proxy": {
    "url": "http://127.0.0.1:8787/v1/responses",
    "model": "deepseek-v4-flash"
  }
}
```

Requires `DEEPSEEK_API_KEY` environment variable on the proxy host.

## Architecture

### Data Flow

```
Codex ──POST /v1/responses──→ ds_proxy.py ──POST /v1/chat/completions──→ DeepSeek API
       ←──SSE event stream──              ←──SSE chunk stream──
```

### Translation Layers

1. **Input messages** (Responses API → Chat Completions):
   - `role: "developer"` → `role: "system"`
   - Content can be string or array of parts (`input_text`, `tool_call`, `tool_result`, `reasoning`)
   - `tool_call` parts → `assistant` message with `tool_calls` array
   - `tool_result` parts → `tool` message with `tool_call_id` + `content`

2. **Tools format** (Responses API flat → Chat Completions wrapped):
   - Responses: `{"type": "function", "name": "...", "parameters": {...}}`
   - Chat Completions: `{"type": "function", "function": {"name": "...", "parameters": {...}}}`

3. **SSE events** (DeepSeek chunks → Responses API events):
   - Text: `response.created` → `in_progress` → `output_item.added` → `content_part.added` → `output_text.delta` (per token) → `output_text.done` → `output_item.done` → `completed`
   - Tool calls: `response.created` → `in_progress` → `output_item.added` (type=function_call) → `function_call_arguments.delta` (per token) → `function_call_arguments.done` → `output_item.done` → `completed`

### Endpoints

| Route | Method | Purpose |
|---|---|---|
| `/v1/responses` | POST | Accepts Responses API, proxies to DeepSeek, returns SSE |
| `/v1/models` | GET | Lists `deepseek-v4-flash` with capabilities |
| `/v1/models/<id>` | GET | Returns model details including `tools: true` |

### CLI Commands (via `python ds_proxy.py`)

- `start` (default) — Run the proxy server (add `--daemon` for background)
- `stop` — Stop the running daemon
- `restart` — Stop + start daemon
- `info` — Show installation paths, dependency status, Codex config

## Key Design Decisions & Known Issues

### Tool Call Loop Prevention

Codex executes tool calls locally (e.g., creates files, runs commands) but never sends tool results back in subsequent API requests. This creates an infinite loop where DeepSeek generates the same `tool_calls` repeatedly.

**Fix** (`_tool_call_log` cache + `_needs_tool_results`):
- Cache tool calls returned for a given message signature
- On repeat requests (same signature, no tool results), inject synthetic assistant + tool messages
- **Critically**: strip tool definitions from the DeepSeek payload so it can only respond with text

The assistant message requires `reasoning_content: ""` for DeepSeek v4 Flash thinking mode compatibility.

### Empty-Role Message Inference

Codex sends separator messages with `role: ""` between conversation turns. DeepSeek rejects these. The proxy infers roles from content types when `role` is empty:
- Content with `tool_call` parts → `role: "assistant"`
- Content with `tool_result` parts → `role: "tool"`
- Other empty-role messages → skipped

### Daemon Mode Logging

When using `start --daemon`, stdout/stderr are redirected to `/tmp/deepseek-proxy/deepseek-proxy.log`. The log file descriptor can outlive the file if it's deleted externally. Use `nohup` or check `/proc/<pid>/fd/` to diagnose.
