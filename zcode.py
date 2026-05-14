#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx",
#     "tiktoken",
#     "textual",
# ]
# ///
"""
ZCode TUI - A Textual-based AI coding assistant
Backend: OpenAI-compatible LLM endpoint
Update 2026-05-03:
- 多后备 API endpoint 支持
- 手动端点切换按钮（工具栏 EP 按钮）
- 轮次递进压缩（compression.rounds 配置）
- 移除对话区端点切换 info 消息
- 技能纯语义自动加载，从 config.json skills.dir 路径读取
- 取消 /skill 命令，技能完全自动匹配
"""
import asyncio
import copy
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import warnings
from datetime import datetime
from pathlib import Path
warnings.filterwarnings("ignore", category=ResourceWarning)
import httpx
import tiktoken
_TIKTOKEN_ENC = None
def _get_tiktoken_enc():
    global _TIKTOKEN_ENC
    if _TIKTOKEN_ENC is None:
        _TIKTOKEN_ENC = tiktoken.get_encoding("cl100k_base")
    return _TIKTOKEN_ENC
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Header,
    Markdown,
    Static,
    TextArea,
)
# ─────────────────────────── Config ───────────────────────────
CONFIG_TEMPLATE_PATH = Path(__file__).parent / "config.json"

def _infer_ep_type(base: str) -> str:
    """Guess endpoint type from base URL when 'type' field is absent."""
    b = base.lower()
    if "anthropic" in b:              return "anthropic"
    if "openrouter" in b:             return "openrouter"
    if "openai.com" in b:             return "openai"
    if "deepseek" in b:               return "deepseek"
    if "localhost" in b or "127.0.0.1" in b or "0.0.0.0" in b: return "llama-server"
    return "generic"

def _normalize_config(config: dict) -> dict:
    """
    Normalise config to new format:
      top-level 'endpoints' list, each entry has a 'type' field.
    Transparently migrates old-style 'api' section configs.
    """
    # Already new format — deep copy to avoid mutating input
    if "endpoints" in config and isinstance(config.get("endpoints"), list):
        config = copy.deepcopy(config)
        eps = config["endpoints"]
        for ep in eps:
            if "type" not in ep:
                ep["type"] = _infer_ep_type(ep.get("base", ""))
        return config

    # Old format: top-level 'api' dict with optional nested 'endpoints'
    api = config.get("api", {})
    raw_eps = api.get("endpoints", [])
    base_defaults = {k: v for k, v in api.items() if k != "endpoints"}

    if not raw_eps:
        merged_eps = [base_defaults]
    else:
        merged_eps = []
        for ep in raw_eps:
            m = copy.deepcopy(base_defaults); m.update(ep)
            if "timeout" in ep and isinstance(ep["timeout"], dict):
                m["timeout"] = {**copy.deepcopy(base_defaults.get("timeout", {})), **ep["timeout"]}
            merged_eps.append(m)

    # Lift old-style top-level reasoning/context into each endpoint
    old_reasoning = config.get("reasoning", {})
    old_ctx_limit  = config.get("context", {}).get("limit", 0)
    old_keep_recent = config.get("context", {}).get("keep_recent", 3)
    for ep in merged_eps:
        if "type" not in ep:
            ep["type"] = _infer_ep_type(ep.get("base", ""))
        if "reasoning" not in ep and old_reasoning:
            ep["reasoning"] = old_reasoning.copy()
        if "context_limit" not in ep and old_ctx_limit:
            ep["context_limit"] = old_ctx_limit
        if "keep_recent" not in ep and old_keep_recent:
            ep["keep_recent"] = old_keep_recent

    out = {k: v for k, v in config.items() if k not in ("api", "reasoning")}
    out["endpoints"] = merged_eps
    return out

def load_default_config() -> dict:
    """Load and normalise config from template file."""
    if CONFIG_TEMPLATE_PATH.exists():
        with open(CONFIG_TEMPLATE_PATH, encoding="utf-8") as f:
            return _normalize_config(json.load(f))
    raise FileNotFoundError(
        f"Config template not found: {CONFIG_TEMPLATE_PATH}\n"
        "Please ensure config.json exists in the application directory."
    )
_DEFAULT_CONFIG_CACHE = None
def get_default_config() -> dict:
    global _DEFAULT_CONFIG_CACHE
    if _DEFAULT_CONFIG_CACHE is None:
        _DEFAULT_CONFIG_CACHE = load_default_config()
    return _DEFAULT_CONFIG_CACHE
def get_skills_dir(config: dict = None) -> Path:
    """Get skills directory from config.json skills.dir, fallback to ~/.claudecode/skills."""
    cfg = config or get_default_config()
    skills_cfg = cfg.get("skills", {})
    dir_path = skills_cfg.get("dir", "")
    if dir_path:
        return Path(dir_path).expanduser()
    return Path("~/.claudecode/skills").expanduser()
def get_mcp_config(config: dict = None) -> dict:
    """Get MCP configuration (servers, enabled)."""
    cfg = config or get_default_config()
    return cfg.get("mcp", {"enabled": False, "servers": {}})
def load_config() -> dict:
    """Load and normalise config (single source of truth)."""
    skills_dir = get_skills_dir()
    skills_dir.mkdir(parents=True, exist_ok=True)
    return copy.deepcopy(get_default_config())
# ─────────────────────────── Tokenizer ───────────────────────────
def _fallback_count_tokens(text: str) -> int:
    """
    Token 估算：中文/全角符号 计 2.0，英文/代码符号 计 0.75
    使用 unicodedata.east_asian_width 判断字符宽度。
    """
    if not text:
        return 0
    total = 0.0
    for ch in text:
        w = unicodedata.east_asian_width(ch)
        total += 2.0 if w in ("W", "F") else 0.75
    return int(total)

def count_tokens(text: str, multiplier: float = 1.0) -> int:
    """优先使用 tiktoken，失败时使用 fallback 估算"""
    if not text:
        return 0
    try:
        base = len(_get_tiktoken_enc().encode(text))
    except Exception:
        base = _fallback_count_tokens(text)
    return max(1, int(base * multiplier))

def messages_token_count(messages: list, multiplier: float = 1.0) -> int:
    """
    计算消息列表的总 token 数，正确处理：
      - content 文本块
      - tool_calls (OpenAI 格式)
      - tool_use / tool_result (Anthropic 格式)
    每则消息额外 4 tokens 结构开销。
    """
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    total += count_tokens(block.get("text", ""), multiplier)
        elif isinstance(content, str):
            total += count_tokens(content, multiplier)
        tool_calls = m.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                fn = tc.get("function", {})
                total += count_tokens(fn.get("name", ""), multiplier)
                total += count_tokens(fn.get("arguments", ""), multiplier)
        if m.get("role") == "assistant" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    total += count_tokens(block.get("name", ""), multiplier)
                    total += count_tokens(json.dumps(block.get("input", {})), multiplier)
        if m.get("role") == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    total += count_tokens(block.get("content", ""), multiplier)
        if m.get("role") == "tool":
            total += count_tokens(m.get("name", ""), multiplier)
            total += count_tokens(m.get("tool_call_id", ""), multiplier)
        total += 4
    return total
# ─────────────────────────── Async Rate Limiter ───────────────────────────
class AsyncRateLimiter:
    """
    Per-endpoint token-bucket rate limiter supporting:
      rpm_limit   – max requests per minute   (0 = disabled)
      tpm_limit   – max tokens per minute     (0 = disabled)
      qps_limit   – max queries per second    (0 = disabled)
      token_budget – hard lifetime token cap  (0 = disabled)
    """
    def __init__(self, rpm_limit: int = 0, tpm_limit: int = 0,
                 qps_limit: float = 0, token_budget: int = 0):
        self.rpm_limit = rpm_limit
        self.tpm_limit = tpm_limit
        self.qps_limit = qps_limit
        self.token_budget = token_budget
        self._rpm_bucket = float(rpm_limit) if rpm_limit > 0 else 0.0
        self._tpm_bucket = float(tpm_limit) if tpm_limit > 0 else 0.0
        self._qps_bucket = 1.0 if qps_limit > 0 else 0.0
        self._total_tokens_used: int = 0
        self._last_time = time.monotonic()
        self._lock = asyncio.Lock()
        self._cooldown_until: float = 0.0

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_time
        if elapsed <= 0:
            return
        if self.rpm_limit > 0:
            self._rpm_bucket = min(self._rpm_bucket + elapsed * (self.rpm_limit / 60.0), float(self.rpm_limit))
        if self.tpm_limit > 0:
            self._tpm_bucket = min(self._tpm_bucket + elapsed * (self.tpm_limit / 60.0), float(self.tpm_limit))
        if self.qps_limit > 0:
            self._qps_bucket = min(self._qps_bucket + elapsed * self.qps_limit, 1.0)
        self._last_time = now

    def check_budget(self) -> str | None:
        """Return error message if token budget is exhausted, else None."""
        if self.token_budget > 0 and self._total_tokens_used >= self.token_budget:
            return (f"Token budget exhausted: {self._total_tokens_used:,} / "
                    f"{self.token_budget:,} tokens used on this endpoint")
        return None

    async def acquire(self, estimated_tokens: int = 0) -> float:
        """Wait until rate limits allow a request. Returns seconds waited."""
        active = self.rpm_limit > 0 or self.tpm_limit > 0 or self.qps_limit > 0
        if not active:
            return 0.0
        async with self._lock:
            # Honour any forced cooldown (e.g. after 429)
            now = time.monotonic()
            if now < self._cooldown_until:
                wait = self._cooldown_until - now
                await asyncio.sleep(wait)
            self._refill()
            wait_time = 0.0
            if self.rpm_limit > 0 and self._rpm_bucket < 1.0:
                wait_time = max(wait_time, (1.0 - self._rpm_bucket) / self.rpm_limit * 60.0)
            if self.tpm_limit > 0 and estimated_tokens > 0 and self._tpm_bucket < estimated_tokens:
                wait_time = max(wait_time, (estimated_tokens - self._tpm_bucket) / self.tpm_limit * 60.0)
            if self.qps_limit > 0 and self._qps_bucket < 1.0:
                wait_time = max(wait_time, (1.0 - self._qps_bucket) / self.qps_limit)
            if wait_time > 0:
                await asyncio.sleep(wait_time)
                self._refill()
            # Consume buckets
            if self.rpm_limit > 0:
                self._rpm_bucket -= 1.0
            if self.qps_limit > 0:
                self._qps_bucket -= 1.0
            # Pre-consume estimated input tokens from TPM bucket
            if self.tpm_limit > 0 and estimated_tokens > 0:
                self._tpm_bucket -= estimated_tokens
            return wait_time

    def consume_tokens(self, tokens: int, input_tokens: int = 0):
        """Called after response completes with actual output (and optionally input) token count."""
        if tokens <= 0 and input_tokens <= 0:
            return
        # Output tokens reduce the TPM bucket (input was already deducted in acquire)
        if self.tpm_limit > 0:
            self._tpm_bucket -= tokens
        self._total_tokens_used += tokens + input_tokens

    def force_429_cooldown(self, seconds: int = 60):
        self._rpm_bucket = 0.0
        self._tpm_bucket = 0.0
        self._qps_bucket = 0.0
        self._cooldown_until = time.monotonic() + seconds

    def get_status(self) -> dict:
        self._refill()
        budget_remaining = (max(0, self.token_budget - self._total_tokens_used)
                            if self.token_budget > 0 else None)
        return {
            "rpm_limit": self.rpm_limit,  "rpm_bucket": self._rpm_bucket,
            "tpm_limit": self.tpm_limit,  "tpm_bucket": self._tpm_bucket,
            "qps_limit": self.qps_limit,  "qps_bucket": self._qps_bucket,
            "token_budget": self.token_budget,
            "total_tokens_used": self._total_tokens_used,
            "budget_remaining": budget_remaining,
        }
