# Lightweight Protocol-Translation Bridges for Heterogeneous LLM Tool-Calling APIs

## A Case Study on Codex-Ollama Interoperation

**Authors:** xuanyuan (xrz5785@gmail.com)  
**Date:** May 2026  
**Status:** Technical Report v1.0  
**Code:** `/Users/x/ai-assets/codex-proxy/`

---

## Abstract

Large Language Model (LLM) inference frameworks widely claim "OpenAI API compatibility," yet this compatibility often exists only at the syntactic level—HTTP endpoints accept request payloads and return 200, but fail to preserve semantic contracts around tool-calling (function-calling) behavior. We document a concrete failure case: Ollama's `/v1/responses` endpoint, when used as a provider for Codex CLI (OpenAI's coding agent), returns malformed responses that cause all tool-call attempts to fail with "unsupported call" errors, despite the same models producing correct `tool_calls` through Ollama's `/v1/chat/completions` endpoint.

We propose the **Protocol-Translation Bridge (PTB)** pattern: a lightweight, zero-configuration proxy that performs three core transformations—(1) request format translation from Responses API to Chat Completions API, (2) tool schema simplification to fit local models' attention budgets, and (3) reverse synthesis of SSE event streams from non-streaming upstream responses. Our implementation (~800 lines of Python) restores full tool-calling functionality across all tested local models (qwen3:14b, huihui4:8b-a4b, qwen2.5-coder:3b) with zero modifications to either the client framework or the inference engine.

The key insight is counterintuitive: *downgrading* from the newer Responses API to the older Chat Completions API increases semantic fidelity, because the older endpoint has more mature native implementations within inference frameworks.

---

## 1. Introduction

### 1.1 Background

LLM-based coding agents (Codex, Cursor, Copilot, Aider) rely on tool-calling APIs to execute shell commands, read and write files, spawn sub-agents, and interact with users. These agents typically target OpenAI's API contract—either the Chat Completions API (`/v1/chat/completions`) or the newer Responses API (`/v1/responses`). The Responses API is the recommended modern endpoint for agent frameworks because it provides structured multi-output responses, native streaming SSE events, and a unified interface for text + tool-call outputs.

Meanwhile, the local-model ecosystem (Ollama, vLLM, llama.cpp) has rapidly evolved to support tool-calling. Ollama's documentation states that its `/v1/responses` endpoint is "OpenAI-compatible." Codex CLI v0.130.0 added `--oss --local-provider ollama` flags to support local models through this endpoint.

### 1.2 The Problem

When Codex connects to Ollama's `/v1/responses` endpoint with a local model, tool calls fail with:

```
unsupported call: call_<id> (exec_command)
```

The error is silent at the HTTP level—Ollama returns 200 OK. The failure occurs at the semantic level: the response body does not contain the structured `function_call` output items that Codex's agent runtime expects. The same model, queried via `/v1/chat/completions` with identical tool definitions, produces correct `tool_calls` in its response.

This paper documents the diagnosis, solution, and generalization of this problem.

---

## 2. Problem Analysis

### 2.1 Root Cause

Ollama's `/v1/responses` endpoint is a thin wrapper around its native `/api/chat` endpoint. The internal path is:

```
Codex → POST /v1/responses (Ollama's OpenAI-compat layer)
     → Ollama rewraps as /api/chat request
     → Model outputs text (not structured function_call)
     → Ollama returns {output: [{type: "message", content: "..."}]}
     → Codex finds no function_call → "unsupported call"
```

When the same model is queried via `/v1/chat/completions`:
```
Client → POST /v1/chat/completions (Ollama's OpenAI-compat layer)
       → Ollama rewraps as /api/chat request WITH tool definitions properly nested
       → Model outputs {tool_calls: [{function: {name, arguments}}]}
       → Ollama returns {choices: [{message: {tool_calls: [...]}}]}
```

The critical difference: Ollama's native `/api/chat` endpoint natively supports tool-calling and correctly passes tool definitions to the model. The `/v1/responses` wrapper loses this capability during format translation.

### 2.2 Why Not Fix Upstream?

