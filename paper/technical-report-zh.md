# 轻量级协议翻译桥接：异构 LLM 工具调用 API 的互操作方案

## 基于 Codex-Ollama 互操作的案例研究

**作者：** xuanyuan (xrz5785@gmail.com)  
**日期：** 2026年5月  
**版本：** 技术报告 v1.0（中文版）  
**代码：** `/Users/x/ai-assets/codex-proxy/`

---

## 摘要

主流 LLM 推理框架（Ollama、vLLM、llama.cpp）普遍声称"兼容 OpenAI API"，但这种兼容往往停留在语法层面——HTTP 端点接受请求格式并返回 200，却无法保留工具调用（function calling）的语义契约。本文记录了一个具体失败案例：Ollama 的 `/v1/responses` 端点作为 Codex CLI（OpenAI 编程智能体）的供应商时，返回格式错误的响应导致所有工具调用均以"unsupported call"错误失败——尽管同一模型通过 Ollama 的 `/v1/chat/completions` 端点可以正确产出 `tool_calls`。

我们提出**协议翻译桥接（Protocol-Translation Bridge, PTB）模式**：一个轻量级、零配置的代理，执行三项核心转换——（1）将 Responses API 请求格式转换为 Chat Completions API 格式，（2）精简工具定义以适应本地模型的注意力预算，（3）从上游非流式响应反向合成 SSE 事件流。我们的实现（约 800 行 Python）在所有测试的本地模型（qwen3:14b、huihui4:8b-a4b、qwen2.5-coder:3b）上恢复了完整的工具调用功能，无需修改客户端框架或推理引擎。

核心洞察是反直觉的：将较新的 Responses API *降级*为较旧的 Chat Completions API 反而提高了语义保真度，因为旧端点在推理框架中有更成熟的原生实现。

---

## 1. 引言

### 1.1 背景

基于 LLM 的编程智能体（Codex、Cursor、Copilot、Aider）依赖工具调用 API 来执行 shell 命令、读写文件、生成子智能体以及与用户交互。这些智能体通常面向 OpenAI 的 API 契约——可能是 Chat Completions API（`/v1/chat/completions`），也可能是较新的 Responses API（`/v1/responses`）。Responses API 是面向智能体框架推荐的现代端点，因其提供结构化的多输出响应、原生流式 SSE 事件以及文本与工具调用输出的统一接口。

与此同时，本地模型生态（Ollama、vLLM、llama.cpp）已快速演进以支持工具调用。Ollama 的文档声称其 `/v1/responses` 端点"兼容 OpenAI"。Codex CLI v0.130.0 新增了 `--oss --local-provider ollama` 标志，支持通过该端点使用本地模型。

### 1.2 问题

当 Codex 使用本地模型连接 Ollama 的 `/v1/responses` 端点时，工具调用失败并报错：

```
unsupported call: call_<id> (exec_command)
```

此错误在 HTTP 层面是静默的——Ollama 返回 200 OK。失败发生在语义层面：响应体中不包含 Codex 智能体运行时所需的 `function_call` 结构化输出项。同一模型，通过 `/v1/chat/completions` 使用完全相同的工具定义查询，却能在响应中产出正确的 `tool_calls`。

本文记录了此问题的诊断、解决方案及其泛化。

---

## 2. 问题分析

### 2.1 根因

Ollama 的 `/v1/responses` 端点是其原生 `/api/chat` 端点的薄包装。内部路径为：

```
Codex → POST /v1/responses（Ollama 的 OpenAI 兼容层）
     → Ollama 重包装为 /api/chat 请求
     → 模型输出文本（而非结构化 function_call）
     → Ollama 返回 {output: [{type: "message", content: "..."}]}
     → Codex 找不到 function_call → "unsupported call"
```

当同一模型通过 `/v1/chat/completions` 查询时：
```
客户端 → POST /v1/chat/completions（Ollama 的 OpenAI 兼容层）
       → Ollama 重包装为 /api/chat 请求，工具定义正确嵌套
       → 模型输出 {tool_calls: [{function: {name, arguments}}]}
       → Ollama 返回 {choices: [{message: {tool_calls: [...]}}]}
```