# ─────────────────────────── Vision ───────────────────────────
MIME_MAP = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}
# ─────────────────────────── Skills (纯语义自动加载) ───────────────────────────
def load_skills(config: dict = None) -> dict[str, str]:
    """Load all .md files from the skills directory specified in config.json.
    Skill name = parent dir name if file is in a subfolder, otherwise the file stem."""
    skills = {}
    skills_dir = get_skills_dir(config)
    if not skills_dir.exists():
        return skills
    for p in skills_dir.glob("**/*.md"):
        # Use parent folder name as skill name (e.g., playwright/SKILL.md → playwright)
        # If file is directly in skills_dir, use file stem
        if p.parent != skills_dir:
            name = p.parent.name
        else:
            name = p.stem
        try:
            skills[name] = p.read_text(encoding="utf-8")
        except Exception:
            pass
    return skills
def build_system_prompt(config: dict, skills: dict[str, str]) -> str:
    """Build system prompt with available skills listed (not auto-injected).
    The LLM uses the `load_skill` tool to load full skill instructions on demand."""
    parts = [config.get("system_prompt", "You are ZCode, an expert AI coding assistant. Be concise, precise, and technically rigorous.")]
    if skills:
        parts.append("\n\n## Available Skills")
        parts.append("The following skills provide specialized instructions. Use the `load_skill` tool to load a skill when the task matches its domain.\n")
        for name in sorted(skills.keys()):
            content = skills[name]
            desc = ""
            for line in content.split("\n"):
                line = line.strip()
                if line.startswith("# "):
                    desc = line[2:].strip()
                    break
            if not desc:
                for line in content.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("---"):
                        desc = line[:120]
                        break
            parts.append(f"- **{name}**: {desc or name}")
    return "\n".join(parts)
# ─────────────────────────── MCP ───────────────────────────
class MCPClient:
    _CONNECT_TIMEOUT = 10
    def __init__(self):
        self.servers: dict[str, dict] = {}
        self.tools: list[dict] = []
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._next_id: int = 1
    async def _readline(self, proc: asyncio.subprocess.Process, timeout: float) -> bytes:
        return await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
    async def connect(self, name: str, cmd: list[str], env: dict | None = None):
        try:
            e = os.environ.copy()
            if env:
                e.update(env)
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=e,
            )
            self._procs[name] = proc
            # 启动后台任务消费 stderr，防止 pipe 阻塞
            async def _drain_stderr():
                try:
                    while True:
                        line = await proc.stderr.readline()
                        if not line:
                            break
                except Exception:
                    pass
            asyncio.get_event_loop().create_task(_drain_stderr())
            for _ in range(30):
                if proc.returncode is not None:
                    raise Exception(f"MCP process exited with {proc.returncode}")
                if proc.stdin and not proc.stdin.is_closing():
                    break
                await asyncio.sleep(0.1)
            else:
                raise Exception("MCP process failed to start within 3 seconds")
            init_req = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "claudecode", "version": "1.0"}}
            }) + "\n"
            proc.stdin.write(init_req.encode()); await proc.stdin.drain()
            raw = await self._readline(proc, self._CONNECT_TIMEOUT)
            if not raw:
                return False
            while raw and not raw.strip().startswith(b"{"):
                raw = await self._readline(proc, self._CONNECT_TIMEOUT)
                if not raw:
                    return False
            resp = json.loads(raw)
            if "result" in resp:
                notif = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
                proc.stdin.write(notif.encode()); await proc.stdin.drain()
            list_req = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n"
            proc.stdin.write(list_req.encode()); await proc.stdin.drain()
            raw = await self._readline(proc, self._CONNECT_TIMEOUT)
            while raw and not raw.strip().startswith(b"{"):
                raw = await self._readline(proc, self._CONNECT_TIMEOUT)
                if not raw:
                    return False
            resp = json.loads(raw)
            tools = resp.get("result", {}).get("tools", [])
            for t in tools:
                self.tools.append({
                    "type": "function",
                    "function": {
                        "name": f"{name}__{t['name']}",
                        "description": t.get("description", ""),
                        "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
                    },
                })
            self.servers[name] = {"cmd": cmd, "tools": tools}
            return True
        except asyncio.TimeoutError:
            return False
        except Exception:
            return False
    async def call_tool(self, full_name: str, arguments: dict, timeout: float = 30) -> str:
        server_name, tool_name = full_name.split("__", 1)
        proc = self._procs.get(server_name)
        if not proc:
            return f"Error: MCP server '{server_name}' not running"
        max_retries = 3
        retry_delays = [1, 2, 3]
        for attempt in range(max_retries):
            try:
                req_id = self._next_id; self._next_id += 1
                req = json.dumps({
                    "jsonrpc": "2.0", "id": req_id, "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments}
                }) + "\n"
                proc.stdin.write(req.encode()); await proc.stdin.drain()
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                if not raw:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delays[attempt]); continue
                    return "No response from MCP server"
                while raw and not raw.strip().startswith(b"{"):
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                    if not raw:
                        break
                if not raw:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delays[attempt]); continue
                    return "No response from MCP server"
                resp = json.loads(raw)
                if "error" in resp:
                    return f"MCP error: {resp['error']}"
                content = resp.get("result", {}).get("content", [])
                result_text = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
                if not result_text:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(retry_delays[attempt]); continue
                    return f"Tool '{tool_name}' returned empty result"
                return result_text
            except asyncio.TimeoutError:
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delays[attempt]); continue
                return f"Tool '{tool_name}' timed out after {timeout}s"
            except Exception as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delays[attempt]); continue
                return f"MCP error: {e}"
        return f"Tool '{tool_name}' failed after {max_retries} attempts"
    def close_all(self):
        for name, proc in self._procs.items():
            try:
                proc.terminate()
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        self._procs.clear()
        self.tools.clear()
        self.servers.clear()
