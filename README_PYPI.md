# DeepSeek Proxy

Translate OpenAI **Responses API** (`/v1/responses`) requests into **DeepSeek Chat Completions API** calls — so [codex (Claude Code CLI)](https://claude.ai/code) can use DeepSeek as its backend model.

---

## Install

```bash
pip install deepseek-proxy
```

## Usage

```bash
# Set your API key
export DEEPSEEK_API_KEY=sk-your-key-here

# Start proxy (foreground)
deepseek-proxy

# Or start as a daemon (background)
deepseek-proxy start --daemon

# Stop the daemon
deepseek-proxy stop

# Restart
deepseek-proxy restart

# Custom host/port
deepseek-proxy --host 0.0.0.0 --port 8080 start --daemon
```

The proxy listens on `http://127.0.0.1:8787` by default.

## Configure codex

Point codex to the proxy via `~/.codex/config.toml`:

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

**Field reference:**

| Field | Value | Description |
|---|---|---|
| `model` | `"deepseek-v4-flash"` | Default model for codex |
| `model_provider` | `"deepseek"` | Matches the provider section name |
| `base_url` | `"http://127.0.0.1:8787/v1"` | Proxy endpoint (no `/responses` suffix) |
| `env_key` | `"DEEPSEEK_API_KEY"` | Env var for the API key |
| `wire_api` | `"responses"` | Required value for this proxy |

## Verify

```bash
curl -X POST http://127.0.0.1:8787/v1/responses \
  -H "Authorization: Bearer $DEEPSEEK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":[{"role":"user","content":[{"type":"input_text","text":"hello"}]}],"model":"deepseek-v4-flash"}'
```

## Why?

codex uses the OpenAI Responses API natively. DeepSeek provides a Chat Completions API. The two protocols are incompatible — this proxy bridges the gap.

---

[GitHub](https://github.com/peinibiancheng/deepseek-proxy)