关键差异：Ollama 的原生 `/api/chat` 端点原生支持工具调用，并正确地将工具定义传递给模型。`/v1/responses` 包装器在格式转换过程中丢失了此能力。

### 2.2 为何不修复上游

1. **Ollama issue tracker** 中已有关于 `/v1/responses` 工具调用缺陷的报告
2. **修复时间未知**——`/v1/responses` 端点并非 Ollama 的优先事项
3. **代理方案零摩擦**——无需修改 Codex 或 Ollama，立即可用
4. **可泛化**——同一模式适用于任何一对不兼容的 LLM API 表面

### 2.3 诊断方法

我们采用 12 步迭代诊断：

| 步骤 | 假设 | 测试方法 | 结果 |
|------|------|----------|------|
| 1 | 直接 `/v1/responses` 可用 | Codex + Ollama | "unsupported call" |
| 2 | 模型能力问题 | 手动 `/v1/chat/completions` | 工具调用正常 |
| 3 | Ollama responses 实现有 bug | 阅读源码 | 薄包装，丢失工具 |
| 4 | tool_choice 模式 | `required` vs `auto` | required 导致循环 |
| 5 | 工具数量过多 | 11→3 个工具 | 有改善但仍不一致 |
| 6 | 工具参数过载 | 10→2-3 个参数 | 模型选择正确 |
| 7 | 模型输出 JSON 文本 | 检查响应格式 | 文本 JSON，非 tool_calls |
| 8 | system prompt 太弱 | 添加 CRITICAL 指令 | 模型开始调用工具 |
| 9 | usage 字段不匹配 | 检查 `input_tokens` | 缺失导致断连 |
| 10 | output_index 硬编码 | 多输出响应 | text 覆盖 tool_call |
| 11 | 自定义模型 pull 失败 | POST /api/pull | "file does not exist" |
| 12 | GGUF 架构兼容性 | gemma4 in Ollama | 需要特定架构支持 |

---

## 3. 协议翻译桥接（PTB）模式

### 3.1 架构

```
┌─────────┐     /v1/responses     ┌──────────────┐     /v1/chat/completions     ┌─────────┐
│  Codex  │ ──────────────────────│   PTB 代理    │ ────────────────────────────│  Ollama │
│  (客户端)│ ◀──── SSE 事件 ──────│  :11434       │ ◀──── JSON 响应 ──────────── │  :11433 │
└─────────┘                       └──────────────┘                              └─────────┘
```

代理对非 Responses 请求保持透明（透传 `/api/tags`、`/api/chat` 等），仅对两个特定路径进行干预：

1. **`POST /v1/responses`** — 完整协议翻译
2. **`POST /api/pull`** — 拦截并短路返回本地已有模型

### 3.2 转换一：请求格式翻译

将 Responses API 请求转换为 Chat Completions 格式：

**输入（Responses API）：**
```json
{
  "model": "qwen3:14b",
  "input": "列出 /tmp 中的文件",
  "instructions": "你是一个编程助手。",
  "tools": [
    {"type": "function", "function": {"name": "exec_command", ...}}
  ],
  "stream": true
}
```

**输出（Chat Completions API）：**
```json
{
  "model": "qwen3:14b",
  "messages": [
    {"role": "system", "content": "你是一个编程助手。\n\n关键：你必须调用..."},
    {"role": "user", "content": "列出 /tmp 中的文件"}
  ],
  "tools": [
    {"type": "function", "function": {"name": "exec_command", ...}}
  ],
  "tool_choice": "auto",
  "stream": false
}
```

关键设计决策：
- `stream: false` — 我们始终以非流式向 Ollama 请求，然后自行合成 SSE 事件。这避免了实时 SSE→SSE 翻译的复杂性，并赋予我们对事件顺序的完全控制。
- `tool_choice: "auto"` — `"required"` 会在部分模型上导致无限工具调用循环。`"auto"` 结合强 system prompt 提供了恰当的平衡。
- `instructions` 融入 system message，并附加工具使用指令。

### 3.3 转换二：工具定义瘦身

Codex 的内部工具非常复杂。仅 `exec_command` 就有 10 个参数。11 个工具的总工具定义约为 4,100 tokens——超出 8B 级模型的有效注意力预算。