# ─────────────────────────── Built-in Tools ───────────────────────────
BUILTIN_TOOLS = [
    {"type": "function", "function": {"name": "read_file", "description": "Read the contents of a file from the filesystem.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Absolute or relative file path"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file", "description": "Write content to a file (creates or overwrites).", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "list_dir", "description": "List files and directories at a given path.", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "Directory path (default: current dir)"}}, "required": []}}},
    {"type": "function", "function": {"name": "run_command", "description": "Run a shell command and return stdout/stderr.", "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "Shell command to execute"}, "cwd": {"type": "string", "description": "Working directory (optional)"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "search_files", "description": "Search for text pattern in files using grep.", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string", "description": "Directory to search in"}, "file_pattern": {"type": "string", "description": "File glob pattern e.g. '*.py'"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "load_skill", "description": "Load a skill's full instructions from SKILL.md into the conversation. Skills are specialized instruction sets for specific tasks (browser automation, web search, etc.). Call this when the user's task matches a skill's domain, then follow the skill instructions precisely.", "parameters": {"type": "object", "properties": {"skill_name": {"type": "string", "description": "Name of the skill to load (e.g., 'playwright', 'taobao_search_headed')"}}, "required": ["skill_name"]}}},
]
_ANSI_ESCAPE_RE = re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
_OSC_ESCAPE_RE = re.compile(r'\x1b\][^\x07]*\x07')
_CONTROL_CHARS_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
def _clean_output(text: str) -> str:
    """剥离 ANSI 转义序列(含OSC)、控制字符，防止 Textual 渲染被污染"""
    if not text:
        return text
    text = _ANSI_ESCAPE_RE.sub('', text)
    text = _OSC_ESCAPE_RE.sub('', text)
    text = _CONTROL_CHARS_RE.sub('', text)
    text = re.sub(r'\r\n', '\n', text)
    return text.strip()

async def execute_builtin_tool(name: str, arguments: dict, timeout: int = 30, command_timeout: int = 30, search_timeout: int = 15) -> str:
    try:
        if name == "read_file":
            p = Path(arguments["path"]).expanduser()
            loop = asyncio.get_event_loop()
            content = await loop.run_in_executor(None, lambda: p.read_text(encoding="utf-8", errors="replace"))
            return _clean_output(content)
        elif name == "write_file":
            p = Path(arguments["path"]).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: p.write_text(arguments["content"], encoding="utf-8"))
            return f"Written {len(arguments['content'])} chars to {p}"
        elif name == "list_dir":
            p = Path(arguments.get("path", ".")).expanduser()
            loop = asyncio.get_event_loop()
            entries = await loop.run_in_executor(None, lambda: sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name)))
            lines = [f"{'📄' if e.is_file() else '📁'} {e.name}" for e in entries]
            return _clean_output("\n".join(lines)) if lines else "(empty)"
        elif name == "run_command":
            cmd_timeout = arguments.get("timeout", command_timeout)
            cwd = arguments.get("cwd")
            _env = os.environ.copy()
            _env.update({"TERM": "dumb", "COLUMNS": "80", "LINES": "24",
                         "CLICOLOR": "0", "NO_COLOR": "1", "PYTHONUNBUFFERED": "1"})
            proc = await asyncio.create_subprocess_shell(
                arguments["command"],
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=cwd,
                env=_env,
            )
            try:
                stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=cmd_timeout)
                output = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
                output = _clean_output(output)
                max_chars = 8000
                if len(output) > max_chars:
                    output = output[:max_chars] + "\n\n... [Output truncated for stability]"
                return f"STDOUT/STDERR:\n{output}\nEXIT_CODE: {proc.returncode}"
            except asyncio.TimeoutError:
                try:
                    proc.terminate()
                    await proc.wait()
                    return f"Error: Command timed out after {cmd_timeout}s."
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return f"Error: Command timed out after {cmd_timeout}s and was killed."
        elif name == "load_skill":
            skill_name = arguments.get("skill_name", "")
            if not skill_name:
                return "Error: 'skill_name' is required"
            skills_dir = get_skills_dir()
            paths_to_try = [
                skills_dir / skill_name / "SKILL.md",
                skills_dir / f"{skill_name}.md",
            ]
            for p in paths_to_try:
                if p.exists():
                    content = p.read_text(encoding="utf-8")
                    return content
            available = []
            if skills_dir.exists():
                for d in skills_dir.iterdir():
                    if d.is_dir() and (d / "SKILL.md").exists():
                        available.append(d.name)
                for f in skills_dir.glob("*.md"):
                    available.append(f.stem)
            return f"Skill '{skill_name}' not found. Available skills: {', '.join(sorted(set(available))) if available else '(none)'}"
        elif name == "search_files":
            s_timeout = arguments.get("timeout", search_timeout)
            pattern = arguments.get("pattern", "")
            file_pat = arguments.get("file_pattern", "*")
            search_path = Path(arguments.get("path", ".")).expanduser()
            if not pattern:
                return "Error: 'pattern' is required"
            re_flags = re.IGNORECASE if sys.platform == "win32" else 0
            try:
                regex = re.compile(pattern, re_flags)
            except re.error as e:
                return f"Error: invalid regex pattern: {e}"
            results = []
            loop = asyncio.get_event_loop()
            def _search():
                matches = []
                for p in search_path.rglob(file_pat):
                    if not p.is_file():
                        continue
                    try:
                        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                            if regex.search(line):
                                matches.append(f"{p}:{i}: {line.rstrip()[:500]}")
                    except Exception:
                        pass
                return matches
            try:
                matches = await asyncio.wait_for(
                    loop.run_in_executor(None, _search), timeout=s_timeout)
            except asyncio.TimeoutError:
                return f"search_files timed out after {s_timeout}s"
            if not matches:
                return "(no matches)"
            output = "\n".join(matches[:200])
            if len(matches) > 200:
                output += f"\n\n... ({len(matches) - 200} more matches truncated)"
            return _clean_output(output)
    except Exception as e:
        return f"Tool error: {e}"
    return f"Unknown tool: {name}"
# ─────────────────────────── XML Tool-Call Parser ───────────────────────────
_XML_TOOL_CALL_RE = re.compile(r"<tool_call>([\s\S]*?)</tool_call>", re.MULTILINE)
_XML_FUNC_RE = re.compile(r"<function=([^>]+)>\s*(?:<parameter>\s*)?(.*?)(?:\s*</parameter>)?\s*</function>", re.DOTALL)
def _parse_xml_tool_calls(text: str) -> tuple[list[dict], str]:
    calls = []
    _id_counter = [0]
    def _make_id() -> str:
        _id_counter[0] += 1
        return f"xml_tc_{_id_counter[0]}"
    def _try_parse_args(raw: str) -> dict:
        raw = raw.strip()
        if not raw:
            return {}
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                for key in ("parameters", "arguments", "params"):
                    if key in obj and isinstance(obj[key], dict):
                        return obj[key]
            return obj if isinstance(obj, dict) else {}
        except json.JSONDecodeError:
            pass
        start = raw.find("{"); end = raw.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                pass
        return {}
    cleaned = text
    for m in _XML_TOOL_CALL_RE.finditer(text):
        body = m.group(1).strip()
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            start = body.find("{"); end = body.rfind("}")
            try:
                obj = json.loads(body[start:end + 1]) if start != -1 else {}
            except Exception:
                obj = {}
        name = obj.get("name") or obj.get("function") or ""
        args = obj.get("parameters") or obj.get("arguments") or obj.get("params") or {}
        if not isinstance(args, dict):
            args = {}
        if name:
            calls.append({"id": _make_id(), "name": name, "arguments": args})
        cleaned = cleaned.replace(m.group(0), "", 1)
    if not calls:
        for m in _XML_FUNC_RE.finditer(text):
            name = m.group(1).strip()
            body = m.group(2).strip()
            args = _try_parse_args(body)
            if name:
                calls.append({"id": _make_id(), "name": name, "arguments": args})
            cleaned = cleaned.replace(m.group(0), "", 1)
    return calls, cleaned.strip()