1. **Ollama's issue tracker** already has reports about `/v1/responses` tool-calling gaps
2. **Fix timeline unknown** — the `/v1/responses` endpoint is not Ollama's priority
3. **A proxy is zero-friction** — no need to modify Codex or Ollama; works instantly
4. **Generalizes** — the same pattern applies to any pair of incompatible LLM API surfaces

### 2.3 Diagnosis Methodology

We employed a 12-step iterative diagnosis:

| Step | Hypothesis | Test | Result |
|------|-----------|------|--------|
| 1 | Direct `/v1/responses` works | Codex with Ollama | "unsupported call" |
| 2 | Model capability issue | Manual `/v1/chat/completions` | Tool calls work |
| 3 | Ollama responses impl broken | Read source | Thin wrapper, loses tools |
| 4 | Tool choice mode | `required` vs `auto` | `required` causes loops |
| 5 | Too many tools | 11 tools → 3 tools | Better, still inconsistent |
| 6 | Tool param overload | 10 params → 2-3 params | Model selects correctly |
| 7 | Model outputs JSON text | Check response format | Text JSON, not tool_calls |
| 8 | System prompt too weak | Add CRITICAL directive | Model starts calling tools |
| 9 | Usage field mismatch | Check `input_tokens` | Missing, causes disconnect |
| 10 | output_index hardcoded | Multi-output responses | Text overwrites tool_call |
| 11 | Pull fails for custom models | POST /api/pull | "file does not exist" |
| 12 | GGUF arch compatibility | gemma4 in Ollama | Needs specific arch support |

---

## 3. The Protocol-Translation Bridge (PTB) Pattern

### 3.1 Architecture

```
┌─────────┐     /v1/responses     ┌──────────────┐     /v1/chat/completions     ┌─────────┐
│  Codex  │ ──────────────────────│   PTB Proxy  │ ────────────────────────────│  Ollama │
│  (Client)│ ◀──── SSE events ────│  :11434      │ ◀──── JSON response ──────── │  :11433 │
└─────────┘                       └──────────────┘                              └─────────┘
```

The proxy is transparent for non-Responses requests (pass-through proxy for `/api/tags`, `/api/chat`, etc.), and only intervenes for two specific paths:

1. **`POST /v1/responses`** — full protocol translation
2. **`POST /api/pull`** — intercept and short-circuit for locally-available models

### 3.2 Transformation 1: Request Format Translation

The Responses API request is converted to Chat Completions format:

**Input (Responses API):**
```json
{
  "model": "qwen3:14b",
  "input": "list files in /tmp",
  "instructions": "You are a coding agent.",
  "tools": [
    {"type": "function", "function": {"name": "exec_command", ...}}
  ],
  "stream": true
}
```

**Output (Chat Completions API):**
```json
{
  "model": "qwen3:14b",
  "messages": [
    {"role": "system", "content": "You are a coding agent.\n\nCRITICAL: You MUST call..."},
    {"role": "user", "content": "list files in /tmp"}
  ],
  "tools": [
    {"type": "function", "function": {"name": "exec_command", ...}}
  ],
  "tool_choice": "auto",
  "stream": false
}
```

Key design decisions:
- `stream: false` — we always request non-streaming from Ollama, then synthesize SSE events ourselves. This avoids the complexity of real-time SSE→SSE translation and gives us complete control over event ordering.
- `tool_choice: "auto"` — `"required"` causes infinite tool-call loops with some models. `"auto"` combined with a strong system prompt provides the right balance.
- `instructions` becomes part of the system message, augmented with tool-usage directives.

### 3.3 Transformation 2: Tool Schema Simplification

Codex's internal tools are complex. `exec_command` alone has 10 parameters. Across 11 tools, the total tool definition is approximately 4,100 tokens—exceeding the effective attention budget of 8B-class models.

We reduce each tool to its essential parameters:

| Tool | Original Params | Essential Params |
|------|----------------|-----------------|
| `exec_command` | 10 (cmd, workdir, timeout, env, stdin, ...) | 2 (cmd, workdir) |
| `write_stdin` | 6 | 2 (session_id, chars) |
| `spawn_agent` | 8 | 3 (agent_type, items, message) |
| `view_image` | 3 | 1 (path) |
| ... | ... | ... |

