import os
import sys
import signal
import tempfile
import argparse
from flask import Flask, request, Response, jsonify
import requests
import json
import uuid
import time
import traceback

import logging
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Logging configuration
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger.setLevel(logging.DEBUG)
DEEPSEEK_CHAT_URL = "https://api.deepseek.com/v1/chat/completions"

# ASGI wrapper for uvicorn
try:
    from asgiref.wsgi import WsgiToAsgi
    asgi_app = WsgiToAsgi(app)
except ImportError:
    asgi_app = None

CRLF = "\r\n"

# Conversation cache for previous_response_id support
_prev_responses = {}
_MAX_CACHED = 50

# Cache: message_signature -> [tool_calls] for synthetic tool result injection
_tool_call_log = {}
_MAX_TOOL_LOG = 100

def _messages_signature(messages):
    """Create a stable signature from the first few non-empty messages."""
    sig = []
    for m in messages[:4]:
        role = m.get("role", "")
        content = str(m.get("content", ""))[:80]
        sig.append(f"{role}:{content}")
    return "|".join(sig)

def _needs_tool_results(messages, log):
    """Check if this request is a repeat (same messages, no tool results)."""
    if not log:
        return None
    # Check if messages contain any tool role
    has_tool_msgs = any(m.get("role") == "tool" for m in messages)
    if has_tool_msgs:
        return None  # Already has tool results
    sig = _messages_signature(messages)
    if sig in log:
        return log[sig]
    return None

def translate_usage(usage):
    """Translate DeepSeek usage format to Responses API format."""
    if not usage:
        return {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}
    result = {
        'input_tokens': usage.get('prompt_tokens', 0),
        'output_tokens': usage.get('completion_tokens', 0),
        'total_tokens': usage.get('total_tokens', 0),
    }
    # Preserve DeepSeek-specific detail fields (optional)
    prompt_details = usage.get('prompt_tokens_details')
    if prompt_details:
        result['input_tokens_details'] = {
            'cached_tokens': prompt_details.get('cached_tokens', 0)
        }
    completion_details = usage.get('completion_tokens_details')
    if completion_details:
        result['output_tokens_details'] = {
            'reasoning_tokens': completion_details.get('reasoning_tokens', 0)
        }
    logger.debug(f"usage 转换: {usage} → {result}")
    return result

def sse_event(event_type, data):
    """Generate a standard Responses API SSE event with type field in JSON."""
    data['type'] = event_type
    payload = json.dumps(data, ensure_ascii=False)
    logger.debug(f"SSE << {event_type}")
    return f"event: {event_type}{CRLF}data: {payload}{CRLF}{CRLF}"


