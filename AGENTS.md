# AGENTS.md - Agent 开发指南

本文件为 AI Agent 提供项目开发规范和代码约定。

## 项目结构

```
zcode_aiagent/
├── zcode.py          # 主程序 (TUI + LLM Client + Tools)
├── config.json       # 配置文件 (无默认值)
├── README.md         # 用户文档
├── requirements.txt   # 依赖
└── skills/           # 技能目录 (可选)
```

## config.json 规范

**关键原则**: 所有参数必须显式配置，无默认值。

### 配置结构

```json
{
  "api": {
    "base": "https://openrouter.ai/api/v1",
    "key": "sk-or-v1-...",
    "model": "nvidia/nemotron-3-super-120b-a12b:free",
    "max_tokens": 32768,
    "temperature": 0.7,
    "top_p": 0.95,
    "top_k": 40,
    "frequency_penalty": 0.0,
    "presence_penalty": 0.0,
    "repetition_penalty": 1.0,
    "min_p": 0.0,
    "top_a": 0.0,
    "seed": null,
    "logit_bias": null,
    "logprobs": null,
    "top_logprobs": null,
    "response_format": null,
    "structured_outputs": null,
    "route": null,
    "stop": null,
    "parallel_tool_calls": true,
    "verbosity": "medium",
    "referer": "ZCode",
    "title": "ZCode AI Agent",
    "rpm_limit": 40,
    "tpm_limit": 0,
    "timeout": { "connect": 120, "read": 120 }
  },
  "context": {
    "limit": 200000,
    "compress_threshold": 0.75,
    "keep_recent": 3
  },
  "system_prompt": "You are ZCode, an expert AI coding assistant. Be concise, precise, and technically rigorous.",
  "retry": { "max_retries": 3, "delays": [10, 20, 40] },
  "tool": {
    "max_loops": 999,
    "timeout": 300,
    "command_timeout": 30,
    "search_timeout": 15,
    "result_preview_length": 300
  },
  "reasoning": {
    "enabled": true,
    "mode": "effort",
    "effort": "medium",
    "max_tokens": 4000,
    "exclude": false
  },
  "rate_limit": {
    "enabled": true,
    "rpm": 50,
    "tpm": 100000,
    "retries_on_429": 3,
    "backoff_delays": [10, 20, 40]
  },
  "mcp": { "enabled": false, "servers": {} },
  "skills": {
    "enabled": false,
    "dir": "skills",
    "active": []
  }
}
```

## 代码规范

### 1. 配置读取

```python
# ✅ 正确: 从 config 读取，不使用默认值
self.max_retries = retry.get("max_retries")
self.connect_timeout = api_timeout.get("connect")

# ❌ 错误: 使用硬编码默认值
self.max_retries = retry.get("max_retries", 3)
```

### 2. 限流器配置读取

```python
# 优先使用 rate_limit 块，兼容旧版 api.rpm_limit/tpm_limit
rate_limit_cfg = self.config.get("rate_limit", {})
rl_enabled = rate_limit_cfg.get("enabled", True)
if rl_enabled:
    rpm_limit = rate_limit_cfg.get("rpm", 0)
    tpm_limit = rate_limit_cfg.get("tpm", 0)
else:
    rpm_limit = 0
    tpm_limit = 0

# 兼容旧版
if rpm_limit <= 0 and tpm_limit <= 0:
    rpm_limit = api.get("rpm_limit", 0)
    tpm_limit = api.get("tpm_limit", 0)

self.limiter = AsyncRateLimiter(rpm_limit, tpm_limit)
```

### 3. API 请求构建

```python
# OpenRouter 统一参数列表
openrouter_params = [
    "temperature", "max_tokens", "top_p", "top_k", "min_p", "top_a",
    "frequency_penalty", "presence_penalty", "repetition_penalty",
    "seed", "logit_bias", "logprobs", "top_logprobs",
    "response_format", "structured_outputs", "route", "stop",
    "parallel_tool_calls", "verbosity"
]
for param in openrouter_params:
    value = api.get(param)
    if value is not None:
        payload[param] = value
```

