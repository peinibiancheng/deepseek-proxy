# Debugging Retrospective: Building the DeepSeek Responses API Proxy

This document chronicles the debugging journey of building `ds_proxy.py` — a Flask proxy that translates OpenAI Responses API requests into DeepSeek Chat Completions API calls, and back. The proxy was developed iteratively by debugging against Claude Code (codex CLI), which acts as a strict Responses API client.

## Table of Contents

- [The Problem](#the-problem)
- [Debugging Strategy](#debugging-strategy)
- [Issue Timeline](#issue-timeline)
  - [1. SSE Event Format: Missing `event:` Line](#1-sse-event-format-missing-event-line)
  - [2. Missing `type` Field in JSON Payload](#2-missing-type-field-in-json-payload)
  - [3. Wrong Event Names (Dots vs Underscores)](#3-wrong-event-names-dots-vs-underscores)
  - [4. Usage Field Format Mismatch](#4-usage-field-format-mismatch)
  - [5. Missing `role` Field on Output Items](#5-missing-role-field-on-output-items)
  - [6. Wrong Output Item Type (`text` vs `message`)](#6-wrong-output-item-type-text-vs-message)
  - [7. Missing `response.in_progress` Event](#7-missing-responsein_progress-event)
  - [8. Missing `item_id` Field](#8-missing-item_id-field)
- [Server Infrastructure Issues](#server-infrastructure-issues)
- [Tools & Techniques](#tools--techniques)
- [Key Takeaways](#key-takeaways)

---

## The Problem

Claude Code uses the **OpenAI Responses API** (`/v1/responses`) with SSE streaming. DeepSeek provides a **Chat Completions API** (`/v1/chat/completions`). The formats differ in:

- Endpoint structure and payload schema
- SSE event naming and structure
- Field names for usage statistics
- Available roles (`developer` vs `system`)
- Output item types and lifecycle events

The proxy must translate bidirectionally while maintaining the correct SSE event lifecycle.

## Debugging Strategy

1. **curl as the primary test tool** — bypass the TTY-dependent client (codex) and send raw HTTP requests to the proxy. Capture the full SSE stream to a file for inspection.
2. **Structured logging** — every event emitted by the proxy is logged with direction (`SSE <<` for proxy→client, `DeepSeek 行` for upstream→proxy), making it easy to trace the flow.
3. **Compare event-by-event** — the OpenAI Responses API spec defines exactly 7 event types in a specific order. Compare the proxy's output against the expected sequence.
4. **Field-level verification** — each SSE event payload must match the expected schema; missing fields cause silent drops.

## Issue Timeline

### 1. SSE Event Format: Missing `event:` Line

**Symptom:** "stream disconnected before completion: stream closed before response.completed"

**Root cause:** The proxy was sending raw JSON lines, not proper SSE format. The Responses API client expects:
```
event: response.created\r\n
data: {"id": "resp_..."}\r\n
\r\n
```

The `event:` line and double `\r\n` terminator are required.

**Fix:** Created a helper:
```python
CRLF = "\r\n"
def sse_event(event_type, data):
    data['type'] = event_type
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event_type}{CRLF}data: {payload}{CRLF}{CRLF}"
```

---

### 2. Missing `type` Field in JSON Payload

**Symptom:** Same as above — the client requires `type` in both the `event:` line AND inside the JSON `data`.

**Root cause:** The JSON payload in `data` must include a `type` field matching the event name. Without it, the client cannot parse the event.

**Fix:** `sse_event()` now sets `data['type'] = event_type` before serialization.

---

### 3. Wrong Event Names (Dots vs Underscores)

**Symptom:** Events not recognized by the client.

**Root cause:** The proxy used `response.output.item.added` (dots between words), but the correct event name is `response.output_item.added` (underscore between `output` and `item`). Similarly for other events. The naming convention: words in the event name are separated by underscores, not dots.

**Affected events:**
- `response.output.item.added` → `response.output_item.added`
- `response.content.part.added` → `response.content_part.added`
- `response.output.text.delta` → `response.output_text.delta`
- `response.output.text.done` → `response.output_text.done`
- `response.output.item.done` → `response.output_item.done`

---

### 4. Usage Field Format Mismatch

**Symptom:** Parse error: "missing field `input_tokens`"

**Root cause:** DeepSeek returns usage in Chat Completions format (`prompt_tokens`, `completion_tokens`), but the Responses API expects `input_tokens` and `output_tokens`. The proxy was passing through the raw DeepSeek usage object without translation.

**Fix:** Added `translate_usage()` to map fields:
```python
{
    'input_tokens': usage.get('prompt_tokens', 0),
    'output_tokens': usage.get('completion_tokens', 0),
    'total_tokens': usage.get('total_tokens', 0),
    'input_tokens_details': {
        'cached_tokens': prompt_details.get('cached_tokens', 0)
    },
    'output_tokens_details': {
        'reasoning_tokens': completion_details.get('reasoning_tokens', 0)
    }
}
```

---

### 5. Missing `role` Field on Output Items

**Symptom:** Content received by client but not displayed.

**Root cause:** Output items in the Responses API require `role: "assistant"`. Without it, the client doesn't know how to render the message.

**Fix:** Added `'role': 'assistant'` to all output item payloads.

---

### 6. Wrong Output Item Type (`text` vs `message`)

**Symptom:** Client receives the response but silently discards the content — no visible output.

**Root cause:** This was the hardest to find. The output item's `type` field was set to `"text"`, but the Responses API requires `"message"` for assistant messages. When the type is `"text"`, the client treats it as a generic text block rather than a renderable message.

**Fix:** Changed `'type': 'text'` → `'type': 'message'` in three places:
- `response.output_item.added` payload
- `response.output_item.done` payload
- `response.completed` output array

---

### 7. Missing `response.in_progress` Event

**Symptom:** Potentially unstable connection — some clients expect this event between `response.created` and `response.output_item.added`.

**Root cause:** The proxy went directly from `response.created` to `response.output_item.added`, skipping the intermediate status event.

**Fix:** Added the `response.in_progress` event.

---

### 8. Missing `item_id` Field

**Symptom:** Client may not properly correlate events.

**Root cause:** The `response.output_item.added` event lacked a top-level `item_id` field that matches the item's `id`.

**Fix:** Added `'item_id': output_id` to the event payload.

## Server Infrastructure Issues

### Uvicorn ASGI Compatibility

The initial approach was `uvicorn.run(app)` with a Flask app. This fails because Flask is WSGI, not ASGI. Uvicorn expects an ASGI application.

**Fix:** Wrap the Flask app with `asgiref.wsgi.WsgiToAsgi`:
```python
from asgiref.wsgi import WsgiToAsgi
asgi_app = WsgiToAsgi(app)

# Use string import for uvicorn reload support:
uvicorn.run("ds_proxy:asgi_app", ...)
```

The string import (`"ds_proxy:asgi_app"`) is required for `reload=True` to work; the object reference (`uvicorn.run(asgi_app, ...)`) does not support hot reload.

## Tools & Techniques

| Tool | Purpose |
|---|---|
| `curl -N -o file` | Capture raw SSE stream for inspection |
| `grep` / `head` / `tail` | Quickly scan structured logs |
| `pkill -f <pattern>` | Kill stale proxy processes |
| `.venv/bin/python` | Use project virtual environment |
| `tail -f` | Watch proxy logs in real time |

## Key Takeaways

1. **SSE is strict about wire format** — the `event:` line and trailing `\r\n\r\n` are mandatory, not optional.
2. **`type` in both places** — the event name must appear in both the `event:` line AND the `data` JSON's `type` field.
3. **Event names use underscores** — `response.output_item.added`, not `response.output.item.added`. The dot separates `noun.verb`, underscores separate words within each part.
4. **Output item types matter** — `type: "message"` renders; `type: "text"` silently disappears.
5. **Debug with curl first** — strip away the client to isolate proxy issues. Only test with the actual client after the proxy passes curl tests.
6. **When in doubt, log everything** — structured logging of each event (directional, with `<<` / `>>` markers) makes trace analysis trivial.
7. **Flask needs ASGI wrapping for uvicorn** — `WsgiToAsgi` is the bridge, and string imports enable hot reload.

---

## Tool Call Support

After the basic streaming text response was working, the next challenge was supporting tool/function calling — the feature that allows Codex to execute commands, create files, and interact with the environment through the proxy.

### Issue 9: Tools/Tool Choice Dropped in Request

**Symptom:** Codex sends tool definitions but DeepSeek never sees them — returns plain text like "I'll create the file" instead of generating `tool_calls`.

**Root cause:** The proxy was not extracting `tools` and `tool_choice` from the request body, so they were never forwarded to DeepSeek's Chat Completions API.

**Fix:** Extract and pass tools through the call chain:
```python
tools = data.get("tools")
tool_choice = data.get("tool_choice")
payload["tools"] = ds_tools
if tool_choice:
    payload["tool_choice"] = tool_choice
```

---

### Issue 10: Tool Format Conversion (Flat vs Wrapped)

**Symptom:** DeepSeek returns 400 error: "missing field `function`"

**Root cause:** The Responses API sends tools in a flat format:
```json
{"type": "function", "name": "create_file", "parameters": {...}}
```
But the Chat Completions API expects a nested format:
```json
{"type": "function", "function": {"name": "create_file", "parameters": {...}}}
```

**Fix:** Added format detection and conversion:
```python
if "function" in t:
    ds_tools.append(t)  # Already wrapped
elif "name" in t:
    ds_tools.append({   # Flat → wrap
        "type": "function",
        "function": {
            "name": t["name"],
            "description": t.get("description", ""),
            "parameters": t.get("parameters", {})
        }
    })
```

---

### Issue 11: Input Message Content Type Handling

**Symptom:** Messages with `tool_call` and `tool_result` content parts are silently dropped — the conversation history loses tool context when Codex includes it.

**Root cause:** The proxy only handled `input_text` content parts. The Responses API uses multiple content types:
- `input_text` — regular text content
- `tool_call` — an assistant message's tool invocation
- `tool_result` — the result of executing a tool
- `reasoning` — model reasoning (not part of Chat Completions format)

**Fix:** Extended `content_parts` processing:
```python
for part in content_parts:
    ptype = part.get("type")
    if ptype == "input_text":
        text_buf += part.get("text", "")
    elif ptype == "tool_call":
        tool_calls.append({
            "id": part["id"],
            "type": "function",
            "function": {
                "name": function["name"],
                "arguments": function["arguments"]
            }
        })
    elif ptype == "tool_result":
        tool_call_id = part.get("tool_call_id", "")
        text_buf += part.get("content", "")
    elif ptype == "reasoning":
        pass  # Not supported in Chat Completions
```

---

### Issue 12: SSE Events for Tool Calls (Streaming)

**Symptom:** Codex receives the SSE stream but doesn't execute the tool calls — files aren't created.

**Root cause:** The proxy was only emitting `output_text.delta` events. Tool calls require a different set of SSE events in the Responses API format:
1. `response.output_item.added` — with `type: "function_call"`, `call_id`, `name`
2. `response.function_call_arguments.delta` — incremental argument tokens
3. `response.function_call_arguments.done` — final accumulated arguments
4. `response.output_item.done` — item completed

Additionally, the `response.completed` event's `output` array must include function_call items, not text items.

**Fix:** Added separate event emission paths for text vs tool calls, tracking per-index tool call state (`tool_call_items`, `tool_call_ids`).

---

### Issue 13: Empty-Role Messages Causing 400 Error

**Symptom:** DeepSeek returns: "role: unknown variant ''"

**Root cause:** Codex sends separator messages with empty role strings (`role: ""`) between conversation turns. These are not valid Chat Completions messages.

**First attempt:** Convert empty role to `"user"` — caused DeepSeek to treat them as new user instructions, exacerbating the loop.

**Final fix:** Infer role from content type when role is empty:
```python
if role not in ("system", "user", "assistant", "tool"):
    if isinstance(content_parts, list):
        types = [p.get("type") for p in content_parts]
        if "tool_call" in types:
            role = "assistant"
        elif "tool_result" in types:
            role = "tool"
        else:
            continue  # Skip true unknown messages
    else:
        continue
```

---

### Issue 14: Infinite Tool Call Loop

**Symptom:** DeepSeek keeps generating the same `tool_calls` (e.g., `exec_command`, `create_file`) in every request, causing Codex to create duplicate files indefinitely.

**Root cause:** Codex executes tool calls locally but never sends the `tool` role messages (tool results) back in subsequent API requests. Each request to DeepSeek has identical conversation history (system + user messages), so the model repeatedly generates the same tool calls.

**Failed approaches:**

1. **System instruction injection** — Adding "[System Note: tools already executed, don't call them again]" — DeepSeek ignored it and kept calling tools.
2. **Synthetic assistant + tool results without stripping tools** — DeepSeek called tools again for the same turn even after seeing tool results. The model wanted to execute the tool for the "current" request, treating the synthetic results as a previous attempt.

**Additional constraint:** DeepSeek v4 Flash's thinking mode requires the `reasoning_content` field to be echoed back when reconstructing assistant messages. Adding `reasoning_content: ""` resolves this.

**Final fix — multi-pronged approach:**

1. **Tool call log cache** — keyed by message signature (first 4 messages, role + truncated content). Stores the tool calls returned for each unique request pattern.
2. **Repeat request detection** — compares message signatures. If the same messages appear without tool results, it's a repeat.
3. **Synthetic context injection + tool stripping:**

```python
# Inject assistant message with tool_calls
messages.append({
    "role": "assistant",
    "content": None,
    "reasoning_content": "",  # Required by DeepSeek thinking mode
    "tool_calls": [...]
})
# Inject tool results
for tc in cached_tc:
    messages.append({
        "role": "tool",
        "tool_call_id": tc["id"],
        "content": "Tool executed successfully."
    })
# CRITICAL: Remove tools so DeepSeek can only respond with text
tools = None
```

4. **`ds_tools` initialization fix** — moved `ds_tools = []` outside the `if tools:` block to prevent `UnboundLocalError` when tools are stripped.

**Key insight:** The only reliable way to stop DeepSeek from calling tools is to remove the tool definitions from the request entirely. System instructions and synthetic tool results alone were both ineffective.

**Verification:**
- First request: `tools: 10` → DeepSeek returns `finish_reason=tool_calls`
- Repeat detected → inject assistant + tool messages, strip tools
- Second request: `tools: 0` → DeepSeek returns `finish_reason=stop` (text response)

### Debugging Techniques for Tool Calls

| Technique | Purpose |
|---|---|
| `curl` repeat test | Verify repeat detection and injection flow by sending identical requests |
| `grep "detect\|inject\|strip\|finish_reason"` | Quick trace of the tool call lifecycle |
| `/proc/<pid>/fd/` inspection | Check if daemon log file descriptors are valid (found deleted log file) |
| Clean restart (`kill; rm -rf logdir; restart`) | Fix log file issues in daemon mode |

### Key Takeaways for Tool Calls

1. **Codex doesn't send tool results back** — this is a fundamental behavioral difference from the OpenAI API. The proxy must compensate with synthetic context injection.
2. **Tool definitions must be stripped on repeats** — telling DeepSeek not to call tools doesn't work; removing the tools entirely is the only reliable approach.
3. **DeepSeek v4 thinking mode** — `reasoning_content` must be present (even if empty) in reconstructed assistant messages, or DeepSeek rejects the request with a 400 error.
4. **Format conversion is multi-layered** — tools need format conversion (flat→wrapped), content parts need type mapping (tool_call→tool_calls, tool_result→tool), and SSE events need a completely different set for function calls vs text.
5. **Daemon log management** — when using `start --daemon`, the log file can be deleted while the process is running. Use `nohup` or check `/proc/<pid>/fd/` to verify. Clean restart is the simplest fix.