def generate_codex_stream(auth, messages, tools=None, tool_choice=None, resp_id=None, tool_cache_key=None):
    if resp_id is None:
        resp_id = f"resp_{uuid.uuid4()}"
    logger.info(f"开始生成响应，ID: {resp_id}")

    # Track if we've started an output item and what type
    output_started = False
    is_tool_call = False
    tool_call_items = {}          # index -> {id, name, arguments}
    tool_call_ids = []            # ordered indices
    full_text = ""
    usage_info = None

    created_at = int(time.time())

    # 1. response.created
    yield sse_event("response.created", {
        'id': resp_id,
        'object': 'response.created',
        'response': {
            'id': resp_id,
            'object': 'response',
            'created_at': created_at,
            'status': 'in_progress',
            'model': 'deepseek-v4-flash',
            'output': []
        }
    })

    # 2. response.in_progress
    yield sse_event("response.in_progress", {
        'id': resp_id,
        'object': 'response.in_progress',
        'response': {
            'id': resp_id,
            'object': 'response',
            'created_at': created_at,
            'status': 'in_progress',
            'model': 'deepseek-v4-flash',
            'output': []
        }
    })

    try:
        payload = {
            "model": "deepseek-v4-flash",
            "messages": messages,
            "stream": True,
            "temperature": 0.7,
            "max_tokens": 4096,
        }
        ds_tools = []
        if tools:
            # Convert Responses API tool format to Chat Completions format
            for t in tools:
                if "function" in t:
                    # Already has function wrapper (Chat Completions format)
                    ds_tools.append(t)
                elif "name" in t:
                    # Responses API flat format: wrap in function key
                    ds_tools.append({
                        "type": "function",
                        "function": {
                            "name": t.get("name", ""),
                            "description": t.get("description", ""),
                            "parameters": t.get("parameters", {"type": "object", "properties": {}})
                        }
                    })
                else:
                    logger.debug(f"跳过未知工具格式: {json.dumps(t)[:200]}")
            if ds_tools:
                payload["tools"] = ds_tools
        if tool_choice:
            payload["tool_choice"] = tool_choice

        if messages:
            roles_str = ", ".join(f"{i}:{m.get('role','?')}" for i, m in enumerate(messages))
            logger.debug(f"发送到 DeepSeek: {len(messages)} 条消息 [{roles_str}], tools: {len(ds_tools) if ds_tools else 0} 个")
        else:
            logger.warning("没有有效消息可发送到 DeepSeek")
            raise Exception("没有有效消息")

        with requests.post(
            DEEPSEEK_CHAT_URL,
            headers={"Authorization": auth, "Content-Type": "application/json"},
            json=payload,
            stream=True,
            timeout=30
        ) as resp:
            logger.info(f"DeepSeek 响应状态码: {resp.status_code}")

            if resp.status_code != 200:
                error_text = resp.text
                logger.error(f"DeepSeek API 错误: {error_text}")
                raise Exception(f"DeepSeek API 返回错误: {resp.status_code} - {error_text}")

            line_count = 0
            for line in resp.iter_lines():
                line_count += 1
                if not line:
                    continue

                line = line.decode('utf-8')
                logger.debug(f"DeepSeek 行 {line_count}: {line}")

                if line.startswith('data: '):
                    data_str = line[6:]
                    if data_str == '[DONE]':
                        logger.debug("收到 DeepSeek [DONE]")
                        continue

                    try:
                        data = json.loads(data_str)
                        if 'usage' in data:
                            usage_info = data['usage']

                        if 'choices' in data and len(data['choices']) > 0:
                            choice = data['choices'][0]
                            delta = choice.get('delta', {})
                            finish_reason = choice.get('finish_reason')

                            # --- Handle text content ---
                            content = delta.get('content')
                            if content:
                                if not output_started:
                                    is_tool_call = False
                                    output_started = True
                                    text_output_id = f"output_{uuid.uuid4()}"
                                    yield sse_event("response.output_item.added", {
                                        'id': resp_id,
                                        'object': 'response.output_item.added',
                                        'item_id': text_output_id,
                                        'output_index': 0,
                                        'item': {
                                            'id': text_output_id,
                                            'object': 'response.output_item',
                                            'type': 'message',
                                            'role': 'assistant',
                                            'status': 'in_progress',
                                            'content': []
                                        }
                                    })
                                    yield sse_event("response.content_part.added", {
                                        'id': resp_id,
                                        'object': 'response.content_part.added',
                                        'output_index': 0,
                                        'content_index': 0,
                                        'part': {
                                            'type': 'text',
                                            'text': ''
                                        }
                                    })

                                full_text += content
                                yield sse_event("response.output_text.delta", {
                                    "id": resp_id,
                                    "object": "response.output_text.delta",
                                    "output_index": 0,
                                    "content_index": 0,
                                    "delta": content
                                })

                            # --- Handle tool calls ---
                            tool_calls = delta.get('tool_calls')
                            if tool_calls:
                                is_tool_call = True
                                for tc in tool_calls:
                                    idx = tc.get('index')
                                    if idx not in tool_call_items:
                                        # First chunk for this tool call
                                        tc_id = tc.get('id', f"call_{uuid.uuid4().hex[:16]}")
                                        tc_name = tc.get('function', {}).get('name', 'unknown_tool')
                                        tc_args = tc.get('function', {}).get('arguments', '')
                                        tool_call_items[idx] = {
                                            'id': tc_id,
                                            'name': tc_name,
                                            'arguments': tc_args
                                        }
                                        tool_call_ids.append(idx)
                                        # Emit output_item.added for this function call
                                        yield sse_event("response.output_item.added", {
                                            'id': resp_id,
                                            'object': 'response.output_item.added',
                                            'item_id': tc_id,
                                            'output_index': idx,
                                            'item': {
                                                'id': tc_id,
                                                'object': 'response.output_item',
                                                'type': 'function_call',
                                                'status': 'in_progress',
                                                'call_id': tc_id,
                                                'name': tc_name,
                                                'arguments': ''
                                            }
                                        })
                                        if tc_args:
                                            yield sse_event("response.function_call_arguments.delta", {
                                                "id": resp_id,
                                                "object": "response.function_call_arguments.delta",
                                                "output_index": idx,
                                                "item_id": tc_id,
                                                "delta": tc_args
                                            })
                                    else:
                                        # Subsequent chunks — accumulate arguments
                                        func = tc.get('function', {})
                                        arg_delta = func.get('arguments', '')
                                        if arg_delta:
                                            tool_call_items[idx]['arguments'] += arg_delta
                                            yield sse_event("response.function_call_arguments.delta", {
                                                "id": resp_id,
                                                "object": "response.function_call_arguments.delta",
                                                "output_index": idx,
                                                "item_id": tool_call_items[idx]['id'],
                                                "delta": arg_delta
                                            })

                            if finish_reason:
                                logger.info(f"DeepSeek finish_reason={finish_reason}")

                    except Exception as e:
                        logger.error(f"解析 DeepSeek 数据失败: {e}")
                        logger.error(f"原始数据: {data_str}")
                        raise Exception(f"数据解析失败: {e}")

            logger.info(f"DeepSeek 流结束，共处理 {line_count} 行")

        # --- Emit done events based on response type ---
        if is_tool_call:
            # Emit function_call_arguments.done + output_item.done for each tool call
            output_items = []
            for idx in sorted(tool_call_ids):
                item = tool_call_items[idx]
                yield sse_event("response.function_call_arguments.done", {
                    'id': resp_id,
                    'object': 'response.function_call_arguments.done',
                    'output_index': idx,
                    'item_id': item['id'],
                    'name': item['name'],
                    'arguments': item['arguments']
                })
                yield sse_event("response.output_item.done", {
                    'id': resp_id,
                    'object': 'response.output_item.done',
                    'output_index': idx,
                    'item': {
                        'id': item['id'],
                        'object': 'response.output_item',
                        'type': 'function_call',
                        'status': 'completed',
                        'call_id': item['id'],
                        'name': item['name'],
                        'arguments': item['arguments']
                    }
                })
                output_items.append({
                    'id': item['id'],
                    'object': 'response.output_item',
                    'type': 'function_call',
                    'status': 'completed',
                    'call_id': item['id'],
                    'name': item['name'],
                    'arguments': item['arguments']
                })

            # Log tool calls for repeat detection
            if tool_cache_key and output_items:
                _tool_call_log[tool_cache_key] = [{
                    "id": item["id"],
                    "name": item["name"],
                    "arguments": item["arguments"]
                } for item in output_items]
                while len(_tool_call_log) > _MAX_TOOL_LOG:
                    _tool_call_log.pop(next(iter(_tool_call_log)))
                logger.info(f"缓存工具调用记录: key={tool_cache_key}, tools={len(output_items)}")

            translated_usage = translate_usage(usage_info)
            yield sse_event("response.completed", {
                'id': resp_id,
                'object': 'response.completed',
                'response': {
                    'id': resp_id,
                    'object': 'response',
                    'created_at': created_at,
                    'status': 'completed',
                    'model': 'deepseek-v4-flash',
                    'output': output_items,
                    'usage': translated_usage
                }
            })
            logger.info(f"工具调用完成: {len(tool_call_items)} 个工具")
        elif output_started:
            # Text response
            yield sse_event("response.output_text.done", {
                'id': resp_id,
                'object': 'response.output_text.done',
                'output_index': 0,
                'content_index': 0,
                'text': full_text
            })

            yield sse_event("response.output_item.done", {
                'id': resp_id,
                'object': 'response.output_item.done',
                'output_index': 0,
                'item': {
                    'id': text_output_id,
                    'object': 'response.output_item',
                    'type': 'message',
                    'role': 'assistant',
                    'status': 'completed',
                    'content': [{'type': 'text', 'text': full_text}]
                }
            })

            translated_usage = translate_usage(usage_info)
            yield sse_event("response.completed", {
                'id': resp_id,
                'object': 'response.completed',
                'response': {
                    'id': resp_id,
                    'object': 'response',
                    'created_at': created_at,
                    'status': 'completed',
                    'model': 'deepseek-v4-flash',
                    'output': [
                        {
                            'id': text_output_id,
                            'object': 'response.output_item',
                            'type': 'message',
                            'role': 'assistant',
                            'status': 'completed',
                            'content': [{'type': 'text', 'text': full_text}]
                        }
                    ],
                    'usage': translated_usage
                }
            })
        else:
            # Empty response (no content, no tool calls)
            translated_usage = translate_usage(usage_info)
            yield sse_event("response.completed", {
                'id': resp_id,
                'object': 'response.completed',
                'response': {
                    'id': resp_id,
                    'object': 'response',
                    'created_at': created_at,
                    'status': 'completed',
                    'model': 'deepseek-v4-flash',
                    'output': [],
                    'usage': translated_usage
                }
            })

    except Exception as e:
        logger.error(f"流生成失败: {str(e)}")
        logger.error(traceback.format_exc())
        yield sse_event("response.error", {
            'id': resp_id,
            'object': 'response.error',
            'message': str(e)
        })
        return