# ─────────────────────────── LLM Client ───────────────────────────
class LLMClient:
    def __init__(self, config: dict):
        self.config = config
        self.retry = config.get("retry", {})
        self.endpoints: list[dict] = config["endpoints"]
        self.active_endpoint_index = 0
        self.app = None
        self._init_rate_limiters()

    def count_tokens(self, text: str) -> int:
        """优先使用 tiktoken，失败时回退至 fallback 逻辑"""
        if not text:
            return 0
        try:
            return len(_get_tiktoken_enc().encode(text))
        except Exception:
            return _fallback_count_tokens(text)

    def get_ep_type(self, ep: dict | None = None) -> str:
        ep = ep or self.get_active_endpoint()
        return ep.get("type") or _infer_ep_type(ep.get("base", ""))
    def _init_rate_limiters(self):
        rl_cfg = self.config.get("rate_limit", {})
        rl_enabled = rl_cfg.get("enabled", True)
        api_cfg = self.config.get("api", {})
        if rl_enabled:
            g_rpm    = rl_cfg.get("rpm", 0)
            g_tpm    = rl_cfg.get("tpm", 0)
            g_qps    = rl_cfg.get("qps", 0.0)
            g_budget = rl_cfg.get("token_budget", 0)
        else:
            g_rpm = g_tpm = 0; g_qps = 0.0; g_budget = 0
        # 兼容旧版 api.rpm_limit / api.tpm_limit
        if g_rpm <= 0 and g_tpm <= 0:
            g_rpm = int(api_cfg.get("rpm_limit", 0))
            g_tpm = int(api_cfg.get("tpm_limit", 0))
        self._limiters: dict[int, AsyncRateLimiter] = {}
        for i, ep in enumerate(self.endpoints):
            rpm    = int(ep.get("rpm_limit",    g_rpm))
            tpm    = int(ep.get("tpm_limit",    g_tpm))
            qps    = float(ep.get("qps_limit",  g_qps))
            budget = int(ep.get("token_budget", g_budget))
            self._limiters[i] = AsyncRateLimiter(rpm, tpm, qps, budget)
    @property
    def limiter(self) -> AsyncRateLimiter:
        return self._limiters[self.active_endpoint_index]
    def get_active_endpoint(self) -> dict:
        return self.endpoints[self.active_endpoint_index]
    def switch_to_next_endpoint(self) -> str:
        """切换端点并同步更新 UI 按钮文字"""
        self.active_endpoint_index = (self.active_endpoint_index + 1) % len(self.endpoints)
        new_ep_name = self.get_active_endpoint().get("name", f"ep{self.active_endpoint_index + 1}")
        if hasattr(self, "app") and self.app:
            try:
                btn = self.app.query_one("#btn-ep", Button)
                total = len(self.endpoints)
                btn.label = f"EP: {new_ep_name} ({self.active_endpoint_index + 1}/{total})"
            except Exception:
                pass
        return new_ep_name
    ANTHROPIC_THINKING_MODELS = frozenset([
        "claude-sonnet-4-20250514", "claude-sonnet-4-20250501",
        "claude-3-5-sonnet-20241022", "claude-3-5-sonnet-20240620",
        "claude-3-5-sonnet", "claude-3-7-sonnet-20250219", "claude-3-7-sonnet",
    ])
    def get_headers(self, ep: dict | None = None) -> dict:
        ep = ep or self.get_active_endpoint()
        ep_type = self.get_ep_type(ep)
        headers = {"Content-Type": "application/json"}
        key = ep.get("key", "")
        if ep_type == "anthropic":
            if key: headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
            reasoning = ep.get("reasoning", {})
            if reasoning.get("enabled") and self._model_supports_thinking(ep.get("model", "")):
                headers["anthropic-beta"] = "interleaved-thinking-2025-05-14"
        else:
            if key: headers["Authorization"] = f"Bearer {key}"
            if ep_type == "openrouter":
                if ep.get("referer"): headers["HTTP-Referer"] = ep["referer"]
                if ep.get("title"):   headers["X-OpenRouter-Title"] = ep["title"]
        return headers

    def _model_supports_thinking(self, model: str) -> bool:
        if not model: return False
        m = model.lower()
        if "claude" not in m:
            return False
        return any(s in m for s in self.ANTHROPIC_THINKING_MODELS) or "sonnet" in m or "3.7" in m

    def _is_reasoning_model(self, model: str) -> bool:
        if not model: return False
        ml = model.lower()
        if ml in ("o1", "o3", "o4"):
            return True
        return ml.startswith(("o1-", "o3-", "o4-", "o1-mini", "o3-mini", "o4-mini"))

    def build_payload(self, messages: list, tools: list, stream: bool = True) -> dict:
        ep       = self.get_active_endpoint()
        ep_type  = self.get_ep_type(ep)
        model    = ep.get("model", "")
        reasoning = ep.get("reasoning", {})
        is_rm    = self._is_reasoning_model(model)
        payload  = {"model": model, "messages": messages, "stream": stream}
        if ep_type == "anthropic":
            payload["system"]   = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
            payload["messages"] = [m for m in messages if m["role"] != "system"]
            payload["max_tokens"] = ep.get("max_tokens", 8192)
            if reasoning.get("enabled"):
                r = {"enabled": True}
                mode = reasoning.get("mode", "effort")
                if mode == "effort":
                    r["effort"] = reasoning.get("effort", "medium")
                elif mode == "max_tokens":
                    r["max_tokens"] = reasoning.get("budget", reasoning.get("max_tokens", 4000))
                if reasoning.get("exclude"): r["exclude"] = True
                payload["reasoning"] = r
                payload["temperature"] = 1.0
            else:
                payload["temperature"] = ep.get("temperature", 0.7)
            if tools: payload["tools"] = self._format_tools(tools)
        elif ep_type == "openrouter":
            for p in ["temperature", "max_tokens", "top_p", "top_k", "min_p", "top_a",
                      "frequency_penalty", "presence_penalty", "repetition_penalty",
                      "seed", "logit_bias", "logprobs", "top_logprobs",
                      "response_format", "structured_outputs", "route", "stop",
                      "parallel_tool_calls", "verbosity"]:
                v = ep.get(p)
                if v is not None: payload[p] = v
            if tools: payload["tools"] = self._format_tools(tools)
            if reasoning.get("enabled"):
                r = {"enabled": True}
                mode = reasoning.get("mode", "effort")
                if mode == "effort":
                    r["effort"] = reasoning.get("effort", "medium")
                elif mode == "max_tokens":
                    r["max_tokens"] = reasoning.get("budget", reasoning.get("max_tokens", 4000))
                if reasoning.get("exclude"): r["exclude"] = True
                payload["reasoning"] = r
            if is_rm:
                for p in ["temperature", "max_tokens", "top_p", "top_k", "min_p", "top_a",
                           "frequency_penalty", "presence_penalty", "repetition_penalty", "seed"]:
                    payload.pop(p, None)
        elif ep_type in ("openai", "deepseek"):
            payload["max_tokens"] = ep.get("max_tokens", 8192)
            if is_rm:
                for k in ("temperature", "top_p", "top_k", "presence_penalty", "frequency_penalty"):
                    payload.pop(k, None)
                payload["max_completion_tokens"] = payload.pop("max_tokens")
            else:
                payload["temperature"] = ep.get("temperature", 0.7)
                for p in ["top_p", "frequency_penalty", "presence_penalty"]:
                    v = ep.get(p)
                    if v is not None and v != 0.0: payload[p] = v
            if tools: payload["tools"] = self._format_tools(tools)
            if reasoning.get("enabled") and not is_rm:
                mode = reasoning.get("mode", "effort")
                if mode == "max_tokens":
                    payload["max_completion_tokens"] = reasoning.get("budget", reasoning.get("max_tokens", 4000))
                effort = reasoning.get("effort", "medium")
                if effort in ("low", "medium", "high", "none"):
                    payload["reasoning_effort"] = effort
        else:
            payload["max_tokens"] = ep.get("max_tokens", 8192)
            payload["temperature"] = ep.get("temperature", 0.7)
            for p in ["top_p", "top_k", "frequency_penalty", "presence_penalty", "repetition_penalty", "min_p"]:
                v = ep.get(p)
                if v is not None and v != 0.0: payload[p] = v
            if tools: payload["tools"] = self._format_tools(tools)
            if reasoning.get("enabled"):
                budget = reasoning.get("budget", reasoning.get("max_tokens", 4000))
                payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
        return payload
    @staticmethod
    def _sanitize_request_for_log(headers: dict, payload: dict) -> tuple[dict, dict]:
        safe_headers = {}
        for k, v in headers.items():
            if k.lower() in ("authorization", "x-api-key"):
                safe_headers[k] = v[:20] + "***" if len(v) > 20 else "***"
            else:
                safe_headers[k] = v
        safe_payload = {}
        for k, v in payload.items():
            if k == "messages":
                safe_msgs = []
                for m in v:
                    mc = m.copy()
                    if "content" in mc:
                        c = mc["content"]
                        if isinstance(c, str) and len(c) > 200:
                            mc["content"] = c[:200] + f"...[{len(c)} chars]"
                        elif isinstance(c, list):
                            mc["content"] = f"[{len(c)} content blocks]"
                    safe_msgs.append(mc)
                safe_payload[k] = safe_msgs
            else:
                safe_payload[k] = v
        return safe_headers, safe_payload

    def _format_tools(self, tools: list) -> list:
        return [{"type": "function", "function": {"name": t["function"]["name"], "description": t["function"]["description"], "parameters": t["function"]["parameters"]}} for t in tools]
    async def stream_completion(self, messages: list, tools: list):
        retry_cfg = self.retry
        max_retries = retry_cfg.get("max_retries", 3)
        total_endpoints = len(self.endpoints)
        original_index = self.active_endpoint_index
        for offset in range(total_endpoints):
            if offset > 0:
                self.switch_to_next_endpoint()
            last_error_msg = ""
            for attempt in range(max_retries):
                try:
                    async for event in self._do_stream(messages, tools):
                        yield event
                    return
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
                    last_error_msg = f"Connection error ({type(e).__name__}): {e}"
                    if attempt < max_retries - 1:
                        delay = retry_cfg.get("delays", [10, 20, 40])[attempt] if attempt < len(retry_cfg.get("delays", [])) else 10
                        await asyncio.sleep(delay)
                        continue
                except httpx.HTTPStatusError as e:
                    status = e.response.status_code
                    try:
                        resp_body = e.response.text[:3000]
                    except Exception:
                        resp_body = "(unable to read response body)"
                    if status == 429:
                        self.limiter.force_429_cooldown(60)
                        backoff = self.config.get("rate_limit", {}).get("backoff_delays", [10, 20, 40])
                        last_error_msg = f"HTTP 429 Rate Limited\nResponse: {resp_body}"
                        if attempt < max_retries - 1:
                            delay = backoff[attempt] if attempt < len(backoff) else backoff[-1]
                            await asyncio.sleep(delay)
                            continue
                    elif 400 <= status < 500:
                        yield ("error", f"HTTP {status} Client Error\nResponse: {resp_body}")
                        return
                    elif status >= 500:
                        last_error_msg = f"HTTP {status} Server Error\nResponse: {resp_body}"
                        if attempt < max_retries - 1:
                            delay = retry_cfg.get("delays", [10, 20, 40])[attempt] if attempt < len(retry_cfg.get("delays", [])) else 10
                            await asyncio.sleep(delay)
                            continue
                except Exception as e:
                    yield ("error", f"Unexpected error: {type(e).__name__}: {e}")
                    return
            if last_error_msg:
                yield ("error", f"{last_error_msg} (max retries exceeded)")
        self.active_endpoint_index = original_index
        yield ("error", "All API endpoints failed.")
    async def _do_stream(self, messages: list, tools: list):
        budget_err = self.limiter.check_budget()
        if budget_err:
            yield ("error", f"⛔ {budget_err}")
            return
        mult = float(self.get_active_endpoint().get("token_count_multiplier", 1.0))
        input_tokens = messages_token_count(messages, mult)
        wait_time = await self.limiter.acquire(input_tokens)
        if wait_time > 0:
            yield ("rate_limit_wait", wait_time)
        ep = self.get_active_endpoint()
        ep_type = self.get_ep_type(ep)
        is_anthropic = ep_type == "anthropic"
        base = ep.get("base", "").rstrip("/")
        url = f"{base}/messages" if is_anthropic else f"{base}/chat/completions"
        payload = self.build_payload(messages, tools, stream=True)
        headers = self.get_headers(ep)
        pending_tool_calls: dict[int, dict] = {}
        timeout_cfg = ep.get("timeout", {})
        timeout = httpx.Timeout(timeout_cfg.get("connect", 120), read=timeout_cfg.get("read", 120))
        async with httpx.AsyncClient(timeout=timeout, limits=httpx.Limits(max_connections=10)) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    safe_headers, safe_payload = self._sanitize_request_for_log(headers, payload)
                    try:
                        body = await resp.aread()
                        body_text = body.decode(errors="replace")
                    except Exception:
                        body_text = "(unable to read response body)"
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}: {body_text[:500]}",
                        request=resp.request,
                        response=resp
                    )
                usage = {}
                if is_anthropic:
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            break
                        try:
                            ev = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        t = ev.get("type", "")
                        if t == "content_block_start":
                            block = ev.get("content_block", {})
                            if block.get("type") == "tool_use":
                                pending_tool_calls[ev["index"]] = {"id": block["id"], "name": block["name"], "input_json": ""}
                        elif t == "content_block_delta":
                            delta = ev.get("delta", {}); dt = delta.get("type", ""); idx = ev.get("index", 0)
                            if dt == "text_delta":
                                yield ("text", delta.get("text", ""))
                            elif dt == "thinking_delta":
                                yield ("thinking", delta.get("thinking", ""))
                            elif dt == "input_json_delta" and idx in pending_tool_calls:
                                pending_tool_calls[idx]["input_json"] += delta.get("partial_json", "")
                        elif t == "content_block_stop":
                            idx = ev.get("index", 0)
                            if idx in pending_tool_calls:
                                tc = pending_tool_calls.pop(idx)
                                try:
                                    args = json.loads(tc["input_json"] or "{}")
                                except json.JSONDecodeError:
                                    args = {}
                                yield ("tool_call", {"id": tc["id"], "name": tc["name"], "arguments": args})
                        elif t == "message_delta":
                            usage = ev.get("usage", {})
                            if usage:
                                yield ("done_partial_usage", usage)
                        elif t == "message_stop":
                            yield ("done", {}); return
                else:
                    done_sent = False; oai_accumulated_text = ""; last_usage: dict = {}
                    async for line in resp.aiter_lines():
                        if done_sent or not line:
                            continue
                        line = line.strip()
                        if line.startswith("event:"):
                            continue
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if raw == "[DONE]":
                            if oai_accumulated_text and not pending_tool_calls:
                                xml_calls, cleaned = _parse_xml_tool_calls(oai_accumulated_text)
                                if xml_calls:
                                    yield ("text_replace", cleaned)
                                    for xc in xml_calls:
                                        yield ("tool_call", xc)
                            yield ("done", last_usage)
                            done_sent = True
                            continue
                        try:
                            ev = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        last_usage = ev.get("usage", {})
                        for choice in ev.get("choices", []):
                            delta = choice.get("delta", {})
                            reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                            if reasoning:
                                yield ("thinking", reasoning)
                            content = delta.get("content")
                            if content:
                                oai_accumulated_text += content; yield ("text", content)
                            for tc in delta.get("tool_calls", []):
                                idx = tc.get("index", 0)
                                if idx not in pending_tool_calls:
                                    pending_tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                                if tc.get("id"):
                                    pending_tool_calls[idx]["id"] = tc["id"]
                                fn = tc.get("function", {})
                                if fn.get("name"):
                                    pending_tool_calls[idx]["name"] = fn["name"]
                                if fn.get("arguments"):
                                    pending_tool_calls[idx]["arguments"] += fn["arguments"]
                            finish = choice.get("finish_reason")
                            if finish in ("tool_calls", "function_call"):
                                for idx, tc in pending_tool_calls.items():
                                    try:
                                        args = json.loads(tc["arguments"] or "{}")
                                    except json.JSONDecodeError:
                                        args = {}
                                    yield ("tool_call", {"id": tc["id"], "name": tc["name"], "arguments": args})
                                pending_tool_calls.clear(); yield ("done", ev.get("usage", {})); done_sent = True
                            elif finish in ("stop", "length"):
                                if oai_accumulated_text and not pending_tool_calls:
                                    xml_calls, cleaned = _parse_xml_tool_calls(oai_accumulated_text)
                                    if xml_calls:
                                        yield ("text_replace", cleaned)
                                        for xc in xml_calls:
                                            yield ("tool_call", xc)
                                yield ("done", ev.get("usage", {})); done_sent = True
        return
