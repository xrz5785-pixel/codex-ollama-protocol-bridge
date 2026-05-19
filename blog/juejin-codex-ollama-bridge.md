# 让本地模型用上 Codex 工具调用：我把两个不兼容的 API 翻译了

## TL;DR

Codex CLI 支持本地 Ollama 模型了，但一用就报 `unsupported call`。我写了一个 800 行的 Python 代理，把 `/v1/responses` 翻译成 `/v1/chat/completions`，让 qwen3:14b、huihui4-8b 都能在 Codex 里执行 shell 命令了。代码开源，论文已发。

---

## 起因

Codex v0.130.0 新增了 `--oss --local-provider ollama` 参数。我兴奋地试了一把：

```bash
codex exec --oss --local-provider ollama -m qwen3:14b "列出文件"
```

然后：

```
unsupported call: call_abc123 (exec_command)
```

换 qwen2.5-coder:3b、llama3.1:8b、huihui4-8b，全部一样。Ollama 返回 200 OK，但工具调用始终失败。

## 诊断过程

### 第一步：定位分歧点

同一个模型，用 `/v1/chat/completions` 手动发请求——工具调用正常：

```bash
curl -X POST http://localhost:11434/v1/chat/completions \
  -d '{"model":"huihui4-8b-a4b","messages":[...],"tools":[...]}'
# → {choices: [{message: {tool_calls: [{function: {name: "exec_command",...}}]}}]}
```

用 `/v1/responses` 同样请求——返回的都是文本，没有 `function_call` 结构。

**结论：问题不在模型，在 API 端点。**

### 第二步：根因确认

读了 Ollama 源码，`/v1/responses` 就是个薄包装——把 Responses API 格式转成 `/api/chat` 请求，但转换过程中**把工具调用语义弄丢了**。模型产出了 `tool_calls`，但 Ollama 的 Responses 包装层不知道怎么把它塞进 `output` 数组里。

### 第三步：方案选择

三个选项：
1. **修 Ollama**：提 PR → 等合并 → 等发版。太慢。
2. **修 Codex**：改客户端的 API 调用。但 Codex 源码是 TypeScript 的黑盒。
3. **中间插一层**：写个代理，翻译协议。零侵入，立即可用。

选了方案三。

## 怎么做

### 架构

```
Codex → /v1/responses → [代理 :11434] → /v1/chat/completions → Ollama :11433
       ← SSE events   ←               ← JSON response           ←
```

三件事：

### 1. 请求格式翻译

Codex 发过来的是 Responses API 格式：

```json
{"model":"qwen3:14b","input":"ls /tmp","instructions":"...","tools":[...],"stream":true}
```

代理把它转成 Chat Completions 格式：

```json
{"model":"qwen3:14b","messages":[{...}],"tools":[...],"tool_choice":"auto","stream":false}
```

`stream` 改成 `false`——不从 Ollama 要流式，我们自己合成 SSE 事件流，这样做更可靠。

### 2. 工具定义瘦身

Codex 的 `exec_command` 工具有 **10 个参数**。全部 11 个工具加起来大约 **4100 tokens**——这对 8B 模型来说太多了。

我把每个工具缩减到核心参数：

| 工具 | 之前 | 之后 |
|------|------|------|
| exec_command | 10 个参数 | 2 个（cmd, workdir） |
| write_stdin | 6 个 | 2 个（session_id, chars） |
| spawn_agent | 8 个 | 3 个（agent_type, items, message） |
| ... | ... | ... |

Codex 会自动给缺失的参数填默认值。瘦身后共约 **800 tokens**——模型选对工具的概率大幅提升。

### 3. SSE 事件合成

从 Ollama 拿回非流式 JSON 响应后，代理按 Responses API 规范合成 SSE 事件流：

```
chat.completion JSON
  → event: response.created
  → event: response.in_progress
  → event: response.output_item.added  (function_call, output_index=0)
  → event: response.function_call_arguments.delta
  → event: response.function_call_arguments.done
  → event: response.output_item.done
  → event: response.completed  (usage 字段已标准化)
```

踩了几个坑：

- **`output_index` 不能硬编码为 0**——当模型同时返回 function_call 和文本时，function_call 的 index 是 0，文本是 1。写死会导致后面的覆盖前面的。
- **`usage` 字段名不一致**——Ollama 返回 `prompt_tokens` / `completion_tokens`，Codex 期望 `input_tokens` / `output_tokens`。
- **自定义模型的 pull 拦截**——Codex 在使用模型前会调用 `POST /api/pull`，但 GGUF 自定义模型不在 Ollama 官方 registry 里。代理拦截这个请求，查询本地模型列表，已有就返回成功。

## 效果

```bash
$ codex exec --oss --local-provider ollama -m huihui4-8b-a4b "列出项目文件"

exec
/bin/zsh -lc 'ls -R /Users/x/ai-assets/codex-proxy/'
  succeeded in 0ms:
proxy.py
paper/
README.md
LICENSE
```

可用模型：

| 模型 | 大小 | 工具调用 | 推荐 |
|------|------|----------|------|
| qwen3:14b | 9.3GB | ✅ 稳定 | 旗舰 |
| huihui4:8b-a4b | 5.4GB | ✅ 良好 | 轻快备选 |
| qwen2.5-coder:3b | 1.9GB | ⚠️ 弱 | 纯文本任务 |

## 关键认知

**"新 API 更好"是一个陷阱。** `/v1/responses` 比 `/v1/chat/completions` 更新，但在 Ollama 中，旧端点的原生工具调用支持更成熟。当你遇到 API 兼容问题时，试试降级到旧端点——答案可能比你想象的简单。

**注意力预算是本地模型的第一级约束。** 工具定义从 4100 tokens 瘦身到 800 tokens（5× 压缩），对模型准确率的提升比任何 prompt 工程技巧都大。小模型的工具调用失败不全是能力问题——很多时候是被工具定义"淹"了。

## 项目地址

- GitHub: [xrz5785-pixel/codex-ollama-protocol-bridge](https://github.com/xrz5785-pixel/codex-ollama-protocol-bridge)
- HuggingFace: [Sheikylife/codex-ollama-protocol-bridge](https://huggingface.co/Sheikylife/codex-ollama-protocol-bridge)
- 完整论文：技术报告（中英文）+ LaTeX 预印本

---

*2026年5月。作者 xuanyuan，独立研究员。代码 MIT 协议。*