@app.route("/v1/responses", methods=["POST"])
def responses():
    auth_header = request.headers.get("Authorization", "")
    auth_prefix = auth_header[:20] if auth_header else "(空)"
    logger.info("=== 收到 /v1/responses 请求 ===")
    logger.info(f"Authorization: {auth_prefix}...")
    logger.info(f"Content-Type: {request.headers.get('Content-Type', 'N/A')}")

    data = request.get_json()
    if not data:
        logger.error("请求体为空或非 JSON")
        return jsonify({"error": "invalid request body"}), 400

    input_msgs = data.get("input", [])
    tools = data.get("tools")
    tool_choice = data.get("tool_choice")
    previous_response_id = data.get("previous_response_id")
    logger.info(f"input 消息数量: {len(input_msgs)}, tools: {'yes' if tools else 'no'}, tool_choice: {tool_choice}")
    if previous_response_id:
        logger.info(f"previous_response_id: {previous_response_id}, in_cache: {previous_response_id in _prev_responses}")

    # Log full details of first 3 messages for debugging
    for i in range(min(3, len(input_msgs))):
        msg = input_msgs[i]
        logger.debug(f"  消息[{i}] 完整 json: {json.dumps(msg, ensure_ascii=False)[:500]}")

    messages = []

    for i, msg in enumerate(input_msgs):
        role = msg.get("role", "")
        logger.debug(f"  消息[{i}]: role={role}, content_type={'list' if isinstance(msg.get('content'), list) else 'string'}")

        content_parts = msg.get("content")

        # Infer role from content type when role is empty/invalid
        if role not in ("system", "user", "assistant", "tool"):
            if isinstance(content_parts, list):
                types_in_content = [p.get("type", "") for p in content_parts]
                if "tool_call" in types_in_content:
                    role = "assistant"
                    logger.debug(f"  消息[{i}]: 推断 role='assistant' (包含 tool_call 内容)")
                elif "tool_result" in types_in_content:
                    role = "tool"
                    logger.debug(f"  消息[{i}]: 推断 role='tool' (包含 tool_result 内容)")
                else:
                    logger.debug(f"  消息[{i}]: 跳过无效 role={role!r}, types={types_in_content}")
                    continue
            else:
                logger.debug(f"  消息[{i}]: 跳过无效 role={role!r}")
                continue

        if role == "developer":
            role = "system"

        logger.debug(f"  消息[{i}] 完整: role={role!r}, keys={list(msg.keys())}, content_preview={str(msg.get('content'))[:120]!r}")
        if isinstance(content_parts, list):
            text_buf = ""
            tool_calls = None
            tool_call_id = None
            for part in content_parts:
                ptype = part.get("type")
                if ptype == "input_text":
                    text_buf += part.get("text", "")
                    logger.debug(f"    消息[{i}] part: type=input_text, len={len(part.get('text', ''))}")
                elif ptype == "tool_call":
                    # Convert tool_call part to Chat Completions tool_calls format
                    function = part.get("function", {})
                    tc = {
                        "id": part.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": function.get("name", ""),
                            "arguments": function.get("arguments", "{}")
                        }
                    }
                    if tool_calls is None:
                        tool_calls = []
                    tool_calls.append(tc)
                    logger.debug(f"    消息[{i}] part: type=tool_call, id={part.get('id', '')}")
                elif ptype == "tool_result":
                    tool_call_id = part.get("tool_call_id", "")
                    text_buf += part.get("content", "")
                    logger.debug(f"    消息[{i}] part: type=tool_result, tool_call_id={tool_call_id}")
                elif ptype == "reasoning":
                    # reasoning is not part of Chat Completions format, skip it
                    logger.debug(f"    消息[{i}] part: type=reasoning, 跳过")
                else:
                    logger.debug(f"    消息[{i}] part: type={ptype}, 跳过")

            if tool_calls:
                # Assistant message with tool_calls
                messages.append({
                    "role": role,
                    "content": text_buf or None,
                    "tool_calls": tool_calls
                })
            elif tool_call_id:
                # Tool result message
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": text_buf
                })
            else:
                # Normal text message
                messages.append({"role": role, "content": text_buf})
        else:
            content = msg.get("content", "")
            messages.append({"role": role, "content": content})

        logger.info(f"  转换后 消息[{i}]: role={role}, content_preview={str(messages[-1].get('content', ''))[:80]!r}")

    # Compute cache key for tool call repeat detection
    tool_cache_key = _messages_signature(messages)
    logger.debug(f"工具缓存 key: {tool_cache_key[:120]}")

    # Check if this is a repeat request that needs synthetic tool results
    # (Codex executes tool calls locally but doesn't send results back)
    cached_tc = _needs_tool_results(messages, _tool_call_log)
    if cached_tc:
        logger.info(f"检测到重复请求，注入 {len(cached_tc)} 个合成工具结果以打破循环")
        # Inject assistant message with tool_calls (required by DeepSeek before tool results)
        assistant_tc = []
        for tc in cached_tc:
            assistant_tc.append({
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc.get("name", "unknown"),
                    "arguments": tc.get("arguments", "{}")
                }
            })
        assistant_msg = {
            "role": "assistant",
            "content": None,
            "reasoning_content": "",
            "tool_calls": assistant_tc
        }
        messages.append(assistant_msg)
        # Inject tool results
        for tc in cached_tc:
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": "Tool executed successfully."
            })
        logger.info(f"已注入助理消息 + {len(cached_tc)} 个工具结果")
        # Strip tools so DeepSeek can't keep calling them
        tools = None
        logger.info("已移除工具定义，DeepSeek 将返回文字回复")

    logger.info("消息转换完成，开始流式请求 DeepSeek")

    # Generate response ID and cache conversation
    resp_id = f"resp_{uuid.uuid4()}"
    _prev_responses[resp_id] = {"ds_messages": list(messages)}
    while len(_prev_responses) > _MAX_CACHED:
        _prev_responses.pop(next(iter(_prev_responses)))

    def generate_with_cache():
        yield from generate_codex_stream(auth_header, messages, tools, tool_choice, resp_id, tool_cache_key)

    return Response(
        generate_with_cache(),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked"
        }
    )

