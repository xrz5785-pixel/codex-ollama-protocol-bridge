#!/usr/bin/env python3
"""
Codex-Ollama Protocol Bridge v1.1.0

Translates between Codex's /v1/responses (OpenAI Responses API) and
Ollama's /v1/chat/completions, enabling local models to use Codex tools.

Why:
  Ollama's /v1/responses endpoint accepts the request but local models
  don't produce structured function_call output items — they output text.
  The SAME models DO produce proper tool_calls via /v1/chat/completions.

Flow:
  Codex POST /v1/responses  →  bridge  →  Ollama POST /v1/chat/completions
       ◀── SSE responses events ← bridge ←  chat.completions JSON

Usage:
  python3 proxy.py [--listen-port 11434] [--ollama-url http://localhost:11433]
                   [--debug] [--quiet] [--version]
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
import uuid
from aiohttp import web, ClientSession, ClientTimeout

# ---------------------------------------------------------------------------
# Logging — level controlled by --debug / --quiet flags
# ---------------------------------------------------------------------------

log = logging.getLogger("codex-bridge")


def setup_logging(level: int = logging.INFO):
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stderr,
    )


def is_debug() -> bool:
    return log.isEnabledFor(logging.DEBUG)


# ---------------------------------------------------------------------------
# Defaults — overridden via CLI
# ---------------------------------------------------------------------------

OLLAMA_URL = "http://localhost:11433"
LISTEN_PORT = 11434
TIMEOUT = ClientTimeout(total=600)
MAX_BODY_SIZE = 4 * 1024 * 1024  # 4 MiB — protects against OOM


# ---------------------------------------------------------------------------
# Utility: usage normalization
# ---------------------------------------------------------------------------

def normalize_usage(usage: dict) -> dict:
    """Convert Ollama usage format to OpenAI Responses API format.

    Ollama:  {prompt_tokens, completion_tokens, total_tokens}
    OpenAI:  {input_tokens,   output_tokens,      total_tokens}
    """
    if not usage:
        return {}
    out = {"total_tokens": usage.get("total_tokens", 0)}
    if "prompt_tokens" in usage:
        out["input_tokens"] = usage["prompt_tokens"]
    if "completion_tokens" in usage:
        out["output_tokens"] = usage["completion_tokens"]
    if "input_tokens" in usage:
        out.setdefault("input_tokens", usage["input_tokens"])
    if "output_tokens" in usage:
        out.setdefault("output_tokens", usage["output_tokens"])
    return out


# ---------------------------------------------------------------------------
# Tool simplification: strip Codex internal tools to essential params
# ---------------------------------------------------------------------------

ESSENTIAL_PARAMS = {
    "exec_command":       ["cmd", "workdir"],
    "write_stdin":        ["session_id", "chars"],
    "update_plan":        ["plan"],
    "request_user_input": ["questions"],
    "view_image":         ["path"],
    "spawn_agent":        ["agent_type", "items", "message"],
    "send_input":         ["target", "message", "items"],
    "resume_agent":       ["id"],
    "wait_agent":         ["targets"],
    "close_agent":        ["target"],
}


def simplify_tools(tools: list) -> list:
    """Reduce Codex agent tools to essential parameters.

    Codex tools have up to 10 params each (~4100 tokens total). Local models
    struggle with this volume. We strip each tool to its 2–3 essential params;
    Codex's tool executor fills in defaults for the rest.
    """
    simplified = []
    for t in tools:
        fn = t.get("function", {})
        name = fn.get("name", "")
        if not name:
            continue

        if name in ESSENTIAL_PARAMS:
            keep = set(ESSENTIAL_PARAMS[name])
            params = fn.get("parameters", {})
            props = params.get("properties", {})
            required = params.get("required", [])

            new_props = {}
            for pname, pdef in props.items():
                if pname in keep:
                    new_props[pname] = {
                        "type": pdef.get("type", "string"),
                        "description": pdef.get("description", ""),
                    }
            new_required = [r for r in required if r in keep]

            fn_new = dict(fn)
            fn_new["parameters"] = {
                "type": "object",
                "properties": new_props,
                "required": new_required,
            }
            t_new = dict(t)
            t_new["function"] = fn_new
            simplified.append(t_new)
        else:
            simplified.append(t)

    return simplified


# ---------------------------------------------------------------------------
# Request conversion: /v1/responses → /v1/chat/completions
# ---------------------------------------------------------------------------

TOOL_SYSTEM_PROMPT = (
    "\n\nCRITICAL: You MUST call one of the provided tools to accomplish "
    "the user's request. NEVER respond with plain text describing what "
    "you would do. ALWAYS execute the appropriate tool directly."
)


def responses_to_chat(body: dict) -> dict:
    """Convert OpenAI Responses API request to Chat Completions format."""
    model = body.get("model", "")
    input_text = body.get("input", "")
    instructions = body.get("instructions", "")
    tools = body.get("tools", [])

    messages = []
    system_parts = []
    if instructions:
        system_parts.append(instructions)
    system_msg = "\n".join(system_parts) if system_parts else (
        "You are a coding agent. Use the provided tools when needed."
    )
    system_msg += TOOL_SYSTEM_PROMPT
    messages.append({"role": "system", "content": system_msg})

    if isinstance(input_text, str):
        messages.append({"role": "user", "content": input_text})
    elif isinstance(input_text, list):
        for item in input_text:
            if isinstance(item, dict):
                if item.get("type") == "message":
                    role = item.get("role", "user")
                    for c in item.get("content", []):
                        if c.get("type") == "input_text":
                            messages.append({"role": role, "content": c["text"]})
            elif isinstance(item, str):
                messages.append({"role": "user", "content": item})

    chat_req = {
        "model": model,
        "messages": messages,
        "stream": False,  # Always non-streaming to Ollama internally
    }

    if tools:
        processed = []
        for t in tools:
            if "function" not in t and "name" in t:
                processed.append({
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {}),
                    }
                })
            else:
                processed.append(t)
        processed = simplify_tools(processed)
        chat_req["tools"] = processed
        chat_req["tool_choice"] = "auto"

    return chat_req


# ---------------------------------------------------------------------------
# SSE Response builder: synthesizes Responses API events from complete
# chat/completions JSON response
# ---------------------------------------------------------------------------

class SSEResponseBuilder:
    """Builds OpenAI Responses API SSE events from chat completion output."""

    def __init__(self):
        self.response_id = f"resp_{uuid.uuid4().hex[:12]}"
        self.msg_id = f"msg_{uuid.uuid4().hex[:12]}"
        self.sequence = 0
        self.created = int(time.time())
        self.output_index = 0
        self.current_tool_call = None
        self.tool_calls_done = []
        self.content_buffer = ""
        self.started_output = False
        self.finish_reason = None

    def _event(self, event_type: str, data: dict) -> str:
        self.sequence += 1
        d = {"sequence_number": self.sequence, "type": event_type, **data}
        return f"event: {event_type}\ndata: {json.dumps(d)}\n\n"

    # —— lifecycle events ——

    def start(self, model: str) -> str:
        return self._event("response.created", {
            "response": {
                "background": False,
                "completed_at": None,
                "created_at": self.created,
                "error": None,
                "id": self.response_id,
                "incomplete_details": None,
                "instructions": None,
                "model": model,
                "object": "response",
                "output": [],
                "parallel_tool_calls": True,
                "status": "in_progress",
                "text": {"format": {"type": "text"}},
                "tool_choice": "auto",
                "tools": [],
                "usage": None,
            }
        })

    def in_progress(self) -> str:
        return self._event("response.in_progress", {
            "response": {"id": self.response_id, "status": "in_progress"}
        })

    def complete(self, usage: dict = None) -> str:
        output = []
        if self.content_buffer:
            output.append({
                "id": self.msg_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{
                    "type": "output_text",
                    "text": self.content_buffer,
                    "annotations": [],
                }],
            })
        for tc in self.tool_calls_done:
            output.append({
                "id": tc["id"],
                "type": "function_call",
                "status": "completed",
                "call_id": tc["call_id"],
                "name": tc["name"],
                "arguments": tc["arguments"],
            })

        return self._event("response.completed", {
            "response": {
                "id": self.response_id,
                "object": "response",
                "status": "completed",
                "output": output,
                "usage": usage or {},
            }
        })

    def error(self, message: str) -> str:
        return self._event("response.completed", {
            "response": {
                "id": self.response_id,
                "status": "failed",
                "output": [],
                "error": {"message": message, "code": "proxy_error"},
            }
        })

    # —— text content ——

    def add_text_delta(self, text: str) -> str:
        if not self.started_output:
            self.started_output = True
            if not self.tool_calls_done:
                self.output_index = 0
            msg_item = {
                "item": {
                    "id": self.msg_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                },
                "output_index": self.output_index,
            }
            out = self._event("response.output_item.added", msg_item)
            cp = {
                "content_index": 0,
                "item_id": self.msg_id,
                "output_index": self.output_index,
                "part": {"type": "output_text", "text": "", "annotations": []},
            }
            out += self._event("response.content_part.added", cp)
        else:
            out = ""

        delta = {
            "content_index": 0,
            "delta": text,
            "item_id": self.msg_id,
            "output_index": self.output_index,
        }
        out += self._event("response.output_text.delta", delta)
        self.content_buffer += text
        return out

    # —— tool calls ——

    def add_tool_call_start(self, call_id: str, name: str) -> str:
        if call_id is None:
            call_id = f"call_{uuid.uuid4().hex[:8]}"
        self.output_index = 0 if not self.started_output else self.output_index + 1
        self.current_tool_call = {
            "id": f"fc_{uuid.uuid4().hex[:8]}",
            "call_id": call_id,
            "name": name,
            "arguments": "",
        }
        return self._event("response.output_item.added", {
            "item": {
                "id": self.current_tool_call["id"],
                "type": "function_call",
                "status": "in_progress",
                "call_id": call_id,
                "name": name,
                "arguments": "",
            },
            "output_index": self.output_index,
        })

    def add_tool_args_delta(self, delta: str) -> str:
        if not self.current_tool_call:
            return ""
        self.current_tool_call["arguments"] += delta
        return self._event("response.function_call_arguments.delta", {
            "delta": delta,
            "item_id": self.current_tool_call["id"],
            "output_index": self.output_index,
        })

    def finish_tool_call(self) -> str:
        if not self.current_tool_call:
            return ""
        tc = self.current_tool_call
        args = tc["arguments"]
        events = (
            self._event("response.function_call_arguments.done", {
                "arguments": args,
                "item_id": tc["id"],
                "output_index": self.output_index,
            })
            + self._event("response.output_item.done", {
                "item": {
                    "id": tc["id"],
                    "type": "function_call",
                    "status": "completed",
                    "call_id": tc["call_id"],
                    "name": tc["name"],
                    "arguments": args,
                },
                "output_index": self.output_index,
            })
        )
        self.tool_calls_done.append(tc)
        self.current_tool_call = None
        self.started_output = True
        return events


# ---------------------------------------------------------------------------
# HTTP handlers
# ---------------------------------------------------------------------------

def _build_nonstream_resp(builder: SSEResponseBuilder, content: str,
                          tool_calls: list, usage: dict) -> dict:
    """Build non-streaming JSON response body."""
    output = []
    if content:
        output.append({
            "id": builder.msg_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{
                "type": "output_text",
                "text": content,
                "annotations": [],
            }],
        })
    for tc in tool_calls:
        f = tc.get("function", {})
        output.append({
            "id": f"fc_{uuid.uuid4().hex[:8]}",
            "type": "function_call",
            "status": "completed",
            "call_id": tc.get("id", ""),
            "name": f.get("name", ""),
            "arguments": f.get("arguments", ""),
        })
    return {
        "id": builder.response_id,
        "object": "response",
        "status": "completed",
        "output": output,
        "usage": usage,
    }


async def check_ollama_health(session: ClientSession) -> bool:
    """Return True if Ollama is reachable."""
    try:
        async with session.get(f"{OLLAMA_URL}/api/tags") as resp:
            return resp.status == 200
    except Exception:
        return False


async def proxy_handler(request: web.Request) -> web.StreamResponse:
    path = request.path
    method = request.method
    query_string = request.query_string

    # Read body with size limit
    try:
        body = await request.read()
    except Exception:
        return web.json_response(
            {"error": {"message": "Failed to read request body", "code": "read_error"}},
            status=400,
        )
    if len(body) > MAX_BODY_SIZE:
        log.warning(f"Request body too large: {len(body)}B (max {MAX_BODY_SIZE}B)")
        return web.json_response(
            {"error": {"message": "Request body too large", "code": "body_too_large"}},
            status=413,
        )

    req_headers = dict(request.headers)
    for h in ("Host", "Transfer-Encoding", "Connection"):
        req_headers.pop(h, None)

    # —— /api/pull: short-circuit for locally available models ——
    if path == "/api/pull" and body:
        try:
            pull_req = json.loads(body)
            model_name = pull_req.get("model", "") or pull_req.get("name", "")
            if model_name:
                async with ClientSession(timeout=ClientTimeout(total=10)) as session:
                    async with session.get(f"{OLLAMA_URL}/api/tags") as tags_resp:
                        tags_data = await tags_resp.json()
                        local_models = {m["name"] for m in tags_data.get("models", [])}
                        if model_name in local_models or any(
                            m.startswith(model_name.rstrip(":latest") + ":")
                            or m == model_name.rstrip(":latest")
                            for m in local_models
                        ):
                            log.info("Pull short-circuit: %s (local)", model_name)
                            resp = web.StreamResponse(status=200, headers={
                                "Content-Type": "application/x-ndjson",
                            })
                            await resp.prepare(request)
                            await resp.write(b'{"status":"success"}\n')
                            await resp.write_eof()
                            return resp
        except Exception as e:
            log.warning("Pull handler degraded: %s", e)

    # —— /v1/responses: convert to chat/completions ——
    if path == "/v1/responses" and body:
        try:
            req_data = json.loads(body)
            model = req_data.get("model", "")
            is_stream = req_data.get("stream", False)

            chat_req = responses_to_chat(req_data)
            new_body = json.dumps(chat_req).encode("utf-8")

            if is_debug():
                tools_list = chat_req.get("tools", [])
                msgs_list = chat_req.get("messages", [])
                log.debug("responses->chat: tools=%d msgs=%d stream=%s",
                          len(tools_list), len(msgs_list), is_stream)
                for i, m in enumerate(msgs_list):
                    c = m.get("content", "")
                    if isinstance(c, str):
                        log.debug("  msg[%d]: %s len=%d %r", i, m["role"], len(c),
                                  c[:120])
                for t in tools_list:
                    f = t.get("function", t)
                    params = f.get("parameters", {})
                    props = list(params.get("properties", {}).keys())
                    log.debug("  tool: %s params=%s required=%s",
                              f.get("name", "?"), props,
                              params.get("required", []))

            log.info("→ chat/completions model=%s stream=%s", model, is_stream)

            target_url = f"{OLLAMA_URL}/v1/chat/completions"
            if query_string:
                target_url += f"?{query_string}"

            req_headers["Content-Length"] = str(len(new_body))
            req_headers["Content-Type"] = "application/json"

            async with ClientSession(timeout=TIMEOUT) as session:
                async with session.request(
                    method="POST",
                    url=target_url,
                    headers=req_headers,
                    data=new_body,
                ) as upstream:
                    if upstream.status != 200:
                        err_text = await upstream.text()
                        log.error("Ollama returned %d: %s", upstream.status,
                                  err_text[:500])
                        if is_stream:
                            builder = SSEResponseBuilder()
                            resp = web.StreamResponse(status=200, headers={
                                "Content-Type": "text/event-stream",
                                "Cache-Control": "no-cache",
                            })
                            await resp.prepare(request)
                            await resp.write(
                                builder.start(model).encode())
                            await resp.write(
                                builder.error(f"Ollama {upstream.status}: {err_text[:200]}").encode())
                            await resp.write_eof()
                            return resp
                        else:
                            return web.json_response(
                                {"error": {"message": f"Ollama error: {err_text[:200]}",
                                           "code": "upstream_error"}},
                                status=502,
                            )

                    chat_resp = await upstream.json()
                    choice = chat_resp.get("choices", [{}])[0]
                    msg = choice.get("message", {})
                    content = msg.get("content", "") or ""
                    tool_calls = msg.get("tool_calls", [])
                    usage = normalize_usage(chat_resp.get("usage", {}))

                    log.info("← Ollama: content=%dB tool_calls=%d tok=%s",
                             len(content), len(tool_calls),
                             usage.get("total_tokens", "?"))
                    if is_debug() and content:
                        log.debug("  content: %r", content[:300])
                    for tc in tool_calls:
                        f = tc.get("function", {})
                        log.info("  tool_call: %s(%s)",
                                 f.get("name", "?"),
                                 f.get("arguments", "")[:80])

                    if is_stream:
                        return await _synthesize_sse(
                            request, model, content, tool_calls, usage)
                    else:
                        builder = SSEResponseBuilder()
                        resp_body = json.dumps(
                            _build_nonstream_resp(
                                builder, content, tool_calls, usage)
                        ).encode()
                        log.info("non-stream response: %dB", len(resp_body))
                        return web.Response(
                            body=resp_body,
                            content_type="application/json",
                        )

        except json.JSONDecodeError as e:
            log.error("JSON parse error: %s", e)
            return web.json_response(
                {"error": {"message": str(e), "code": "json_parse_error"}},
                status=400,
            )
        except asyncio.TimeoutError:
            log.error("Timeout talking to Ollama")
            return web.json_response(
                {"error": {"message": "Upstream timeout", "code": "timeout"}},
                status=504,
            )
        except Exception as e:
            log.error("Conversion error: %s", e, exc_info=is_debug())
            return web.json_response(
                {"error": {"message": str(e), "code": "conversion_error"}},
                status=502,
            )

    # —— All other requests: transparent proxy ——
    target_url = f"{OLLAMA_URL}{path}"
    if query_string:
        target_url += f"?{query_string}"

    log.info("→ %s %s (body=%dB)", method, path, len(body))

    try:
        async with ClientSession(timeout=TIMEOUT) as session:
            async with session.request(
                method=method,
                url=target_url,
                headers=req_headers,
                data=body,
            ) as upstream:
                resp = web.StreamResponse(
                    status=upstream.status,
                    headers={
                        k: v
                        for k, v in upstream.headers.items()
                        if k not in ("Transfer-Encoding", "Connection")
                    },
                )
                await resp.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
                log.info("← %s %s", upstream.status, path)
                return resp
    except asyncio.TimeoutError:
        log.error("Timeout proxying %s", path)
        return web.json_response(
            {"error": {"message": "Upstream timeout", "code": "timeout"}},
            status=504,
        )
    except Exception as e:
        log.error("Proxy error %s: %s", path, e)
        return web.json_response({"error": str(e)}, status=502)


async def _synthesize_sse(request: web.Request, model: str, content: str,
                          tool_calls: list, usage: dict) -> web.StreamResponse:
    """Build SSE event stream from complete chat/completions response."""
    builder = SSEResponseBuilder()

    resp = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
    await resp.prepare(request)
    await resp.write(builder.start(model).encode())
    await resp.write(builder.in_progress().encode())

    if tool_calls:
        for tc in tool_calls:
            f = tc.get("function", {})
            call_id = tc.get("id", "")
            name = f.get("name", "")
            args = f.get("arguments", "")

            if not name:
                log.warning("Skipping tool call with empty name")
                continue

            await resp.write(
                builder.add_tool_call_start(call_id, name).encode())
            if args:
                await resp.write(
                    builder.add_tool_args_delta(args).encode())
            await resp.write(
                builder.finish_tool_call().encode())

    if content:
        if builder.tool_calls_done:
            builder.started_output = False
            builder.output_index = len(builder.tool_calls_done)
        await resp.write(
            builder.add_text_delta(content).encode())

    await resp.write(builder.complete(usage).encode())
    await resp.write_eof()
    log.info("SSE synthesis: %d tool_calls + %dB text → %d events",
             len(tool_calls), len(content), builder.sequence)
    return resp


async def health_handler(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "target": OLLAMA_URL})


# ---------------------------------------------------------------------------
# Application factory & main
# ---------------------------------------------------------------------------

def create_app() -> web.Application:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", proxy_handler)
    app.router.add_get("/__health", health_handler)
    return app


def main():
    global OLLAMA_URL, LISTEN_PORT, MAX_BODY_SIZE

    parser = argparse.ArgumentParser(
        description="Codex-Ollama Protocol Bridge v1.1.0")
    parser.add_argument("--listen-port", type=int, default=11434,
                        help="Port to listen on (default: 11434)")
    parser.add_argument("--ollama-url", default="http://localhost:11433",
                        help="Ollama base URL (default: http://localhost:11433)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug-level logging")
    parser.add_argument("--quiet", action="store_true",
                        help="Errors only (overrides --debug)")
    parser.add_argument("--version", action="version", version="codex-bridge 1.1.0")
    parser.add_argument("--max-body-size", type=int, default=MAX_BODY_SIZE,
                        help=f"Max request body in bytes (default: {MAX_BODY_SIZE})")
    args = parser.parse_args()

    # Log level
    if args.quiet:
        level = logging.WARNING
    elif args.debug:
        level = logging.DEBUG
    else:
        level = logging.INFO
    setup_logging(level)

    OLLAMA_URL = args.ollama_url.rstrip("/")
    LISTEN_PORT = args.listen_port
    MAX_BODY_SIZE = args.max_body_size

    log.info("Codex-Ollama Bridge v1.1.0 :%d → %s", LISTEN_PORT, OLLAMA_URL)

    # Startup health check
    async def startup_check():
        async with ClientSession(timeout=ClientTimeout(total=5)) as session:
            if await check_ollama_health(session):
                log.info("Ollama reachable at %s", OLLAMA_URL)
            else:
                log.warning("Ollama NOT reachable at %s — proxy will start anyway",
                            OLLAMA_URL)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Graceful shutdown
    app = create_app()

    def shutdown():
        log.info("Shutting down...")
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown)
        except NotImplementedError:
            pass  # Windows

    try:
        loop.run_until_complete(startup_check())
        web.run_app(app, host="127.0.0.1", port=LISTEN_PORT, loop=loop)
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()
        log.info("Bridge stopped.")


if __name__ == "__main__":
    main()