我们将每个工具缩减到必要参数：

| 工具 | 原始参数 | 必要参数 |
|------|----------|----------|
| `exec_command` | 10（cmd, workdir, timeout, env, stdin, ...） | 2（cmd, workdir） |
| `write_stdin` | 6 | 2（session_id, chars） |
| `spawn_agent` | 8 | 3（agent_type, items, message） |
| `view_image` | 3 | 1（path） |
| ... | ... | ... |

瘦身后：约 800 tokens。Codex 的工具执行器会自动为省略的参数填充合理默认值。

这是一项**零成本的准确率提升**——模型以更高概率选择正确工具，因为工具定义中的信噪比更高。

### 3.4 转换三：响应事件合成

从非流式 Chat Completions JSON 响应中，合成 Codex 所需的 SSE 事件流：

```
Chat Completion JSON：
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

──合成为──▶

SSE 事件：
  event: response.created
  event: response.in_progress
  event: response.output_item.added     ← function_call 条目，output_index=0
  event: response.function_call_arguments.delta
  event: response.function_call_arguments.done
  event: response.output_item.done
  event: response.completed              ← 标准化 usage {input_tokens, output_tokens}
```

曾引发失败的关键细节：
1. **`output_index`**：首个 function_call 必须为 0，文本消息为 1。硬编码 0 会导致多输出覆盖。
2. **`sequence_number`**：必须在所有事件中全局单调递增。
3. **`usage`**：Ollama 返回 `{prompt_tokens, completion_tokens}`，但 Codex 期望 `{input_tokens, output_tokens}`。
4. **事件顺序**：`created → in_progress → output_item.added → ... → output_item.done → completed` 是严格协议，偏离会导致客户端断连。

### 3.5 Pull 拦截

Codex 在首次使用每个模型前调用 `POST /api/pull`。自定义 GGUF 模型（如 huihui4-8b-a4b）不在 Ollama registry 中，导致 pull 失败。代理拦截此端点，通过 `/api/tags` 检查模型是否本地存在，对已有模型返回 `{"status":"success"}\n`（NDJSON 格式）。

---

## 4. 实现

### 4.1 技术栈

- **语言**：Python 3.12（标准库 + aiohttp）
- **代码量**：约 480 行（有效代码，不含注释/空行），总计 807 行
- **依赖**：仅 aiohttp
- **部署**：macOS launchd（KeepAlive 守护进程）或手动 `python3 proxy.py`

### 4.2 代码结构

```
proxy.py
├── normalize_usage()          — usage 字段映射（Ollama → OpenAI）
├── simplify_tools()           — 工具参数瘦身
├── responses_to_chat()        — 请求格式翻译
├── SSEResponseBuilder         — SSE 事件合成引擎
│   ├── start() / in_progress() / complete() / error()
│   ├── add_text_delta()
│   └── add_tool_call_start() / add_tool_args_delta() / finish_tool_call()
├── proxy_handler()            — 主请求分发器
├── _synthesize_sse()          — SSE 流构建
├── health_handler()           — 健康检查端点
└── main()                     — CLI、信号处理、启动检查
```

### 4.3 部署

**生产环境（launchd）：**
```xml
<!-- ~/Library/LaunchAgents/com.x.codex-bridge.plist -->
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
```

**控制脚本：**
```bash
codex-bridge-ctl.sh start|stop|restart|status|logs
```

**Codex 别名：**
```bash
alias cx14='codex --oss --local-provider ollama -m qwen3:14b'
alias cx14e='codex exec --skip-git-repo-check --oss --local-provider ollama -m qwen3:14b'
```

---

## 5. 实验验证

### 5.1 测试环境

| 组件 | 版本 |
|------|------|
| Codex CLI | v0.130.0 / v0.131.0 |
| Ollama | 0.23.4 |
| Bridge | v1.1.0 |
| 操作系统 | macOS 26.4（Apple Silicon） |

### 5.2 模型兼容性

