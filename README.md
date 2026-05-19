---
language:
- en
- zh
license: mit
tags:
- tool-calling
- llm
- ollama
- codex
- proxy
- protocol-translation
- openai-api
- local-models
- sse
- agent-framework
pretty_name: Codex-Ollama Protocol Bridge
---

# Codex-Ollama Protocol Bridge

**Lightweight protocol translation proxy enabling local Ollama models to use Codex CLI tools.**

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-production-green.svg)]()
[![Lines](https://img.shields.io/badge/code-~480_effective-blue.svg)]()

## The Problem

Codex CLI v0.130.0+ supports local models via `--oss --local-provider ollama`. However, Ollama's `/v1/responses` endpoint (used by Codex) does not properly handle tool-calling with local models — all tool calls fail with `unsupported call` errors.

The same models produce correct `tool_calls` through Ollama's `/v1/chat/completions` endpoint. This bridge performs the protocol translation.

```
Codex → /v1/responses → [Bridge :11434] → /v1/chat/completions → Ollama :11433
                        ← SSE events ←                    ← JSON response ←
```

## Quick Start

```bash
# 1. Start Ollama on port 11433
OLLAMA_HOST="127.0.0.1:11433" ollama serve

# 2. Start the bridge
python3 proxy.py

# 3. Use Codex with any local model
codex --oss --local-provider ollama -m qwen3:14b
```

## Installation

```bash
# Clone or copy proxy.py
cp proxy.py /usr/local/bin/codex-bridge
chmod +x /usr/local/bin/codex-bridge

# Deploy as macOS daemon (auto-start on boot)
cp com.x.codex-bridge.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.x.codex-bridge.plist

# Or use the control script
./codex-bridge-ctl.sh start
```

## Usage

```
python3 proxy.py [--listen-port 11434] [--ollama-url http://localhost:11433]
                 [--debug] [--quiet] [--version]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--listen-port` | `11434` | Port the bridge listens on |
| `--ollama-url` | `http://localhost:11433` | Ollama base URL |
| `--debug` | off | Verbose request/response logging |
| `--quiet` | off | Errors only |
| `--max-body-size` | `4194304` | Max request body in bytes |
| `--version` | — | Print version and exit |

## How It Works

### Three Core Transformations

1. **Request Format Translation** — `/v1/responses` → `/v1/chat/completions`
   - `input` → `messages[]`
   - `instructions` → system message with tool-use directives
   - `stream: true` → `stream: false` (synthesize SSE ourselves)

2. **Tool Schema Simplification** — Reduce 4,100 tokens → ~800 tokens
   - Strip Codex's internal tools to essential parameters only
   - Codex fills in defaults for omitted params
   - 5× reduction dramatically improves local model accuracy

3. **SSE Event Synthesis** — Non-streaming JSON → SSE event stream
   - `response.created` → `in_progress` → `output_item.added` → ... → `completed`
   - Proper `output_index` for multi-output responses
   - Usage field normalization (`prompt_tokens` → `input_tokens`)

### Supported Tools

| Tool | Essential Params |
|------|-----------------|
| `exec_command` | cmd, workdir |
| `write_stdin` | session_id, chars |
| `spawn_agent` | agent_type, items, message |
| `view_image` | path |
| `update_plan` | plan |
| `request_user_input` | questions |
| `send_input` | target, message, items |
| `resume_agent` | id |
| `wait_agent` | targets |
| `close_agent` | target |

## Model Compatibility

| Model | Size | Tool Calls | Chinese | Recommended |
|-------|------|-----------|---------|-------------|
| qwen3:14b | 9.3GB | ✅ Stable | ✅ Native | 🏆 Flagship |
| huihui4:8b-a4b | 5.4GB | ✅ Good | ✅ | MoE option |
| Qwen2.5-Coder-7B | 7B | ⚠️ Moderate | ✅ | Backup |
| qwen2.5-coder:3b | 1.9GB | ⚠️ Weak | ✅ | Lightweight |
| llama3.1:8b | 4.9GB | ⚠️ Weak | ❌ | English only |

## Codex Aliases

Add to `~/.zshrc`:

```bash
# Flagship: qwen3:14b with tool calling
alias cx14='codex --oss --local-provider ollama -m qwen3:14b'
alias cx14e='codex exec --skip-git-repo-check --oss --local-provider ollama -m qwen3:14b'

# Lightweight: huihui4-8b-a4b MoE
alias cxhu='codex --oss --local-provider ollama -m huihui4-8b-a4b:latest'

# Health check
alias codex-health='bash ~/ai-assets/commands/codex-health.sh'
```

## Project Structure

```
codex-proxy/
├── proxy.py                     # Protocol bridge (807 lines, v1.1.0)
├── README.md                    # This file
├── LICENSE                      # MIT
├── codex-bridge-ctl.sh          # Service control script
├── com.x.codex-bridge.plist     # macOS launchd config
└── paper/
    ├── technical-report.md      # Full technical report (English)
    ├── technical-report-zh.md   # Full technical report (Chinese)
    ├── paper.tex                # LaTeX preprint (arXiv-ready)
    ├── paper.pdf                # Compiled PDF
    └── arxiv-submit.zip         # arXiv submission package
```

## Paper & Blog

| Type | Language | Link |
|------|----------|------|
| Technical Report | English | [paper/technical-report.md](paper/technical-report.md) |
| Technical Report | 中文 | [paper/technical-report-zh.md](paper/technical-report-zh.md) |
| LaTeX Preprint | English | [paper/paper.tex](paper/paper.tex) → [paper.pdf](paper/paper.pdf) |
| Blog Post | 中文 | [掘金：让本地模型在 Codex 里调用工具](https://juejin.cn/post/7641409122262040582) |

```
@misc{xuanyuan2026ptb,
  title={Lightweight Protocol-Translation Bridges for Heterogeneous
         LLM Tool-Calling APIs},
  author={xuanyuan},
  year={2026},
  note={Technical Report. Code: /Users/x/ai-assets/codex-proxy}
}
```

## Development

### Running Tests

Manual end-to-end test:

```bash
# Terminal 1: Start Ollama
OLLAMA_HOST="127.0.0.1:11433" ollama serve

# Terminal 2: Start bridge with debug logging
python3 proxy.py --debug

# Terminal 3: Test with Codex
codex exec --skip-git-repo-check --ephemeral --oss \
  --local-provider ollama -m huihui4-8b-a4b:latest \
  "list files in /tmp"
```

### Debugging

```bash
# Check bridge health
curl http://127.0.0.1:11434/__health

# Test /v1/responses translation directly
curl -X POST http://127.0.0.1:11434/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"huihui4-8b-a4b:latest","input":"ls /tmp","stream":false,...}'

# View logs
codex-bridge-ctl.sh logs
```

## License

MIT — see [LICENSE](LICENSE) file.

## Related Work

- [LiteLLM](https://github.com/BerriAI/litellm) — Universal LLM proxy
- [vLLM](https://github.com/vllm-project/vllm) — OpenAI-compatible server
- [Ollama](https://ollama.com) — Local LLM inference
- [Codex CLI](https://github.com/openai/codex) — OpenAI coding agent

## Citation

If you use this work, please cite:

```bibtex
@misc{xuanyuan2026ptb,
  title={Lightweight Protocol-Translation Bridges for Heterogeneous
         LLM Tool-Calling APIs: A Case Study on Codex-Ollama Interoperation},
  author={xuanyuan},
  year={2026},
  note={Technical Report v1.0}
}
```