# Shared model metadata
_MODEL_INFO = {
    "id": "deepseek-v4-flash",
    "object": "model",
    "created": 1778889145,
    "owned_by": "deepseek",
    "context_window": 65536,
    "max_output_tokens": 8192,
    "type": "model",
    "capabilities": {
        "chat": True,
        "streaming": True,
        "tools": True,
        "tool_choice": True
    },
    "pricing": {
        "input": 0.0,
        "output": 0.0,
        "cached_input": 0.0
    }
}
_EMPTY_MODELS_LIST = {"data": [_MODEL_INFO]}


@app.route("/v1/models", methods=["GET"])
def list_models():
    logger.info(f"=== GET /v1/models {dict(request.args)} ===")
    return jsonify(_EMPTY_MODELS_LIST)

@app.route("/v1/models/<model_id>", methods=["GET"])
def get_model(model_id):
    logger.info(f"=== GET /v1/models/{model_id} {dict(request.args)} ===")
    return jsonify({**_MODEL_INFO, "id": model_id})

def run_server(host="127.0.0.1", port=8787, log_dir=None):
    """Start the proxy server (blocking)."""
    logger.info("=" * 50)
    logger.info("DeepSeek Proxy starting")
    logger.info(f"Listening on http://{host}:{port}")
    logger.info(f"DeepSeek API: {DEEPSEEK_CHAT_URL}")
    logger.info(f"DEEPSEEK_API_KEY set: {bool(os.environ.get('DEEPSEEK_API_KEY'))}")
    logger.info("=" * 50)

    try:
        import uvicorn
        if asgi_app is None:
            raise ImportError("asgiref not installed")
        uvicorn.run(
            asgi_app,
            host=host,
            port=port,
            log_level="info",
        )
    except ImportError as e:
        logger.warning(f"ASGI dependency not available ({e}), falling back to Flask dev server")
        app.run(host=host, port=port, threaded=True)