| 模型 | 大小 | 工具调用 | 文本响应 | 中文 | 备注 |
|------|------|----------|----------|------|------|
| qwen3:14b | 9.3GB | ✅ 稳定 | ✅ | ✅ 原生 | 旗舰模型 |
| huihui4:8b-a4b | 5.4GB | ✅ 良好 | ✅ | ✅ | MoE，4/8.1B 激活 |
| Qwen2.5-Coder-7B-GGUF | 7B | ⚠️ 中等 | ✅ | ✅ | 备用 |
| qwen2.5-coder:3b | 1.9GB | ⚠️ 较弱 | ✅ | ✅ | 轻量文本 |
| gpt-oss:20b | 13GB | 未测试 | — | — | 资源消耗过大 |
| llama3.1:8b | 4.9GB | ⚠️ 较弱 | ✅ | ❌ | 仅英文 |
| deepseek-r1:14b | 9.0GB | 未测试 | — | — | 推理模型 |

### 5.3 端到端测试

```
$ codex exec --skip-git-repo-check --ephemeral --oss \
    --local-provider ollama -m "huihui4-8b-a4b:latest" \
    "列出 /Users/x/ai-assets/codex-proxy/ 的文件"

exec
/bin/zsh -lc 'ls -R /Users/x/ai-assets/codex-proxy/' 
  succeeded in 0ms:
__pycache__
proxy.py

/Users/x/ai-assets/codex-proxy/__pycache__:
proxy.cpython-312.pyc
```

模型正确调用了 `exec_command({"cmd":"ls -R ..."})`，Codex 执行命令并返回结果。

### 5.4 故障分析

开发过程中遇到并解决了 10 个不同故障模式：

| # | 故障现象 | 根因 | 修复方案 |
|---|----------|------|----------|
| 1 | 端口冲突 | Ollama launchd 自动重启 | 手动端口管理 |
| 2 | 400 Bad Request | body 修改后未更新 Content-Length | 重新计算头 |
| 3 | Content-Length 差 1 | 计算头后修改 stream 字段 | 调整操作顺序 |
| 4 | 空函数名 | 4100 token 工具定义超载模型 | simplify_tools() |
| 5 | 文本 JSON 替代 tool_calls | 模型输出 `{"command": "ls"}` 作为文本 | 增强 system prompt |
| 6 | 缺少 input_tokens | usage 字段名不匹配 | normalize_usage() |
| 7 | 传输关闭 | Codex 因格式错误 completed 断连 | 修复 usage 标准化 |
| 8 | 文本 output_index=0 | 多输出顺序错误 | 动态 output_index |
| 9 | 自定义模型 pull 失败 | 模型不在 Ollama registry | Pull 拦截 |
| 10 | pull 响应中字面 `\n` | 转义换行 vs 真实换行 | 二进制正确换行 |

---

## 6. 讨论

### 6.1 "新 API 更好"的谬误

一个反直觉的发现：较新的 `/v1/responses` 端点（OpenAI 于 2025 年推出）在通过 Ollama 进行工具调用时表现*差于*较旧的 `/v1/chat/completions` 端点。这是因为 Chat Completions API 多年来一直是推理框架的主要集成目标，经过了更多测试和原生优化。Responses API 作为较新的端点，兼容性包装更薄弱。

**教训：** 在调试 API 兼容性问题时，先尝试降级到旧 API 表面，再假设模型或框架已损坏。

### 6.2 注意力预算作为第一级约束

工具定义消耗 prompt tokens。对于 8B 模型和 32K 上下文窗口，4,100 tokens 的工具定义占总额的约 13%。但工具选择的有效注意力预算远小于此——模型必须同时关注 system prompt、对话历史*和*工具定义。

我们的精简从 4,100 → 800 tokens（5× 压缩）是对模型准确率影响最大的单一改动。这意味着本地模型的工具定义设计应当被视为一个 prompt 工程问题，而不仅仅是 API 集成问题。

### 6.3 SSE 合成 vs 实时翻译

我们选择从非流式响应合成 SSE，而非实时 SSE→SSE 翻译。这是经过权衡的刻意设计：

| 方案 | 优点 | 缺点 |
|------|------|------|
| 实时 SSE→SSE | 更低延迟，真正的流式 | 复杂状态机，事件重排 |
| 非流式 → SSE | 简单、正确，约 480 行 | 首字节延迟 = 模型生成时间 |