### 4. Reasoning 参数 (三家 API 差异)

```python
reasoning = self.config.get("reasoning", {})

if "anthropic" in base:
    # Anthropic: reasoning 对象 + exclude + 强制 temperature=1.0
    if reasoning.get("enabled"):
        payload["reasoning"] = {"enabled": True, "effort": reasoning.get("effort", "medium")}
        if reasoning.get("exclude"):
            payload["reasoning"]["exclude"] = True
        payload["temperature"] = 1.0
elif "openrouter" in base:
    # OpenRouter: reasoning 对象 (转发给下游)
    if reasoning.get("enabled"):
        payload["reasoning"] = {"enabled": True, "effort": reasoning.get("effort", "medium")}
elif "openai" in base:
    # OpenAI: reasoning_effort 字符串
    if reasoning.get("enabled"):
        effort = reasoning.get("effort", "medium")
        if effort in ("low", "medium", "high", "none"):
            payload["reasoning_effort"] = effort
```

### 5. 推理模型检测 (OpenAI o1/o3/o4)

```python
def _is_reasoning_model(self, model: str) -> bool:
    model_lower = model.lower()
    return model_lower.startswith(("o1-", "o3-", "o4-", "o3-mini", "o4-mini"))

def _filter_reasoning_params(self, payload: dict) -> dict:
    # 推理模型不支持这些参数
    reasoning_remove = {"temperature", "top_p", "top_k", "presence_penalty", "frequency_penalty"}
    return {k: v for k, v in payload.items() if k not in reasoning_remove}
```

### 6. HTTP Headers

```python
def get_headers(self):
    api = self.config.get("api", {})
    headers = {"Content-Type": "application/json"}
    
    if "anthropic" in self.api_base:
        headers["x-api-key"] = self.api_key
        headers["anthropic-version"] = "2023-06-01"
        # 模型支持 thinking 时添加 beta 头
        reasoning_cfg = self.config.get("reasoning", {})
        if reasoning_cfg.get("enabled") and self._model_supports_thinking(model):
            headers["anthropic-beta"] = "interleaved-thinking-2025-05-14"
    else:
        headers["Authorization"] = f"Bearer {self.api_key}"
        referer = api.get("referer")
        title = api.get("title")
        if referer:
            headers["HTTP-Referer"] = referer
        if title:
            headers["X-OpenRouter-Title"] = title
    return headers
```

### 7. 工具格式

```python
# Tools 必须包含 type: function 外层
if tools:
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": t["function"]["name"],
                "description": t["function"]["description"],
                "parameters": t["function"]["parameters"],
            }
        }
        for t in tools
    ]
```

### 8. 响应解析

防御性默认值仅用于处理 API 响应（不是配置）:

```python
# ✅ 合理: API 响应字段的默认值
usage.get("input_tokens", 0)
ev.get("index", 0)

# ❌ 错误: 配置字段使用默认值
ctx_limit = ctx_cfg.get("limit", 200000)
```

## 依赖

- textual (TUI)
- httpx (HTTP Client)
- tiktoken (Token 计数)

## 注意事项

1. **API 类型判断**: 通过 `base` URL 包含关键字判断 (`anthropic`, `openai`, `openrouter`)
2. **限流优先级**: rate_limit.rpm/tpm > api.rpm_limit/tpm_limit (兼容旧版)
3. **429 处理**: 使用 rate_limit.backoff_delays，触发时清空桶并强制冷静 60 秒
4. **推理模型**: o1/o3/o4 系列自动过滤 temperature/top_p 并转换 max_tokens → max_completion_tokens
5. **tools 格式**: 必须包含 `{"type": "function", "function": {...}}` 结构
6. **内置工具**: read_file, write_file, list_dir, run_command, search_files
7. **MCP 集成**: 通过 mcp.servers 配置，支持动态加载外部工具
8. **Skills 集成**: 通过 skills.dir 和 skills.active 配置技能目录和激活列表
9. **系统提示**: 可通过 config.json 中的 system_prompt 自定义 agent 角色，当前默认为 "You are ZCode, an expert AI coding assistant. Be concise, precise, and technically rigorous."