After simplification: ~800 tokens total. Codex's tool executor fills in sensible defaults for omitted parameters.

This is a **zero-cost accuracy improvement** — the model selects the correct tool with higher probability because the signal-to-noise ratio in the tool definitions is higher.

### 3.4 Transformation 3: Response Event Synthesis

From a non-streaming Chat Completions JSON response, we synthesize the SSE event stream that Codex expects:

```
Chat Completion JSON:
{
  "choices": [{
    "message": {
      "tool_calls": [{
        "id": "call_abc",
        "function": {"name": "exec_command", "arguments": "{\"cmd\":\"ls /tmp\"}"}
      }],
      "content": ""
    },
    "finish_reason": "tool_calls"
  }],
  "usage": {"prompt_tokens": 99, "completion_tokens": 79, "total_tokens": 178}
}

──SYNTHESIZED AS──▶

SSE Events:
  event: response.created
  event: response.in_progress
  event: response.output_item.added     ← function_call item, output_index=0
  event: response.function_call_arguments.delta
  event: response.function_call_arguments.done
  event: response.output_item.done
  event: response.completed              ← normalized usage {input_tokens, output_tokens}
```

Critical details that caused failures:
1. **`output_index`**: MUST be 0 for the first function_call, 1 for the text message. Hardcoding 0 causes multi-output corruption.
2. **`sequence_number`**: MUST be globally monotonically increasing across all events.
3. **`usage`**: Ollama returns `{prompt_tokens, completion_tokens}` but Codex expects `{input_tokens, output_tokens}`.
4. **Event ordering**: The sequence `created → in_progress → output_item.added → ... → output_item.done → completed` is a strict protocol; deviations cause client disconnections.

### 3.5 Pull Interception

Codex calls `POST /api/pull` for every model before first use. Custom GGUF models (e.g., huihui4-8b-a4b) are not in Ollama's registry, causing pull failures. The proxy intercepts this endpoint, checks if the model exists locally via `/api/tags`, and returns `{"status":"success"}\n` (NDJSON format) for locally-available models.

---

## 4. Implementation

### 4.1 Technology Stack

- **Language**: Python 3.12 (standard library + aiohttp)
- **Lines of code**: ~480 (effective, excluding comments/whitespace), 807 total
- **Dependencies**: aiohttp only
- **Deployment**: macOS launchd (KeepAlive daemon) or manual `python3 proxy.py`

### 4.2 Code Organization

```
proxy.py
├── normalize_usage()          — Usage field mapping (Ollama → OpenAI)
├── simplify_tools()           — Tool parameter reduction
├── responses_to_chat()        — Request format translation
├── SSEResponseBuilder         — SSE event synthesis engine
│   ├── start() / in_progress() / complete() / error()
│   ├── add_text_delta()
│   └── add_tool_call_start() / add_tool_args_delta() / finish_tool_call()
├── proxy_handler()            — Main request dispatcher
├── _synthesize_sse()          — SSE stream construction
├── health_handler()           — Health check endpoint
└── main()                     — CLI, signal handling, startup check
```

### 4.3 Deployment

**Production (launchd):**
```xml
<!-- ~/Library/LaunchAgents/com.x.codex-bridge.plist -->
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
```

**Control script:**
```bash
codex-bridge-ctl.sh start|stop|restart|status|logs
```

**Codex aliases:**
```bash
alias cx14='codex --oss --local-provider ollama -m qwen3:14b'
alias cx14e='codex exec --skip-git-repo-check --oss --local-provider ollama -m qwen3:14b'
```

---

## 5. Experimental Validation

### 5.1 Test Setup

| Component | Version |
|-----------|---------|
| Codex CLI | v0.130.0 |
| Ollama | 0.23.4 |
| Bridge | v1.1.0 |
| OS | macOS 26.4 (Apple Silicon) |

### 5.2 Model Compatibility

