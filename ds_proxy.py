import os
import sys
import signal
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

# 配置日志格式和级别
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger.setLevel(logging.DEBUG)
DEEPSEEK_CHAT_URL = "https://api.deepseek.com/v1/chat/completions"

# ASGI 包装（供 uvicorn 使用）
try:
    from asgiref.wsgi import WsgiToAsgi
    asgi_app = WsgiToAsgi(app)
except ImportError:
    asgi_app = None

CRLF = "\r\n"

def translate_usage(usage):
    """将 DeepSeek usage 格式转为 Responses API 格式。"""
    if not usage:
        return {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}
    result = {
        'input_tokens': usage.get('prompt_tokens', 0),
        'output_tokens': usage.get('completion_tokens', 0),
        'total_tokens': usage.get('total_tokens', 0),
    }
    # 保留 DeepSeek 独有的细节字段（可选）
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
    """生成 Responses API 标准 SSE 事件（含 event: 行，JSON 中使用 type 字段）。"""
    data['type'] = event_type
    payload = json.dumps(data, ensure_ascii=False)
    logger.debug(f"SSE << {event_type}")
    return f"event: {event_type}{CRLF}data: {payload}{CRLF}{CRLF}"


def generate_codex_stream(auth, messages):
    resp_id = f"resp_{uuid.uuid4()}"
    output_id = f"output_{uuid.uuid4()}"
    logger.info(f"开始生成响应，ID: {resp_id}")

    # 当前时间戳
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

    # 3. response.output_item.added
    yield sse_event("response.output_item.added", {
        'id': resp_id,
        'object': 'response.output_item.added',
        'item_id': output_id,
        'output_index': 0,
        'item': {
            'id': output_id,
            'object': 'response.output_item',
            'type': 'message',
            'role': 'assistant',
            'status': 'in_progress',
            'content': []
        }
    })

    # 3. response.content_part.added
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

    full_text = ""          # 累积全部文本
    usage_info = None       # 保存 DeepSeek 返回的 usage (原始格式)

    try:
        payload = {
            "model": "deepseek-v4-flash",
            "messages": messages,
            "stream": True,
            "temperature": 0.7,
            "max_tokens": 4096
        }

        logger.debug(f"发送到 DeepSeek 的请求: {json.dumps(payload, indent=2)}")

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
                        # 捕获 usage（一般最后一条 chunk 会带）
                        if 'usage' in data:
                            usage_info = data['usage']
                            logger.info(f"捕获 DeepSeek usage: {usage_info}")

                        if 'choices' in data and len(data['choices']) > 0:
                            choice = data['choices'][0]
                            delta = choice.get('delta', {})
                            content = delta.get('content', '')

                            # 记录 finish_reason
                            finish_reason = choice.get('finish_reason')
                            if finish_reason:
                                logger.info(f"DeepSeek finish_reason={finish_reason}")

                            if content:
                                full_text += content
                                logger.debug(f"  增量文本: {repr(content)}")
                                yield sse_event("response.output_text.delta", {
                                    "id": resp_id,
                                    "object": "response.output_text.delta",
                                    "output_index": 0,
                                    "content_index": 0,
                                    "delta": content
                                })
                    except Exception as e:
                        logger.error(f"解析 DeepSeek 数据失败: {e}")
                        logger.error(f"原始数据: {data_str}")
                        raise Exception(f"数据解析失败: {e}")

            logger.info(f"DeepSeek 流结束，共处理 {line_count} 行")

        # 5. response.output_text.done
        yield sse_event("response.output_text.done", {
            'id': resp_id,
            'object': 'response.output_text.done',
            'output_index': 0,
            'content_index': 0,
            'text': full_text
        })

        # 6. response.output_item.done
        yield sse_event("response.output_item.done", {
            'id': resp_id,
            'object': 'response.output_item.done',
            'output_index': 0,
            'item': {
                'id': output_id,
                'object': 'response.output_item',
                'type': 'message',
                'role': 'assistant',
                'status': 'completed',
                'content': [{'type': 'text', 'text': full_text}]
            }
        })

        # 7. response.completed
        translated_usage = translate_usage(usage_info)
        logger.info(f"最终 usage: {translated_usage}")
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
                        'id': output_id,
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

        # 注意：Responses API 流不需要 [DONE]
        logger.info(f"响应成功完成 (full_text_len={len(full_text)})")

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
    logger.info(f"input 消息数量: {len(input_msgs)}")
    logger.info(f"请求 model: {data.get('model', '(未指定)')}")
    logger.info(f"请求 settings: {json.dumps(data.get('settings', {}))}")

    messages = []
    for i, msg in enumerate(input_msgs):
        role = msg.get("role", "")
        logger.debug(f"  消息[{i}]: role={role}, content_type={'list' if isinstance(msg.get('content'), list) else 'string'}")

        if role == "developer":
            logger.debug(f"  消息[{i}]: 将 role 'developer' → 'system'")
            role = "system"

        content = ""
        if isinstance(msg.get("content"), list):
            for j, part in enumerate(msg["content"]):
                if part.get("type") == "input_text":
                    text = part.get("text", "")
                    content += text
                    logger.debug(f"    消息[{i}] part[{j}]: type=input_text, len={len(text)}")
                else:
                    logger.debug(f"    消息[{i}] part[{j}]: type={part.get('type')}, 跳过")
        else:
            content = msg.get("content", "")

        messages.append({"role": role, "content": content})
        logger.info(f"  转换后 消息[{i}]: role={role}, content_len={len(content)}, content_preview={content[:80]!r}")

    logger.info("消息转换完成，开始流式请求 DeepSeek")

    return Response(
        generate_codex_stream(auth_header, messages),
        content_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked"
        }
    )

# 模型公共信息
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

def run_server(host="127.0.0.1", port=8787):
    """Start the proxy server (blocking)."""
    logger.info("=" * 50)
    logger.info("DeepSeek Proxy 启动")
    logger.info(f"监听地址: http://{host}:{port}")
    logger.info(f"DeepSeek API: {DEEPSEEK_CHAT_URL}")
    logger.info(f"DEEPSEEK_API_KEY 已设置: {bool(os.environ.get('DEEPSEEK_API_KEY'))}")
    logger.info("=" * 50)

    try:
        import uvicorn
        if asgi_app is None:
            raise ImportError("asgiref 未安装")
        uvicorn.run(
            asgi_app,
            host=host,
            port=port,
            log_level="info",
        )
    except ImportError as e:
        logger.warning(f"ASGI 依赖未安装 ({e})，回退到 Flask 开发服务器")
        app.run(host=host, port=port, threaded=True)


PID_FILE = "/tmp/deepseek-proxy.pid"


def daemonize():
    """Fork into background and write PID file."""
    pid = os.fork()
    if pid > 0:
        # Parent process exits
        sys.exit(0)
    # Child continues
    os.setsid()
    # Second fork to fully detach
    pid = os.fork()
    if pid > 0:
        sys.exit(0)
    # Write PID file
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def cmd_start(args):
    if args.daemon:
        daemonize()
    run_server(host=args.host, port=args.port)


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
    except ProcessLookupError:
        os.remove(PID_FILE)
        print("DeepSeek Proxy was not running (stale PID file removed)")
        sys.exit(1)


def cmd_restart(args):
    try:
        cmd_stop(args)
    except SystemExit:
        pass
    args.daemon = True
    cmd_start(args)


def main():
    parser = argparse.ArgumentParser(
        description="DeepSeek Proxy — translate Responses API ↔ Chat Completions API",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8787, help="Bind port (default: 8787)")

    sub = parser.add_subparsers(dest="command")

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