# ─────────────────────────── Context Compression (Progressive Rounds) ───────────────────────────
def _load_compression_rounds(config: dict) -> list[dict]:
    comp = config.get("compression", {})
    rounds = comp.get("rounds", [])
    if not rounds:
        ctx = config.get("context", {})
        return [{"name": "默认压缩", "keep_recent": ctx.get("keep_recent", 3), "summary_max_chars": 500, "trigger_token_ratio": ctx.get("compress_threshold", 0.75), "mode": "summarize"}]
    return sorted(rounds, key=lambda r: r.get("trigger_token_ratio", 0.99), reverse=True)
def _get_active_compression_round(rounds: list[dict], token_count: int, context_limit: int) -> dict | None:
    for r in rounds:
        if token_count > context_limit * r.get("trigger_token_ratio", 0.99):
            return r
    return None
def apply_progressive_compression(messages: list, system_prompt: str, config: dict,
                                   context_limit: int | None = None,
                                   multiplier: float = 1.0) -> list:
    rounds = _load_compression_rounds(config)
    ctx_limit = context_limit or config.get("context", {}).get("limit") or 80000
    all_msgs = [{"role": "system", "content": system_prompt}] + messages
    active_round = _get_active_compression_round(rounds, messages_token_count(all_msgs, multiplier), ctx_limit)
    if active_round is None:
        return messages
    return _compress_messages(messages, system_prompt, active_round.get("keep_recent", 3), active_round.get("summary_max_chars", 500), active_round.get("mode", "summarize"))
def _compress_messages(messages: list, system_prompt: str, keep_recent: int, summary_max_chars: int, mode: str) -> list:
    non_system = [m for m in messages if m["role"] != "system"]
    if len(non_system) <= 0:
        return non_system
    turns = []; current_turn = []
    for m in non_system:
        if m.get("role") == "user":
            if current_turn:
                turns.append(current_turn)
            current_turn = [m]
        else:
            current_turn.append(m)
    if current_turn:
        turns.append(current_turn)
    if len(turns) <= keep_recent:
        return non_system
    old_turns = turns[:-keep_recent]; recent_turns = turns[-keep_recent:]
    if mode == "truncate":
        return [m for turn in recent_turns for m in turn]
    summary_parts = []
    for turn in old_turns:
        parts_inner = []
        for m in turn:
            content = m.get("content", "")
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
            parts_inner.append(f"[{m.get('role', '').upper()}]: {content[:summary_max_chars]}")
        summary_parts.append(" | ".join(parts_inner))
    summary_text = "Previous conversation summary:\n" + "\n".join(summary_parts)
    return [{"role": "user", "content": f"[CONTEXT COMPRESSED]\n{summary_text}"}, {"role": "assistant", "content": "Understood. I have the context summary."}] + [m for turn in recent_turns for m in turn]
# ─────────────────────────── UI Components ───────────────────────────
ROLE_STYLES = {"user": ("◆ You", "#64B5F6"), "assistant": ("◇ Assistant", "#A5D6A7"), "tool": ("⚙ Tool", "#FFD54F"), "thinking": ("◈ Thinking", "#CE93D8"), "system": ("⊡ System", "#90A4AE"), "error": ("✗ Error", "#EF5350"), "info": ("ℹ Info", "#4FC3F7")}
def _escape_rich_markup(text: str) -> str:
    return text.replace("[", "\\[") if text else text
CSS = """
Screen { background: #0D1117; }
#main-layout { layout: vertical; height: 100%; }
#chat-area { height: 1fr; border: solid #21262D; border-title-color: #58A6FF; border-title-background: #0D1117; padding: 0 0; overflow-y: auto; scrollbar-color: #30363D #0D1117; }
#chat-area > * { height: auto; }
#status-bar { height: auto; background: #161B22; layout: horizontal; padding: 0 1; }
#status-main { width: 1fr; color: #8B949E; height: 1; }
#input-area { height: auto; max-height: 12; border: solid #30363D; border-title-color: #8B949E; background: #0D1117; }
#user-input { background: #0D1117; color: #E6EDF3; border: none; padding: 0 1; height: auto; min-height: 3; max-height: 10; }
#toolbar { height: auto; background: #161B22; layout: horizontal; padding: 0 1; }
.toolbar-btn { background: #21262D; color: #8B949E; border: none; height: auto; min-width: 8; margin-right: 1; padding: 0 1; }
.toolbar-btn:hover { background: #30363D; color: #E6EDF3; }
.toolbar-btn.-active { background: #1F6FEB; color: #FFFFFF; }
.msg-content { color: #E6EDF3; padding: 0 1; height: auto; }
.msg-tool-call { color: #FFD54F; background: #1A1F2E; padding: 0 1; margin: 0; }
.msg-tool-result { color: #A5D6A7; background: #0D1F0D; padding: 0 1; margin: 0; }
.thinking-body { color: #9575CD; background: #1A0F2E; padding: 0 1; margin: 0; border-left: solid #4A148C; height: auto; }
Footer { background: #161B22; color: #8B949E; }
Header { background: #161B22; color: #58A6FF; }
"""
class MessageBlock(Widget):
    def __init__(self, role: str, content: str, is_thinking: bool = False, is_tool_call: bool = False, is_tool_result: bool = False, tool_name: str = ""):
        super().__init__(); self._role = role; self._content = content; self._is_thinking = is_thinking; self._is_tool_call = is_tool_call; self._is_tool_result = is_tool_result; self._tool_name = tool_name
    def compose(self) -> ComposeResult:
        label, color = ROLE_STYLES.get(self._role, ("◇", "#8B949E"))
        if self._is_thinking:
            label, color = ROLE_STYLES["thinking"]
        elif self._is_tool_call:
            label = f"⚙ Call: {_escape_rich_markup(self._tool_name)}"; color = "#FFD54F"
        elif self._is_tool_result:
            label = f"⚙ Result: {_escape_rich_markup(self._tool_name)}"; color = "#A5D6A7"
        chars = len(self._content); tokens_est = chars // 4; ts = datetime.now().strftime("%H:%M:%S")
        title = f"[bold {color}]{_escape_rich_markup(label)}[/] [dim]{ts}[/] [dim #8B949E]{chars} chars ({tokens_est:,} tokens)[/]"
        with Collapsible(title=title, collapsed=self._role not in ("assistant", "user"), id=f"msg-{self._role}-{id(self)}"):
            if self._is_thinking:
                yield Static(_escape_rich_markup(self._content), classes="msg-thinking")
            elif self._is_tool_call or self._is_tool_result:
                yield Static(_escape_rich_markup(self._content), classes="msg-tool-call" if self._is_tool_call else "msg-tool-result")
            else:
                yield Markdown(self._content, classes="msg-content")
class StreamingBlock(Widget):
    def __init__(self, role: str):
        super().__init__(); self._role = role; self._static: Static | None = None; self._content = ""
    def compose(self) -> ComposeResult:
        label, color = ROLE_STYLES.get(self._role, ("◇", "#8B949E")); ts = datetime.now().strftime("%H:%M:%S")
        with Collapsible(title=f"[bold {color}]{_escape_rich_markup(label)}[/] [dim]{ts}[/]", collapsed=False, id=f"stream-{id(self)}"):
            self._static = Static("▌", classes="msg-content", markup=False); self._static.styles.height = "auto"; yield self._static
    def append(self, chunk: str):
        self._content += chunk
        if self._static:
            self._static.update(self._content + "▌")
    def finalize(self) -> str:
        if self._static:
            self._static.update(self._content)
        return self._content