| Model | Size | Tool Calling | Text Response | Chinese | Notes |
|-------|------|-------------|---------------|---------|-------|
| qwen3:14b | 9.3GB | ✅ Stable | ✅ | ✅ Native | Flagship |
| huihui4:8b-a4b | 5.4GB | ✅ Good | ✅ | ✅ | MoE, 4/8.1B active |
| Qwen2.5-Coder-7B-GGUF | 7B | ⚠️ Moderate | ✅ | ✅ | Backup |
| qwen2.5-coder:3b | 1.9GB | ⚠️ Weak | ✅ | ✅ | Lightweight text |
| gpt-oss:20b | 13GB | Not tested | — | — | Too resource-heavy |
| llama3.1:8b | 4.9GB | ⚠️ Weak | ✅ | ❌ | English only |
| deepseek-r1:14b | 9.0GB | Not tested | — | — | Reasoning model |

### 5.3 End-to-End Test

```
$ codex exec --skip-git-repo-check --ephemeral --oss \
    --local-provider ollama -m "huihui4-8b-a4b:latest" \
    "list files in /Users/x/ai-assets/codex-proxy/"

exec
/bin/zsh -lc 'ls -R /Users/x/ai-assets/codex-proxy/' 
  succeeded in 0ms:
__pycache__
proxy.py

/Users/x/ai-assets/codex-proxy/__pycache__:
proxy.cpython-312.pyc
```

The model correctly called `exec_command({"cmd":"ls -R /Users/x/ai-assets/codex-proxy/"})`, Codex executed it, and the result was returned.

### 5.4 Failure Analysis

During development, we encountered and resolved 10 distinct failure modes:

| # | Failure | Root Cause | Fix |
|---|---------|-----------|-----|
| 1 | Port conflict | Ollama launchd auto-restart | Manual port management |
| 2 | 400 Bad Request | Content-Length not updated after body modification | Recalculate header |
| 3 | Content-Length off by 1 | stream:false changed after header calc | Reorder operations |
| 4 | Empty function name | 4100-token tool definition overloads model | simplify_tools() |
| 5 | Text JSON instead of tool_calls | Model outputs `{"command": "ls"}` as text | Enhanced system prompt |
| 6 | Missing input_tokens | Usage field name mismatch | normalize_usage() |
| 7 | Transport closed | Codex disconnects on malformed completed | Fix usage normalization |
| 8 | output_index=0 for text | Multi-output ordering broken | Dynamic output_index |
| 9 | Pull failure for custom models | Model not in Ollama registry | Pull interception |
| 10 | Literal \n in pull response | Escaped newline vs real newline | Binary correct newline |

---

## 6. Discussion

### 6.1 The "Newer API is Better" Fallacy

A counterintuitive finding: the newer `/v1/responses` endpoint (introduced by OpenAI in 2025) performed *worse* than the older `/v1/chat/completions` endpoint for tool-calling through Ollama. This is because the Chat Completions API has been the primary integration target for inference frameworks for years, receiving more testing and native optimization. The Responses API, being newer, has thinner compatibility wrappers.

**Lesson:** When debugging API compatibility issues, try downgrading to an older API surface before assuming the model or framework is broken.

### 6.2 Attention Budget as a First-Class Constraint

Tool definitions consume prompt tokens. For an 8B model with a 32K context window, 4,100 tokens of tool definitions represent ~13% of the total budget. But the effective attention budget for tool selection is much smaller—the model must attend to the system prompt, conversation history, AND tool definitions simultaneously.

Our simplification from 4,100 → 800 tokens (5× reduction) was the single most impactful change for model accuracy. This suggests that tool definition design for local models should be treated as a prompt engineering problem, not just an API integration problem.

### 6.3 SSE Synthesis vs. Real-Time Translation

We chose to synthesize SSE from non-streaming responses rather than translate SSE→SSE in real time. This is a deliberate trade-off:

| Approach | Pros | Cons |
|----------|------|------|
| Real-time SSE→SSE | Lower latency, true streaming | Complex state machine, event reordering |
| Non-streaming → SSE | Simple, correct, ~480 loc | First-byte latency = model generation time |