PID_FILE = os.path.join(tempfile.gettempdir(), "deepseek-proxy.pid")
DEFAULT_LOG_DIR = os.path.join(tempfile.gettempdir(), "deepseek-proxy")
IS_WINDOWS = sys.platform == "win32"


def ensure_log_dir(log_dir):
    """Create log directory if it doesn't exist."""
    os.makedirs(log_dir, exist_ok=True)


def _cmd_name():
    """Return the command name users should see in help text."""
    arg0 = sys.argv[0] if sys.argv else "deepseek-proxy"
    base = os.path.basename(arg0)
    # Running as `python ds_proxy.py` or `python -m deepseek_proxy`
    if base in ("ds_proxy.py", "__main__.py") or "python" in base:
        return "python ds_proxy.py"
    return "deepseek-proxy"


def daemonize(log_dir):
    """Fork into background, redirect stdout/stderr to log file, and write PID file.

    Windows: fork() is unavailable; --daemon prints an error with
    platform-appropriate alternatives (start /B, Start-Process).
    """
    if IS_WINDOWS:
        cmd = _cmd_name()
        print("Error: --daemon is not supported on Windows.", file=sys.stderr)
        print("  Suggestions:", file=sys.stderr)
        print(f"    Run in foreground:  {cmd}", file=sys.stderr)
        print(f"    Background in cmd:  start /B {cmd}", file=sys.stderr)
        print(f"    Background in pwsh: Start-Process -NoNewWindow {cmd}", file=sys.stderr)
        sys.exit(1)

    pid = os.fork()
    if pid > 0:
        sys.exit(0)
    os.setsid()
    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    # Redirect stdin→/dev/null, stdout/stderr→log file at OS level
    # (os.dup2 keeps existing Python file objects working since the FD changes)
    ensure_log_dir(log_dir)
    log_path = os.path.join(log_dir, "deepseek-proxy.log")
    devnull_fd = os.open(os.devnull, os.O_RDONLY)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(devnull_fd, 0)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(devnull_fd)
    os.close(log_fd)

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def cmd_start(args):
    log_dir = getattr(args, "log_dir", None) or DEFAULT_LOG_DIR
    if args.daemon:
        daemonize(log_dir)
    run_server(host=args.host, port=args.port, log_dir=log_dir)