class ThinkingBlock(Widget):
    text = reactive("", layout=False, repaint=False)
    def __init__(self):
        super().__init__(); self._streaming = True; self._pending_chunks: list[str] = []; self._ts = datetime.now().strftime("%H:%M:%S")
    def compose(self) -> ComposeResult:
        label, color = ROLE_STYLES["thinking"]; chars = len(self.text); tokens_est = chars // 4
        title = f"[bold {color}]{label}[/] [dim]{self._ts}[/] [dim #CE93D8]{chars} chars ({tokens_est:,} tokens)[/]"
        self._title_template = f"[bold {color}]{label}[/] [dim]{self._ts}[/] [dim #CE93D8]{{}} chars ({{:,}} tokens)[/]"
        with Collapsible(title=title, collapsed=True, id="thinking-collapsible"):
            self._static = Static("", classes="thinking-body", markup=False)
            yield self._static
    def _update_title(self):
        try:
            self.query_one("#thinking-collapsible", Collapsible).title = self._title_template.format(len(self.text), len(self.text) // 4)
        except Exception:
            pass
    def on_mount(self):
        if self._pending_chunks and self._static:
            self.text = "".join(self._pending_chunks); self._pending_chunks.clear()
            self._static.update(self.text + "▌"); self._update_title()
    def append(self, chunk: str):
        was_empty = len(self.text) == 0
        self.text += chunk
        if self._static:
            if was_empty:
                try:
                    self.query_one("#thinking-collapsible", Collapsible).collapsed = False
                except Exception:
                    pass
            self._static.update(self.text + "▌"); self._static.refresh(); self._update_title()
        else:
            self._pending_chunks.append(chunk)
    def finalize(self):
        self._streaming = False
        if self._static:
            self._static.update(self.text); self._update_title()
            try:
                self.query_one("#thinking-collapsible", Collapsible).collapsed = True
            except Exception:
                pass
# ─────────────────────────── Main App ───────────────────────────
class ZCodeApp(App):
    CSS = CSS
    BINDINGS = [
        Binding("ctrl+l", "clear_chat", "Clear", show=True),
        Binding("ctrl+t", "toggle_thinking", "Thinking", show=True),
        Binding("ctrl+n", "new_session", "New Session", show=True),
        Binding("ctrl+c", "quit", "Quit", show=True),
        Binding("ctrl+enter", "submit", "Send", show=True),
        Binding("ctrl+z", "interrupt", "Stop", show=True),
        Binding("f1", "show_help", "Help", show=False),
        Binding("f2", "toggle_tools", "Tools On/Off", show=False),
    ]
    TITLE = "ZCode"
    SUB_TITLE = "AI Coding Assistant"
    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.skills = load_skills(self.config)
        self.mcp = MCPClient()
        self.llm = LLMClient(self.config)
        self.llm.app = self
        self.messages: list[dict] = []
        self.tools_enabled = True
        self._streaming = False
        self._interrupt = False
        self._current_stream_block: StreamingBlock | None = None
        self._total_tokens_in = 0
        self._total_tokens_out = 0
        self._stream_chunk_count = 0
        self._last_input_text = ""
        self._exec_text = ""
        self._retrying = False
        self._session_file_path: Path | None = None
        self._session_write_lock = asyncio.Lock()
        self._last_compression_round: str | None = None
        self._pending_images: list[dict] = []
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="main-layout"):
            with Horizontal(id="status-bar"):
                yield Static("", id="status-main")
            with ScrollableContainer(id="chat-area"):
                pass
            with Horizontal(id="toolbar"):
                yield Button("⚙ Tools: ON", id="btn-tools", classes="toolbar-btn -active")
                _think_enabled = self.llm.get_active_endpoint().get("reasoning", {}).get("enabled", False)
                yield Button(f"◈ Think: {'ON' if _think_enabled else 'OFF'}", id="btn-thinking", classes="toolbar-btn -active" if _think_enabled else "toolbar-btn")
                yield Button("✗ Clear", id="btn-clear", classes="toolbar-btn")
                yield Button("+ New", id="btn-new", classes="toolbar-btn")
                total = len(self.llm.endpoints)
                yield Button(f"EP: 1/{total}" if total > 1 else "EP: -", id="btn-ep", classes="toolbar-btn")
            with Vertical(id="input-area"):
                yield TextArea(id="user-input", soft_wrap=True, tab_behavior="indent", show_line_numbers=False)
        yield Footer()
    def on_mount(self):
        self._start_new_session_file()
        self.update_status()
        self._add_info_message("ZCode ready. Ctrl+Enter to send")
        self._load_mcp()
        self.query_one("#user-input").focus()
        self._sync_thinking_button()
        self._sync_ep_button()
    def _start_new_session_file(self):
        session_dir = Path(__file__).parent / "session_history"
        session_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._session_file_path = session_dir / f"session_{timestamp}.md"
        self._session_file_path.write_text(f"# ZCode Session - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n", encoding="utf-8")
    async def _append_to_session(self, text: str):
        if not self._session_file_path:
            return
        async with self._session_write_lock:
            loop = asyncio.get_event_loop()
            _path, _text = self._session_file_path, text + "\n"
            def _write_log(): 
                with open(_path, "a", encoding="utf-8") as _f: _f.write(_text)
            await loop.run_in_executor(None, _write_log)
    def _log_info(self, text: str):
        asyncio.get_running_loop().create_task(self._append_to_session(f"### System\n{text}\n"))
    def _log_user(self, text: str):
        asyncio.get_running_loop().create_task(self._append_to_session(f"### User\n{text}\n"))
    def _log_assistant(self, text: str):
        if text.strip():
            asyncio.get_running_loop().create_task(self._append_to_session(f"### Assistant\n{text}\n"))
    def _log_tool_call(self, tool_name: str, args_str: str):
        asyncio.get_running_loop().create_task(self._append_to_session(f"### Tool Call: {tool_name}\n```json\n{args_str}\n```\n"))
    def _log_tool_result(self, tool_name: str, result: str):
        asyncio.get_running_loop().create_task(self._append_to_session(f"### Tool Result: {tool_name}\n{result}\n"))
    def _log_error(self, msg: str):
        asyncio.get_running_loop().create_task(self._append_to_session(f"### Error\n{msg}\n"))
    def _sync_thinking_button(self):
        try:
            ep = self.llm.get_active_endpoint()
            enabled = ep.get("reasoning", {}).get("enabled", False)
            btn = self.query_one("#btn-thinking")
            btn.label = f"◈ Think: {'ON' if enabled else 'OFF'}"
            btn.set_class(enabled, "-active")
            btn.styles.background = "#1F6FEB" if enabled else "#21262D"
            btn.styles.color = "#FFFFFF" if enabled else "#8B949E"
            btn.refresh()
        except Exception:
            pass
    def _sync_ep_button(self):
        try:
            btn = self.query_one("#btn-ep")
        except NoMatches:
            return
        total = len(self.llm.endpoints)
        if total <= 1:
            btn.label = "EP: -"; btn.disabled = True
        else:
            ep = self.llm.get_active_endpoint()
            ep_name = ep.get("name") or ep.get("type", f"ep{self.llm.active_endpoint_index + 1}")
            btn.label = f"EP: {ep_name} ({self.llm.active_endpoint_index + 1}/{total})"; btn.disabled = False
    def on_shutdown(self):
        self.mcp.close_all()
    def _load_mcp(self):
        mcp_cfg = get_mcp_config(self.config)
        if not mcp_cfg.get("enabled", False):
            return
        for name, srv in mcp_cfg.get("servers", {}).items():
            cmd_raw = srv.get("command", "")
            args = srv.get("args", [])
            if not cmd_raw:
                self._add_info_message(f"MCP server '{name}': no command configured, skipping"); continue
            if isinstance(cmd_raw, str):
                cmd = cmd_raw.split()
            elif isinstance(cmd_raw, list):
                cmd = list(cmd_raw)
            else:
                self._add_info_message(f"MCP server '{name}': invalid command type, skipping"); continue
            if isinstance(args, list):
                cmd = cmd + args
            asyncio.get_event_loop().create_task(self._connect_mcp(name, cmd, srv.get("env")))
    async def _connect_mcp(self, name, cmd, env):
        ok = await self.mcp.connect(name, cmd, env)
        msg = f"MCP server '{name}' connected ({len(self.mcp.tools)} tools)" if ok else f"MCP server '{name}' failed to connect"
        self._add_info_message(msg); self._log_info(msg); self.update_status()
    def _refresh_status(self, exec_text: str = ""):
        try:
            bar = self.query_one("#status-main")
        except NoMatches:
            return
        state = "[bold #F0883E]●[/]" if self._streaming else "[#3FB950]●[/]"
        model = self.llm.get_active_endpoint().get("model", "?").split("/")[-1][:20]
        ep = self.llm.get_active_endpoint()
        ep_type_tag = f"[dim]{ep.get('type','?')}[/]"
        mult = float(ep.get("token_count_multiplier", 1.0))
        ctx_used = messages_token_count(self.messages, mult)
        ctx_limit = self.config.get("context", {}).get("limit")
        pct = ctx_used / ctx_limit * 100 if ctx_limit else 0
        ctx_color = "red" if pct > 90 else ("yellow" if pct > 75 else "#8B949E")
        tok = f"↑{self._total_tokens_in:,} ↓{self._total_tokens_out:,}"
        sys_prompt = build_system_prompt(self.config, self.skills)
        total_req = self.llm.count_tokens(sys_prompt) + ctx_used + (self.llm.count_tokens(self._last_input_text) if self._last_input_text else 0)
        req_pct = total_req / ctx_limit * 100 if ctx_limit else 0
        req_color = "red" if req_pct > 90 else ("yellow" if req_pct > 75 else "#58A6FF")
        flags = []
        _think_on = self.llm.get_active_endpoint().get("reasoning", {}).get("enabled", False)
        flags.append("[#CE93D8]think:ON[/]" if _think_on else "[dim]think:OFF[/]")
        if not self.tools_enabled:
            flags.append("[dim]no-tools[/]")
        flags.append(f"[#58A6FF]mcp:{len(self.mcp.tools)}[/]")
        if self.skills:
            flags.append(f"[#58A6FF]sk:{len(self.skills)}[/]")
        if len(self.llm.endpoints) > 1:
            flags.append(f"[#F0883E]ep:[{self.llm.active_endpoint_index + 1}/{len(self.llm.endpoints)}][/]")
        # Per-endpoint rate-limit status
        rl_st = self.llm.limiter.get_status()
        rl_parts = []
        if rl_st["rpm_limit"] > 0:
            rl_parts.append(f"rpm {rl_st['rpm_bucket']:.0f}/{rl_st['rpm_limit']}")
        if rl_st["tpm_limit"] > 0:
            rl_parts.append(f"tpm {rl_st['tpm_bucket']:,.0f}/{rl_st['tpm_limit']:,}")
        if rl_st["qps_limit"] > 0:
            rl_parts.append(f"qps {rl_st['qps_bucket']:.1f}/{rl_st['qps_limit']:.1f}")
        if rl_st["token_budget"] > 0:
            remaining = rl_st["budget_remaining"]
            budget_pct = remaining / rl_st["token_budget"] * 100
            bcolor = "red" if budget_pct < 10 else ("yellow" if budget_pct < 25 else "#8B949E")
            rl_parts.append(f"[{bcolor}]budget {remaining:,}/{rl_st['token_budget']:,}[/]")
        if rl_parts:
            flags.append(f"[dim]rl:[/][#8B949E]{'  '.join(rl_parts)}[/]")
        parts = [state, f"[#58A6FF]{model}[/]", ep_type_tag, f"[{ctx_color}]ctx {pct:.0f}%[/]", f"[dim]{tok}[/]", f"[{req_color}]req {total_req:,}t[/]"]
        bar.update("  [dim]│[/]  ".join(parts) + ("  " + "  ".join(flags) if flags else "") + (f"  [dim]│[/]  {exec_text}" if exec_text else ""))
    def update_status(self):
        self._refresh_status(self._exec_text)
    def update_token_preview(self, input_text: str):
        self._last_input_text = input_text
        if input_text:
            tokens = self.llm.count_tokens(input_text)
            self._exec_text = f"[dim]input {tokens:,} tokens[/]"
        self._refresh_status(self._exec_text)
    def update_exec_state(self, text: str):
        self._exec_text = text; self._refresh_status(text)
    def _add_info_message(self, text: str):
        self.query_one("#chat-area").mount(MessageBlock("info", text)); self._log_info(text); self._scroll_bottom()
    def _scroll_bottom(self):
        try:
            self.query_one("#chat-area", ScrollableContainer).scroll_end(animate=False)
        except Exception:
            pass
    def _guess_mime(self, path: str) -> str:
        return MIME_MAP.get(Path(path).suffix.lower(), "image/png")

    def _attach_image(self, image_path: str) -> bool:
        try:
            img_path = Path(image_path).expanduser()
            if not img_path.exists():
                self._add_info_message(f"✗ Image not found: {image_path}")
                return False
            if img_path.suffix.lower() not in MIME_MAP:
                self._add_info_message(f"✗ Unsupported image format: {img_path.suffix}")
                return False
            import base64
            img_bytes = img_path.read_bytes()
            mime_type = self._guess_mime(str(img_path))
            b64_data = base64.b64encode(img_bytes).decode()
            self._pending_images.append({
                "path": str(img_path),
                "data": f"data:{mime_type};base64,{b64_data}",
                "mime_type": mime_type,
            })
            size_kb = len(img_bytes) / 1024
            img_count = len(self._pending_images)
            self._add_info_message(f"📷 [{img_count}] Attached: {img_path.name} ({size_kb:.1f} KB)")
            return True
        except Exception as e:
            self._add_info_message(f"✗ Failed to load image: {e}")
            return False

    def _build_user_message(self, text: str) -> dict:
        if not self._pending_images:
            return {"role": "user", "content": text}
        content = []
        is_anthropic = self.llm.get_ep_type() == "anthropic"
        for img in self._pending_images:
            mime = img.get("mime_type", "image/png")
            b64 = img["data"].split(";base64,", 1)[-1] if ";base64," in img["data"] else img["data"]
            if is_anthropic:
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": b64}
                })
            else:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": img["data"]}
                })
        content.append({"type": "text", "text": text})
        attached = [img["path"] for img in self._pending_images]
        self._add_info_message(f"📷 Sending with {len(attached)} image(s): {', '.join(Path(p).name for p in attached)}")
        self._pending_images.clear()
        return {"role": "user", "content": content}

    def _parse_image_refs(self, text: str) -> str:
        pattern = re.compile(r'@image:(\S+)')
        matches = pattern.findall(text)
        for match in matches:
            self._attach_image(match)
        return pattern.sub('', text).strip()

    @on(Button.Pressed, "#btn-tools")
    def btn_tools(self):
        self.action_toggle_tools()
    @on(Button.Pressed, "#btn-thinking")
    def btn_thinking(self):
        self.action_toggle_thinking()
    @on(Button.Pressed, "#btn-clear")
    def btn_clear(self):
        self.action_clear_chat()
    @on(Button.Pressed, "#btn-new")
    def btn_new(self):
        self.action_new_session()
    @on(Button.Pressed, "#btn-ep")
    def btn_ep(self):
        if self._streaming:
            self.notify("Cannot switch endpoint while streaming", severity="warning"); return
        if len(self.llm.endpoints) <= 1:
            self.notify("Only one endpoint configured", severity="information"); return
        self.llm.switch_to_next_endpoint()
        self._sync_ep_button(); self._sync_thinking_button(); self.update_status()
        self.notify(f"Switched to endpoint {self.llm.active_endpoint_index+1}: {self.llm.get_active_endpoint().get('model','?')}")
    def action_submit(self):
        inp = self.query_one("#user-input", TextArea)
        text = inp.text.strip()
        if text and not self._streaming:
            inp.clear(); self._submit_message(text)
    def _save_config(self):
        """将当前 self.config（含所有 endpoint 的 reasoning 状态）写回 config.json。"""
        try:
            with open(CONFIG_TEMPLATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.notify(f"⚠ 保存配置失败: {e}", severity="warning")

    def action_toggle_thinking(self):
        ep = self.llm.get_active_endpoint()
        ep_reasoning = ep.setdefault("reasoning", {})
        ep_reasoning["enabled"] = not ep_reasoning.get("enabled", False)
        self._save_config()
        btn = self.query_one("#btn-thinking")
        on = ep_reasoning['enabled']
        btn.label = f"◈ Think: {'ON' if on else 'OFF'}"
        btn.set_class(on, "-active")
        btn.styles.background = "#1F6FEB" if on else "#21262D"
        btn.styles.color = "#FFFFFF" if on else "#8B949E"
        btn.refresh()
        self.update_status()
        ep_name = ep.get("name", f"ep{self.llm.active_endpoint_index + 1}")
        self.notify(f"Thinking [{ep_name}]: {'ON' if on else 'OFF'}")
    def action_toggle_tools(self):
        self.tools_enabled = not self.tools_enabled
        btn = self.query_one("#btn-tools")
        on = self.tools_enabled
        btn.label = f"⚙ Tools: {'ON' if on else 'OFF'}"
        btn.set_class(on, "-active")
        btn.styles.background = "#1F6FEB" if on else "#21262D"
        btn.styles.color = "#FFFFFF" if on else "#8B949E"
        btn.refresh()
        self.update_status()
    def action_clear_chat(self):
        self.query_one("#chat-area").remove_children(); self._add_info_message("Chat cleared.")
    def action_new_session(self):
        self.messages.clear(); self._total_tokens_in = 0; self._total_tokens_out = 0
        self._pending_images.clear()
        self.query_one("#chat-area").remove_children(); self._start_new_session_file()
        self._add_info_message("New session started."); self.update_status()
    def action_interrupt(self):
        if self._streaming:
            self._interrupt = True; self.notify("Interrupting…", severity="warning")
    def action_show_help(self):
        skills_dir = get_skills_dir(self.config)
        help_text = f"""**Keyboard Shortcuts**
- **Ctrl+Enter** — Send message
- **Ctrl+L** — Clear chat display
- **Ctrl+N** — New session (clears history)
- **Ctrl+T** — Toggle thinking mode
- **Ctrl+Z** — Interrupt streaming
- **F2** — Toggle tools on/off
- **F1** — This help
- **Ctrl+C** — Quit
**Skill Commands:**
- `@skill` — List all available skills
- The assistant uses the `load_skill` tool to load skill instructions when needed

**Image Attachments:**
- `@image:path/to/file.png` — Attach an image for vision-capable models

**Skills Directory:** {skills_dir}
Skills are loaded on demand via the `load_skill` tool.
"""
        self.query_one("#chat-area").mount(MessageBlock("info", help_text)); self._log_info(help_text); self._scroll_bottom()

    # ── Chat submission ──
    def _submit_message(self, text: str):
        # Parse @image: references and load images
        text = self._parse_image_refs(text)
        
        # Handle @skill command: list available skills
        if text.startswith("@skill"):
            self._pending_images.clear()
            self.skills = load_skills(self.config)
            skill_list = "\n".join(f"- **{name}**" for name in sorted(self.skills.keys()))
            if not skill_list:
                skill_list = "(no skills found in configured skills directory)"
            info_text = f"**Available Skills:**\n{skill_list}\n\nTip: The assistant can load skills automatically when needed using the `load_skill` tool."
            self.query_one("#chat-area").mount(MessageBlock("info", info_text))
            self._log_info(info_text)
            self._scroll_bottom()
            return
        
        if not text and not self._pending_images:
            return
        
        # Reload skills from the configured directory
        self.skills = load_skills(self.config)
        
        self.query_one("#chat-area").mount(MessageBlock("user", text))
        self._log_user(text)
        self._scroll_bottom()
        
        sys_prompt = build_system_prompt(self.config, self.skills)
        
        user_msg = self._build_user_message(text)
        self._pending_images.clear()
        self.messages.append(user_msg)
        self._maybe_compress(sys_prompt)
        full_msgs = [{"role": "system", "content": sys_prompt}] + self.messages
        tools = BUILTIN_TOOLS + self.mcp.tools if self.tools_enabled else []
        self._streaming = True; self._interrupt = False; self._stream_chunk_count = 0
        self.update_status()
        self.update_exec_state(f"[dim]turn 1[/]  [#58A6FF]⟳ requesting model…[/]")
        self._run_completion(full_msgs, tools, sys_prompt)

    def _get_context_limit(self) -> int:
        """Per-endpoint context limit, falling back to global config."""
        ep = self.llm.get_active_endpoint()
        return int(ep.get("context_limit") or self.config.get("context", {}).get("limit") or 80000)

    def _get_keep_recent(self) -> int:
        ep = self.llm.get_active_endpoint()
        return int(ep.get("keep_recent") or self.config.get("context", {}).get("keep_recent") or 3)

    def _maybe_compress(self, sys_prompt: str):
        rounds = _load_compression_rounds(self.config)
        if not rounds:
            return
        ctx_limit = self._get_context_limit()
        mult = float(self.llm.get_active_endpoint().get("token_count_multiplier", 1.0))
        all_msgs = [{"role": "system", "content": sys_prompt}] + self.messages
        current = messages_token_count(all_msgs, mult)
        active_round = _get_active_compression_round(rounds, current, ctx_limit)
        if active_round is None:
            return
        round_name = active_round.get("name", "Unknown")
        if round_name != self._last_compression_round:
            self._add_info_message(f"⚡ Compression: {round_name}")
        self.messages = apply_progressive_compression(self.messages, sys_prompt, self.config, ctx_limit, mult)
        new_count = messages_token_count([{"role": "system", "content": sys_prompt}] + self.messages, mult)
        self._total_tokens_in = new_count
        self._total_tokens_out = 0
        self._add_info_message(f"✓ Compressed: {current:,} → {new_count:,} tokens")
        self._last_compression_round = round_name

    def _compress_and_retry(self, tools: list, sys_prompt: str):
        keep_turns = self._get_keep_recent()
        mult = float(self.llm.get_active_endpoint().get("token_count_multiplier", 1.0))
        compressed = _compress_messages(self.messages, sys_prompt, keep_turns, 300, "summarize")
        # 防止重复压缩死循环：如果压缩后消息未变化则放弃重试
        if compressed == self.messages:
            self._add_info_message("⚠ Compression had no effect; context overflow is unrecoverable.")
            return
        self._add_info_message(f"⚡ Context overflow, compressing to last {keep_turns} turns...")
        self.messages = compressed
        new_count = messages_token_count([{"role": "system", "content": sys_prompt}] + self.messages, mult)
        self._total_tokens_in = new_count
        self._total_tokens_out = 0
        self._add_info_message(f"✓ Compressed to {new_count:,} tokens, retrying...")
        self._retrying = True
        full_msgs = [{"role": "system", "content": sys_prompt}] + self.messages
        self._run_completion(full_msgs, tools, sys_prompt)

    @work(exclusive=True)
    async def _run_completion(self, messages: list, tools: list, sys_prompt: str):
        chat_area = self.query_one("#chat-area")
        original_ep_index = self.llm.active_endpoint_index
        try:
            loop_count = 0
            tool_cfg = self.config.get("tool", {})
            max_loops = tool_cfg.get("max_loops", 100); tool_timeout = tool_cfg.get("timeout", 300)
            command_timeout = tool_cfg.get("command_timeout", 30); search_timeout = tool_cfg.get("search_timeout", 15)
            result_preview_length = tool_cfg.get("result_preview_length", 5000)
            while loop_count < max_loops:
                loop_count += 1
                self.update_exec_state(f"[dim]turn {loop_count}[/]  [#58A6FF]⟳ requesting model…[/]")
                await asyncio.sleep(0)  # 让 Textual 有机会绘制状态后再进入流循环
                thinking_block = None; stream_block = StreamingBlock("assistant"); self._current_stream_block = stream_block
                full_text = ""; tool_calls_received = []; got_thinking = False; final_text = ""
                async for event_type, event_data in self.llm.stream_completion(messages, tools):
                    if self._interrupt:
                        break
                    if event_type in ("info", "rate_limit_wait"):
                        continue
                    if event_type == "text":
                        if stream_block.parent is None:
                            await chat_area.mount(stream_block); await asyncio.sleep(0)
                        full_text += event_data; final_text += event_data; stream_block.append(event_data)
                        self._stream_chunk_count += 1
                        if self._stream_chunk_count % 8 == 0:
                            self._scroll_bottom()
                    elif event_type == "thinking":
                        if not got_thinking:
                            got_thinking = True
                            thinking_block = ThinkingBlock(); await chat_area.mount(thinking_block); await asyncio.sleep(0.01); self._scroll_bottom()
                        if thinking_block:
                            thinking_block.append(event_data)
                            if self._stream_chunk_count % 4 == 0:
                                self._scroll_bottom()
                    elif event_type == "tool_call":
                        tool_calls_received.append(event_data)
                    elif event_type == "text_replace":
                        final_text = event_data; full_text = event_data
                        if stream_block.parent is not None:
                            stream_block._content = event_data
                            stream_block.finalize()
                    elif event_type == "done_partial_usage":
                        usage = event_data
                        in_t = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
                        out_t = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
                        self._total_tokens_in += in_t; self._total_tokens_out += out_t
                        if self.llm.limiter:
                            self.llm.limiter.consume_tokens(out_t, in_t)
                    elif event_type == "done":
                        usage = event_data
                        in_t = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
                        out_t = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
                        self._total_tokens_in += in_t; self._total_tokens_out += out_t
                        if self.llm.limiter:
                            self.llm.limiter.consume_tokens(out_t, in_t)
                    elif event_type == "error":
                        error_msg = str(event_data)
                        if ("maximum context length" in error_msg.lower() or "requested about" in error_msg.lower()) and loop_count == 1:
                            self._compress_and_retry(tools, sys_prompt); return
                        if stream_block.parent is not None:
                            stream_block.finalize()
                        if thinking_block and thinking_block.parent is not None:
                            thinking_block.finalize()
                        await chat_area.mount(MessageBlock("error", event_data)); self._log_error(error_msg)
                        self._streaming = False; self.update_status(); self.update_exec_state("[red]✗ error[/]"); self._scroll_bottom(); return
                if stream_block.parent is None and final_text:
                    await chat_area.mount(stream_block); await asyncio.sleep(0)
                final_text = stream_block.finalize() if final_text else ""
                if thinking_block:
                    thinking_block.finalize()
                if self._interrupt:
                    await chat_area.mount(MessageBlock("info", "⚡ Interrupted")); self._log_info("Interrupted")
                    self.messages.append({"role": "assistant", "content": final_text or "(interrupted)"}); break
                if not tool_calls_received:
                    if final_text:
                        self._log_assistant(final_text); self.messages.append({"role": "assistant", "content": final_text})
                    elif stream_block.parent is not None:
                        stream_block.remove()
                    else:
                        self._log_info("Empty response from AI")
                        self.messages.append({"role": "assistant", "content": ""})
                    break
                is_anthropic = self.llm.get_ep_type() == "anthropic"
                if final_text:
                    self._log_assistant(final_text)
                if is_anthropic:
                    assistant_content = [{"type": "text", "text": final_text}] if final_text else []
                    for tc in tool_calls_received:
                        assistant_content.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["arguments"]})
                    self.messages.append({"role": "assistant", "content": assistant_content or ""})
                else:
                    self.messages.append({"role": "assistant", "content": final_text or "", "tool_calls": [{"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}} for tc in tool_calls_received]})
                tool_results = []
                for tc in tool_calls_received:
                    tname = tc.get("name") or ""; targs = tc.get("arguments") or {}
                    if not tname:
                        tool_results.append((tc.get("id", ""), tname, "Error: tool call had no name")); continue
                    args_str = json.dumps(targs, ensure_ascii=False, indent=2); self._log_tool_call(tname, args_str)
                    self.update_exec_state(f"[dim]turn {loop_count}[/]  [#FFD54F]⚙ calling[/] [bold]{tname}[/]…")
                    await chat_area.mount(MessageBlock("tool", f"**{_escape_rich_markup(tname)}**\n```json\n{_escape_rich_markup(args_str)}\n```", is_tool_call=True, tool_name=tname))
                    self._scroll_bottom()
                    try:
                        if self.mcp.servers and tname.startswith(tuple(f"{s}__" for s in self.mcp.servers)):
                            result = await self.mcp.call_tool(tname, targs, timeout=tool_timeout)
                        else:
                            result = await asyncio.wait_for(execute_builtin_tool(tname, targs, timeout=tool_timeout, command_timeout=command_timeout, search_timeout=search_timeout), timeout=tool_timeout)
                    except asyncio.TimeoutError:
                        result = f"Tool '{tname}' timed out after {tool_timeout} seconds"
                    except Exception as e:
                        result = f"Tool error: {type(e).__name__}: {e}"
                    self._log_tool_result(tname, result)
                    self.update_exec_state(f"[dim]turn {loop_count}[/]  [#A5D6A7]✓ {tname}[/] [dim]done[/]")
                    result_len = len(result)
                    result_clean = _clean_output(result)
                    result_preview = result_clean[:result_preview_length] + (f"\n\n[...truncated, {result_len:,} chars total]" if result_len > result_preview_length else "")
                    await chat_area.mount(MessageBlock("tool", result_preview, is_tool_result=True, tool_name=tname)); self._scroll_bottom()
                    tool_results.append((tc["id"], tname, result))
                if is_anthropic:
                    self.messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": res} for tid, _, res in tool_results]})
                else:
                    for tid, tname, res in tool_results:
                        self.messages.append({"role": "tool", "tool_call_id": tid, "name": tname, "content": res})
                self._maybe_compress(sys_prompt)
                messages = [{"role": "system", "content": sys_prompt}] + self.messages
            if loop_count >= max_loops:
                await chat_area.mount(MessageBlock("info", "⚠ Max tool call loops reached."))
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            await chat_area.mount(MessageBlock("error", f"**Error:** {e}\n```\n{tb[:800]}\n```")); self._log_error(f"{e}\n{tb[:800]}")
        finally:
            was_streaming = self._streaming
            if not self._retrying:
                self._streaming = False
            self._retrying = False; self._current_stream_block = None
            if was_streaming:
                self._exec_text = ""
            if self.llm.active_endpoint_index != original_ep_index:
                self._sync_ep_button()
                self._sync_thinking_button()
            self.update_status(); self.update_token_preview(""); self._scroll_bottom()

    # ── Key handlers ──
    async def on_key(self, event) -> None:
        if event.key == "ctrl+enter":
            event.stop(); self.action_submit()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "user-input":
            self.update_token_preview(event.text_area.text)
# ─────────────────────────── Entry Point ───────────────────────────
def main():
    missing = []
    for mod in ["textual", "httpx"]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        print(f"Missing dependencies: {', '.join(missing)}")
        print(f"Install with: pip install {' '.join(missing)}")
        sys.exit(1)
    app = ZCodeApp(); app.run()
if __name__ == "__main__":
    main()