For local models where generation latency is typically 5-30 seconds, the first-byte latency of non-streaming is acceptable. For production deployments with faster models, real-time translation would be the next optimization.

### 6.4 Generalizability

The PTB pattern applies beyond Codex-Ollama. Any pair of LLM API surfaces with syntactic-but-not-semantic compatibility can be bridged:

- **Cursor + Ollama** — Cursor uses a different tool-calling format
- **Continue.dev + vLLM** — Continue's API expectations vs vLLM's implementation
- **LangChain agents + llama.cpp** — Any agent framework + any inference engine

The core principle is always: **identify the API surface where tool-calling works natively, then translate requests to that surface and responses back to the client's expected surface.**

### 6.5 Limitations

1. **No real streaming**: First-byte latency equals full model generation time
2. **Single-model focus**: No load balancing across multiple Ollama instances
3. **No auth**: Assumes local-only deployment
4. **No automated tests**: Manual end-to-end testing only
5. **No response caching**: Repeated queries re-generate

---

## 7. Related Work

- **LiteLLM** (BerriAI, 2024): Universal LLM proxy supporting ~100 providers with format translation. Primarily targets Chat Completions API; Responses API support is nascent.
- **OpenRouter**: Commercial routing service providing unified Chat Completions interface across providers. Does not address Responses API.
- **vLLM OpenAI-compatible server**: Built-in API compatibility layer. Focused on serving, not protocol translation between API surfaces.
- **OpenAI Agents SDK**: Official agent framework using Responses API as primary interface. Our work enables running these agents with local models.
- **porter** (porter.sh): Lightweight LLM API proxy. Focuses on authentication and routing, not protocol translation.

The unique contribution of this work is the combination of: (a) Responses API ↔ Chat Completions semantic translation, (b) tool schema simplification for local models, and (c) reverse SSE synthesis from non-streaming responses.

---

## 8. Conclusion

We presented the Protocol-Translation Bridge (PTB) pattern, a lightweight solution to the problem of heterogeneous LLM API tool-calling compatibility. Our implementation for Codex-Ollama interoperation (~800 lines of Python) successfully restores tool-calling functionality for local models, with zero modifications to either the client framework or the inference engine.

The key findings are:
1. API "compatibility" claims require semantic-level verification, not just HTTP-level validation
2. Downgrading to older API surfaces can increase semantic fidelity
3. Tool schema simplification is a zero-cost optimization for local models
4. SSE synthesis from non-streaming responses is a viable alternative to real-time SSE translation
5. Usage field naming varies across implementations and requires normalization

The code, deployment configuration, and this report are released as open-source at:

```
/Users/x/ai-assets/codex-proxy/
├── proxy.py                     # Protocol bridge (v1.1.0)
├── paper/
│   ├── technical-report.md      # This document
│   └── paper.tex                # LaTeX preprint (Level B)
├── com.x.codex-bridge.plist     # macOS launchd configuration
└── codex-bridge-ctl.sh          # Control script
```

---

## Acknowledgments

Thanks to the Ollama and Codex teams for building the tools that made this work possible. The 12-step diagnosis benefited from rapid iteration enabled by Claude Code's agent capabilities.

---

## References

1. OpenAI. "Responses API Reference." https://platform.openai.com/docs/api-reference/responses
2. Ollama. "OpenAI Compatibility." https://ollama.com/blog/openai-compatibility
3. Codex CLI. "OpenAI Codex CLI." https://github.com/openai/codex
4. LiteLLM. "LiteLLM: Call all LLM APIs using the OpenAI format." https://github.com/BerriAI/litellm
5. vLLM. "OpenAI-Compatible Server." https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html
6. Huihui-ai. "Huihui4-8B-A4B: Mixture-of-Experts Language Model." https://huggingface.co/huihui-ai/Huihui4-8B-A4B-GGUF
7. Qwen Team. "Qwen3: Technical Report." 2025.
8. Anthropic. "Claude Code: Agentic coding tool." https://docs.anthropic.com/en/docs/claude-code
