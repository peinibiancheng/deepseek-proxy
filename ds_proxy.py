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
        "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", ""),
        "ANTHROPIC_MODEL": os.environ.get("ANTHROPIC_MODEL", ""),
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

    # ── Claude Code proxy config ──
    claude_dir = Path.home() / ".claude"
    settings_file = claude_dir / "settings.json"
    settings_local = claude_dir / "settings.local.json"
    proxy_url = "N/A"
    proxy_model = "N/A"
    for f in [settings_file, settings_local]:
        if f.exists():
            try:
                sc = json.loads(f.read_text())
                proxy = sc.get("proxy", {})
                if proxy:
                    proxy_url = proxy.get("url", proxy_url)
                    proxy_model = proxy.get("model", proxy_model)
            except Exception:
                pass

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
    print("🔧 Claude Code Config")
    print(f"  ~/.claude/settings.json          {'✓ found' if settings_file.exists() else '✗ not found'}")
    print(f"  ~/.claude/settings.local.json    {'✓ found' if settings_local.exists() else '✗ not found'}")
    print()
    print("🔀 Two Ways to Route Through DeepSeek")
    print()
    anthropic_base = os.environ.get("ANTHROPIC_BASE_URL", "")
    print("  ① Direct DeepSeek Anthropic-compatible endpoint")
    print(f"     Endpoint: {anthropic_base or 'https://api.deepseek.com/anthropic'}")
    print(f"     Status:   {'✓ configured' if anthropic_base else '✗ not configured'}")
    print("     Use case: Native Claude API mode (recommended, no proxy needed)")
    print()
    print("  ② Via deepseek-proxy (Responses API ↔ Chat Completions)")
    print(f"     Status: {'✓ configured' if proxy_url != 'N/A' else '✗ not configured'}")
    if proxy_url != "N/A":
        print(f"     Proxy URL:   {proxy_url}")
        print(f"     Proxy model: {proxy_model}")
    print("     Use case: codex using OpenAI Responses API protocol")
    print()
    print("🌐 Environment Variables")
    for k, v in env_vars.items():
        if k == "DEEPSEEK_API_KEY":
            print(f"  {k}={v}")
        elif v:
            print(f"  {k}={v}")
        else:
            print(f"  {k}=<not set>")
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