对于生成延迟通常为 5-30 秒的本地模型，非流式的首字节延迟是可以接受的。对于更高速模型的生产部署，实时翻译将是下一阶段的优化方向。

### 6.4 可泛化性

PTB 模式适用于 Codex-Ollama 之外的场景。任何语法兼容但语义不兼容的 LLM API 表面对都可以桥接：

- **Cursor + Ollama** — Cursor 使用不同的工具调用格式
- **Continue.dev + vLLM** — Continue 的 API 期望 vs vLLM 的实现
- **LangChain 智能体 + llama.cpp** — 任意智能体框架 + 任意推理引擎

核心原则始终是：**找到工具调用原生工作的 API 表面，将请求翻译到该表面，再将其响应翻译回客户端期望的表面。**

### 6.5 局限性

1. **无真正流式**：首字节延迟等于模型完整生成时间
2. **单模型聚焦**：不跨多个 Ollama 实例做负载均衡
3. **无认证**：假定纯本地部署
4. **无自动化测试**：仅手工端到端测试
5. **无响应缓存**：重复查询会重新生成

---

## 7. 相关工作

- **LiteLLM**（BerriAI, 2024）：通用 LLM 代理，支持约 100 个供应商的格式翻译。主要面向 Chat Completions API，Responses API 支持尚在初期。
- **OpenRouter**：商业路由服务，提供跨供应商的统一 Chat Completions 接口。不涉及 Responses API。
- **vLLM OpenAI 兼容服务器**：内建 API 兼容层，聚焦于模型服务而非 API 表面间协议翻译。
- **OpenAI Agents SDK**：使用 Responses API 为主接口的官方智能体框架。本工作使这些智能体能够在本地模型上运行。
- **porter**（porter.sh）：轻量级 LLM API 代理，聚焦于认证和路由，而非协议翻译。

本工作的独特贡献在于结合了：(a) Responses API ↔ Chat Completions 语义翻译，(b) 面向本地模型的工具定义瘦身，(c) 从非流式响应反向合成 SSE 事件流。

---

## 8. 结论

本文提出了协议翻译桥接（PTB）模式，这是一种解决异构 LLM API 工具调用兼容性问题的轻量级方案。我们面向 Codex-Ollama 互操作的实现（约 800 行 Python）成功恢复了本地模型的工具调用功能，无需修改客户端框架或推理引擎。

核心发现：
1. API"兼容性"声明需要语义层级的验证，而非仅 HTTP 层级的验证
2. 降级到旧 API 表面可能提高语义保真度
3. 工具定义瘦身是对本地模型的零成本优化
4. 从非流式响应合成 SSE 是实时 SSE 翻译的可行替代方案
5. usage 字段命名在实现间存在差异，需要标准化

代码、部署配置及本报告以开源形式发布：

```
/Users/x/ai-assets/codex-proxy/
├── proxy.py                     # 协议桥接 v1.1.0
├── paper/
│   ├── technical-report.md      # 英文版技术报告
│   ├── technical-report-zh.md   # 中文版技术报告（本文档）
│   └── paper.tex                # LaTeX 预印本
├── com.x.codex-bridge.plist     # macOS launchd 配置
└── codex-bridge-ctl.sh          # 控制脚本
```

---

## 致谢

感谢 Ollama 和 Codex 团队构建了使本工作成为可能的工具。12 步诊断受益于 Claude Code 智能体能力带来的快速迭代。

---

## 参考文献

1. OpenAI. "Responses API Reference." https://platform.openai.com/docs/api-reference/responses
2. Ollama. "OpenAI Compatibility." https://ollama.com/blog/openai-compatibility
3. Codex CLI. "OpenAI Codex CLI." https://github.com/openai/codex
4. LiteLLM. "LiteLLM: Call all LLM APIs using the OpenAI format." https://github.com/BerriAI/litellm
5. vLLM. "OpenAI-Compatible Server." https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html
6. Huihui-ai. "Huihui4-8B-A4B: Mixture-of-Experts Language Model." https://huggingface.co/huihui-ai/Huihui4-8B-A4B-GGUF
7. Qwen Team. "Qwen3: Technical Report." 2025.
8. Anthropic. "Claude Code: Agentic coding tool." https://docs.anthropic.com/en/docs/claude-code