def cmd_stop(args):
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, signal.SIGTERM)
        os.remove(PID_FILE)
        print(f"✓ DeepSeek Proxy stopped (PID {pid})")
    except FileNotFoundError:
        print("DeepSeek Proxy is not running (no PID file found)")
        sys.exit(1)
    except (ProcessLookupError, OSError):
        # Windows os.kill may raise OSError (EINVAL) instead of
        # ProcessLookupError (ESRCH) for non-existent processes
        stale = os.path.exists(PID_FILE)
        if stale:
            os.remove(PID_FILE)
            print("DeepSeek Proxy was not running (stale PID file removed)")
        else:
            print("DeepSeek Proxy was not running")
        sys.exit(1)


def cmd_restart(args):
    try:
        cmd_stop(args)
    except SystemExit:
        pass
    args.daemon = True
    cmd_start(args)


def cmd_info(args):
    """Display deepseek-proxy installation and config information."""
    import shutil
    import subprocess
    from pathlib import Path

    version = "unknown"
    try:
        f = Path(__file__).resolve()
        # Try package version first
        try:
            import importlib.metadata as im
            version = im.version("deepseek-proxy")
        except Exception:
            # Read from pyproject.toml
            for parent in [f.parent] + list(f.parent.parents):
                p = parent / "pyproject.toml"
                if p.exists():
                    import re
                    m = re.search(r'version = "(.+?)"', p.read_text())
                    if m:
                        version = m.group(1)
                        break
    except Exception:
        pass

    script_path = Path(__file__).resolve()
    install_path = script_path.parent
    is_editable = (install_path / "pyproject.toml").exists()

    # ── proxy running status ──
    running = False
    pid = None
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        running = True
    except Exception:
        pass

    # Env vars for codex/Anthropic proxy routing
    env_vars = {
        "DEEPSEEK_API_KEY": "<set>" if os.environ.get("DEEPSEEK_API_KEY") else "<not set>",
    }

    # Dependencies status
    deps = {
        "flask": None,
        "requests": None,
        "uvicorn": None,
        "asgiref": None,
    }
    for mod in deps:
        try:
            __import__(mod)
            try:
                import importlib.metadata as im
                deps[mod] = im.version(mod)
            except Exception:
                deps[mod] = "installed"
        except ImportError:
            deps[mod] = "MISSING"

    print("=" * 54)
    print(f"  DeepSeek Proxy  v{version}")
    print("=" * 54)
    print()
    print("📦 Installation")
    print(f"  Script:   {script_path}")
    print(f"  Location: {install_path}")
    print(f"  Type:     {'editable install (pip install -e .)' if is_editable else 'system install or running from source'}")
    print()
    print("🔌 Dependencies")
    for mod, ver in deps.items():
        status = "✓" if ver != "MISSING" else "✗"
        print(f"  {status} {mod} ({ver})")
    print()
    print("⚙️  Runtime Status")
    log_dir_val = getattr(args, "log_dir", None) or DEFAULT_LOG_DIR
    log_file = os.path.join(log_dir_val, "deepseek-proxy.log")
    print(f"  Platform:  {'Windows' if IS_WINDOWS else sys.platform}")
    print(f"  Proxy:     {'running (PID ' + str(pid) + ')' if running else 'stopped'}")
    print(f"  Endpoint:  http://{args.host if args.command == 'start' else '127.0.0.1'}:{args.port if args.command == 'start' else '8787'}")
    print(f"  API Key:   {'✓ set' if os.environ.get('DEEPSEEK_API_KEY') else '✗ not set'}")
    print(f"  Log dir:   {log_dir_val}")
    print(f"  Log file:  {log_file} {'(exists)' if os.path.exists(log_file) else '(no log yet)'}")
    if IS_WINDOWS:
        print(f"  Daemon:    not supported on Windows — use foreground or `start /B {_cmd_name()}`")
    print()
    # ── Codex config ──
    codex_dir = Path.home() / ".codex"
    codex_config = codex_dir / "config.toml"
    codex_auth = codex_dir / "auth.json"
    codex_version = codex_dir / "version.json"

    wire_api = "N/A"
    codex_base_url = "N/A"
    codex_model = "N/A"
    cfg = {}
    if codex_config.exists():
        try:
            import tomllib
            cfg = tomllib.loads(codex_config.read_text())
        except Exception:
            try:
                import tomli as tomllib
                cfg = tomllib.loads(codex_config.read_text())
            except Exception:
                cfg = {}
        codex_model = cfg.get("model", "N/A")
        provider = cfg.get("model_providers", {}).get(cfg.get("model_provider", ""), {})
        codex_base_url = provider.get("base_url", "N/A")
        wire_api = provider.get("wire_api", "N/A")

    print("🔧 Codex Config")
    print(f"  Directory: {codex_dir}")
    print(f"  Version:   {json.loads(codex_version.read_text()).get('latest_version', 'N/A') if codex_version.exists() else 'N/A'}")
    print()
    print(f"  Config file: {'✓ ' + str(codex_config) if codex_config.exists() else '✗ not found'}")
    print(f"    Model:       {codex_model}")
    print(f"    Provider:    {cfg.get('model_provider', 'N/A')}")
    print(f"    Base URL:    {codex_base_url}")
    print(f"    Wire API:    {wire_api}")
    print(f"    Proxy link:  {'✓ points to this proxy' if '127.0.0.1:8787' in codex_base_url or 'localhost:8787' in codex_base_url else '✗ not pointing to this proxy'}")
    print()
    print(f"  Auth:    {'✓ ' + str(codex_auth) if codex_auth.exists() else '✗ not found'}")
    print(f"  History: {'✓ ' + str(codex_dir / 'history.jsonl') if (codex_dir / 'history.jsonl').exists() else '✗ not found'}")
    print(f"  Log:     {codex_dir / 'log/'}")
    print()
    print("🔀 Proxy Usage")
    print("  Target: codex (OpenAI Responses API → DeepSeek Chat Completions)")
    print("  Status: see Codex Config → Proxy link above")
    print()
    print("🌐 Environment Variables")
    for k, v in env_vars.items():
        print(f"  {k}={v}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="DeepSeek Proxy — translate Responses API ↔ Chat Completions API",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8787, help="Bind port (default: 8787)")
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR, help=f"Log directory (default: {DEFAULT_LOG_DIR})")

    sub = parser.add_subparsers(dest="command")

    info_parser = sub.add_parser("info", help="Show installation and configuration information")
    info_parser.set_defaults(func=cmd_info)

    start_parser = sub.add_parser("start", help="Start the proxy (default)")
    start_parser.add_argument("--daemon", "-d", action="store_true", help="Run in background")
    start_parser.set_defaults(func=cmd_start)

    stop_parser = sub.add_parser("stop", help="Stop the running proxy")
    stop_parser.set_defaults(func=cmd_stop)

    restart_parser = sub.add_parser("restart", help="Restart the proxy")
    restart_parser.set_defaults(func=cmd_restart)

    args = parser.parse_args()

    if args.command is None:
        # No subcommand: foreground start
        run_server(host=args.host, port=args.port)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
