"""
Claude <-> Feishu Bridge — Multi-Bot Edition
Each bot uses lark_oapi SDK for event streaming + httpx for API calls.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import asyncio
import functools
import uuid
import threading
from pathlib import Path
from typing import Optional, Callable, Awaitable

if sys.platform == "win32":
    import io
    _orig_stdout = sys.stdout
    sys.stdout = io.TextIOWrapper(_orig_stdout.buffer, encoding='utf-8', line_buffering=True)

import httpx


def _kill_process(pid: int):
    """Kill a process tree. On Windows, TerminateProcess leaves orphans;
    taskkill /T ensures all descendants are cleaned up."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, timeout=10,
        )
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass

if getattr(sys, 'frozen', False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent

CONFIG_PATH = APP_DIR / "bridge-config.json"
DATA_DIR = APP_DIR
CLI_SESSIONS_DIR = DATA_DIR / ".cli-sessions"
MEDIA_DIR = DATA_DIR / ".bridge-media"

_LARK_CLI = shutil.which("lark-cli") or shutil.which("lark-cli.cmd")
if not _LARK_CLI:
    _npm_root = os.path.expandvars(r"%APPDATA%\npm")
    for _c in ["lark-cli.cmd", "lark-cli"]:
        _p = os.path.join(_npm_root, _c)
        if os.path.exists(_p):
            _LARK_CLI = _p
            break
if not _LARK_CLI:
    _LARK_CLI = "lark-cli"
LARK_CLI = _LARK_CLI


def _lark_env(bot_name: str = "") -> dict:
    """Per-bot isolated lark-cli home prevents bus daemon cross-contamination."""
    env = os.environ.copy()
    if bot_name:
        home = APP_DIR / f".lark-home-{bot_name}"
        home.mkdir(parents=True, exist_ok=True)
        env["USERPROFILE"] = str(home)
    else:
        env["USERPROFILE"] = str(APP_DIR)
    return env


def _lark(*args, env=None, **kwargs):
    """Run lark-cli with project-scoped env. Returns subprocess.CompletedProcess."""
    return subprocess.run(
        [LARK_CLI] + list(args),
        capture_output=True, text=True, timeout=15,
        env=env or _lark_env(), **kwargs
    )


def _sync_profiles(bots_cfg: dict):
    """Register each bot's profile in its own isolated lark-cli home."""
    for name, bot_cfg in bots_cfg.items():
        app_id = bot_cfg.get("feishu_app_id", "")
        app_secret = bot_cfg.get("feishu_app_secret", "")
        if not app_id or not app_secret:
            continue
        bot_env = _lark_env(name)
        try:
            r = _lark("profile", "list", env=bot_env)
            existing = {p["name"]: p.get("active", False)
                        for p in json.loads(r.stdout)} if r.returncode == 0 else {}
        except Exception:
            existing = {}
        if name in existing:
            print(f"  [OK] profile '{name}' exists")
            continue
        print(f"  [{name}] Registering profile...")
        r = _lark("profile", "add", "--name", name, "--app-id", app_id,
                   "--brand", "feishu", "--app-secret-stdin",
                   env=bot_env, input=app_secret)
        if r.returncode != 0:
            print(f"  [{name}] WARN: {r.stderr or r.stdout}")
        else:
            print(f"  [{name}] profile registered")


def load_json(path: Path, default):
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (UnicodeDecodeError, json.JSONDecodeError):
            print(f"WARN: corrupted file {path}, using default")
            return default
    return default


def save_json(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if "bots" not in cfg:
        cfg = {
            "bots": {
                "default": {
                    "feishu_app_id": cfg["feishu"]["app_id"],
                    "feishu_app_secret": cfg["feishu"]["app_secret"],
                    "claude": cfg["claude"],
                }
            }
        }
    for name, bot_cfg in cfg["bots"].items():
        cc = bot_cfg.get("claude") or {}
        if not cc.get("system_prompt"):
            raise RuntimeError(f"Bot '{name}' has no claude.system_prompt configured")
    return cfg


# ===========================================================================
# ClaudeDaemon - Long-running Claude process manager
# ===========================================================================

class ClaudeDaemon:
    """Manages a long-running Claude process with stream-json I/O.

    Instead of spawning a new `claude -p` for each message, this keeps
    a single Claude process alive and sends messages via stdin stream.
    """

    def __init__(self, bot_name: str, cli_cfg: dict, system_prompt: str,
                 on_output: Optional[Callable[[str], Awaitable[None]]] = None):
        self.bot_name = bot_name
        self.cli_cfg = cli_cfg
        self.system_prompt = system_prompt
        self.on_output = on_output

        self._process: Optional[asyncio.subprocess.Process] = None
        self._session_id: Optional[str] = None
        self._response_futures: dict[str, asyncio.Future] = {}
        self._read_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._running = False
        self._pending_lines: list[str] = []
        self._current_msg_id: Optional[str] = None

    def _build_daemon_args(self) -> tuple[list[str], dict]:
        """Build args for long-running Claude process."""
        timeout = self.cli_cfg.get("timeout_seconds", 300)
        perm_mode = self.cli_cfg.get("permission_mode", "default")

        base = [
            "claude",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--session-id", str(uuid.uuid4()),
        ]

        if perm_mode == "bypass-permissions":
            base.append("--dangerously-skip-permissions")

        if self.cli_cfg.get("model"):
            base += ["--model", self.cli_cfg["model"]]

        for d in self.cli_cfg.get("add_dirs", []):
            base += ["--add-dir", d]

        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        base += ["--add-dir", str(MEDIA_DIR)]

        env = os.environ.copy()
        env["USERPROFILE"] = str(APP_DIR)
        env.update(self.cli_cfg.get("env", {}))

        return base, env

    async def start(self) -> bool:
        """Start the Claude daemon process."""
        async with self._lock:
            if self._running and self._process and self._process.returncode is None:
                return True

            args, env = self._build_daemon_args()
            print(f"[{self.bot_name}] Starting Claude daemon...")

            try:
                self._process = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    cwd=str(CLI_SESSIONS_DIR),
                )
                self._running = True
                self._read_task = asyncio.create_task(self._read_output())
                print(f"[{self.bot_name}] Claude daemon started (PID: {self._process.pid})")
                return True
            except Exception as e:
                print(f"[{self.bot_name}] Failed to start daemon: {e}")
                return False

    async def _read_output(self):
        """Read and parse stream-json output from Claude."""
        buffer = ""
        while self._running and self._process and self._process.returncode is None:
            try:
                chunk = await asyncio.wait_for(
                    self._process.stdout.read(4096), timeout=1.0
                )
                if not chunk:
                    break

                buffer += chunk.decode("utf-8", errors="replace")

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        msg = json.loads(line)
                        await self._handle_stream_message(msg)
                    except json.JSONDecodeError:
                        pass

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                if self._running:
                    print(f"[{self.bot_name}] Daemon read error: {e}")
                break

        print(f"[{self.bot_name}] Daemon output reader stopped")

    async def _handle_stream_message(self, msg: dict):
        """Handle a parsed stream-json message."""
        msg_type = msg.get("type")

        if msg_type == "system":
            self._session_id = msg.get("session_id")
            print(f"[{self.bot_name}] Daemon session: {self._session_id}")

        elif msg_type == "assistant":
            content = msg.get("message", {}).get("content", [])
            text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
            if text_parts and self.on_output:
                await self.on_output("".join(text_parts))

        elif msg_type == "result":
            msg_id = msg.get("request_id")
            if msg_id and msg_id in self._response_futures:
                future = self._response_futures.pop(msg_id)
                result_text = msg.get("result", "")
                if not future.done():
                    future.set_result(result_text)

        elif msg_type == "error":
            error_msg = msg.get("error", "Unknown error")
            print(f"[{self.bot_name}] Daemon error: {error_msg}")
            # Resolve any pending future with error
            for msg_id, future in list(self._response_futures.items()):
                if not future.done():
                    future.set_exception(Exception(error_msg))

    async def send_message(self, message: str, timeout: float = 300) -> str:
        """Send a message to the running Claude process and wait for response."""
        if not self._running or not self._process or self._process.returncode is not None:
            if not await self.start():
                return "[错误] 无法启动 Claude daemon"

        msg_id = str(uuid.uuid4())
        future = asyncio.get_event_loop().create_future()
        self._response_futures[msg_id] = future

        # Send message in stream-json format
        request = {
            "type": "user",
            "message": {
                "role": "user",
                "content": message
            },
            "request_id": msg_id
        }

        try:
            self._process.stdin.write((json.dumps(request) + "\n").encode())
            await self._process.stdin.drain()
        except Exception as e:
            self._response_futures.pop(msg_id, None)
            return f"[错误] 发送消息失败: {e}"

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._response_futures.pop(msg_id, None)
            return "[错误] Claude 响应超时"
        except Exception as e:
            return f"[错误] {str(e)}"

    async def stop(self):
        """Stop the Claude daemon process."""
        self._running = False
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass

        if self._process and self._process.returncode is None:
            try:
                self._process.stdin.close()
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except Exception:
                _kill_process(self._process.pid)
            print(f"[{self.bot_name}] Claude daemon stopped")

    @property
    def is_running(self) -> bool:
        return self._running and self._process is not None and self._process.returncode is None


# ===========================================================================
# BotRunner
# ===========================================================================

class BotRunner:
    def __init__(self, name: str, config: dict):
        self.name = name
        cc = config["claude"]
        self.cli_cfg = config.get("claude_cli", {})
        self.system_prompt = cc["system_prompt"]
        self.display_name = config.get("display_name", name)

        self.feishu_app_id = config["feishu_app_id"]
        self.feishu_app_secret = config["feishu_app_secret"]
        self.feishu_base = "https://open.feishu.cn"
        self.max_context = config.get("max_context_messages", 0)

        self.msg_file = DATA_DIR / f"bridge-last-msg-{name}.json"
        self._session_file = DATA_DIR / f"bridge-sessions-{name}.json"
        self._session_ids: dict[str, str] = load_json(self._session_file, {})
        self._processing: set[str] = set()
        self.last_msg_ids: dict[str, float] = load_json(self.msg_file, {})
        self._tenant_token: str | None = None
        self._token_expire: float = 0
        self._bot_open_id: str | None = None
        self._all_bot_ids: set[str] = set()
        self._siblings: list[dict] = []
        self._session_summaries: dict[str, str] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._pending_approvals: dict[str, asyncio.Event] = {}
        self._approval_results: dict[str, str] = {}
        self._approval_counter: int = 0

        # Claude daemon for long-running process
        self._use_daemon = self.cli_cfg.get("use_daemon", False)
        self._daemon: Optional[ClaudeDaemon] = None

    # -- Feishu REST --

    async def _get_token(self) -> str:
        now = time.time()
        if self._tenant_token and now < self._token_expire:
            return self._tenant_token
        last_err = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=10) as cli:
                    r = await cli.post(
                        f"{self.feishu_base}/open-apis/auth/v3/tenant_access_token/internal",
                        json={"app_id": self.feishu_app_id, "app_secret": self.feishu_app_secret},
                    )
                    data = r.json()
                    if data.get("code") != 0:
                        raise Exception(f"[{self.name}] Token error: {data}")
                    self._tenant_token = data["tenant_access_token"]
                    self._token_expire = now + data.get("expire", 3600) - 300
                    return self._tenant_token
            except Exception as e:
                last_err = e
                if attempt < 2:
                    await asyncio.sleep(1)
        raise last_err  # type: ignore[misc]

    async def _get_bot_open_id(self) -> str:
        if self._bot_open_id:
            return self._bot_open_id
        token = await self._get_token()
        async with httpx.AsyncClient() as cli:
            r = await cli.get(
                f"{self.feishu_base}/open-apis/bot/v3/info",
                headers={"Authorization": f"Bearer {token}"},
            )
        data = r.json()
        self._bot_open_id = data.get("bot", {}).get("open_id", "")
        return self._bot_open_id

    @staticmethod
    def _has_markdown_formatting(text: str) -> bool:
        """Check if text contains Markdown formatting that warrants a card."""
        import re
        return bool(re.search(
            r'(\*\*|__|`{1,3}|^#{1,6}\s|^>\s|^[\-\*]\s|^\d+\.\s'
            r'|\[.*?\]\(.*?\)|^---\s*$)',
            text, re.MULTILINE,
        ))

    @staticmethod
    def _markdown_to_card(text: str) -> dict:
        """Convert Markdown text to a Feishu card with lark_md elements."""
        lines = text.split("\n")
        header_title = None
        body_start = 0

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("# ") and not stripped.startswith("## "):
                header_title = stripped[2:].strip()
                body_start = i + 1
                break

        body = "\n".join(lines[body_start:]).strip()

        elements = []
        remaining = body
        while remaining:
            if len(remaining) <= 4500:
                chunk = remaining.strip()
                if chunk:
                    elements.append({"tag": "div", "text": {"content": chunk, "tag": "lark_md"}})
                break
            chunk = remaining[:4500]
            split_pos = -1
            for sep in ["\n\n", "\n", ". "]:
                pos = chunk.rfind(sep)
                if pos > 2000:
                    split_pos = pos + len(sep)
                    break
            if split_pos < 0:
                split_pos = 4500
            elements.append({"tag": "div", "text": {"content": remaining[:split_pos].strip(), "tag": "lark_md"}})
            remaining = remaining[split_pos:].strip()

        card: dict = {"config": {"wide_screen_mode": True}, "elements": elements}
        if header_title:
            card["header"] = {"title": {"content": header_title, "tag": "plain_text"}}
        return card

    @staticmethod
    def _parse_structured_reply(text: str) -> tuple[str | None, str | None]:
        """If text is valid JSON with msg_type+content or card-like fields,
        return (msg_type, content_json_string). Otherwise return (None, None)."""
        text = text.strip()
        if not text.startswith("{"):
            return None, None
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return None, None
        if not isinstance(data, dict):
            return None, None
        # Format 1: {"msg_type": "...", "content": {...}}
        if "msg_type" in data and "content" in data:
            ct = data["content"]
            if isinstance(ct, dict):
                return data["msg_type"], json.dumps(ct, ensure_ascii=False)
            return data["msg_type"], str(ct)
        # Format 2: raw card body with card-like fields
        if any(k in data for k in ("header", "elements", "config", "card_link")):
            return "interactive", json.dumps(data, ensure_ascii=False)
        return None, None

    async def _send_reply(self, message_id: str, msg_type: str, content: str) -> str:
        token = await self._get_token()
        print(f"[{self.name}] _send_reply: app={self.feishu_app_id[:14]}..., token={token[:10]}...")
        async with httpx.AsyncClient() as cli:
            r = await cli.post(
                f"{self.feishu_base}/open-apis/im/v1/messages/{message_id}/reply",
                headers={"Authorization": f"Bearer {token}"},
                json={"content": content, "msg_type": msg_type},
            )
            data = r.json()
            if data.get("code") != 0:
                print(f"[{self.name}] Reply error: {data}")
                return ""
            return data.get("data", {}).get("message_id", "")

    async def _edit_message(self, message_id: str, msg_type: str, content: str):
        token = await self._get_token()
        async with httpx.AsyncClient() as cli:
            r = await cli.put(
                f"{self.feishu_base}/open-apis/im/v1/messages/{message_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={"content": content, "msg_type": msg_type},
            )
            data = r.json()
            if data.get("code") != 0:
                print(f"[{self.name}] Edit error: {data}")

    async def _delete_message(self, message_id: str):
        if not message_id:
            return
        token = await self._get_token()
        async with httpx.AsyncClient() as cli:
            r = await cli.delete(
                f"{self.feishu_base}/open-apis/im/v1/messages/{message_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            data = r.json()
            if data.get("code") != 0:
                print(f"[{self.name}] Delete error: {data}")

    async def _reply_text(self, message_id: str, text: str):
        # 1) Explicit card JSON from Claude
        msg_type, card_content = self._parse_structured_reply(text)
        if msg_type and card_content:
            await self._send_reply(message_id, msg_type, card_content)
            return

        # 2) Markdown → Feishu card with lark_md
        if self._has_markdown_formatting(text):
            card = self._markdown_to_card(text)
            await self._send_reply(message_id, "interactive",
                                   json.dumps(card, ensure_ascii=False))
            return

        # 3) Plain text with chunking
        for i in range(0, len(text), 7000):
            chunk = text[i:i + 7000]
            await self._send_reply(message_id, "text",
                                   json.dumps({"text": chunk}, ensure_ascii=False))

    async def _send_message(self, receive_id: str, msg_type: str,
                            content: str, *, title: str = "") -> str:
        token = await self._get_token()
        body = {"receive_id": receive_id, "msg_type": msg_type, "content": content}
        if title:
            body["title"] = title
        async with httpx.AsyncClient() as cli:
            r = await cli.post(
                f"{self.feishu_base}/open-apis/im/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
            data = r.json()
            if data.get("code") != 0:
                print(f"[{self.name}] Send error: {data}")
                return ""
            return data.get("data", {}).get("message_id", "")

    async def _get_chat_info(self, chat_id: str) -> dict:
        token = await self._get_token()
        async with httpx.AsyncClient() as cli:
            r = await cli.get(
                f"{self.feishu_base}/open-apis/im/v1/chats/{chat_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            data = r.json()
            return data.get("data", {}) if data.get("code") == 0 else {}

    async def _get_chat_members(self, chat_id: str) -> list[dict]:
        token = await self._get_token()
        members, page_token = [], ""
        async with httpx.AsyncClient() as cli:
            while True:
                params = {"page_size": 50}
                if page_token:
                    params["page_token"] = page_token
                r = await cli.get(
                    f"{self.feishu_base}/open-apis/im/v1/chats/{chat_id}/members",
                    headers={"Authorization": f"Bearer {token}"}, params=params,
                )
                data = r.json()
                if data.get("code") != 0:
                    break
                d = data.get("data", {})
                members.extend(d.get("items", []))
                if not d.get("has_more"):
                    break
                page_token = d.get("page_token", "")
                if not page_token:
                    break
        return members

    async def _get_user_info(self, open_id: str) -> dict:
        token = await self._get_token()
        async with httpx.AsyncClient() as cli:
            r = await cli.get(
                f"{self.feishu_base}/open-apis/contact/v3/users/{open_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            data = r.json()
            return data.get("data", {}).get("user", {}) if data.get("code") == 0 else {}

    async def _upload_image(self, image_path: str) -> str:
        token = await self._get_token()
        fname = os.path.basename(image_path)
        with open(image_path, "rb") as f:
            async with httpx.AsyncClient() as cli:
                r = await cli.post(
                    f"{self.feishu_base}/open-apis/im/v1/images",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"image_type": "message"},
                    files={"image": (fname, f, "application/octet-stream")},
                )
                data = r.json()
                return data.get("data", {}).get("image_key", "") if data.get("code") == 0 else ""

    async def _upload_file(self, file_path: str) -> tuple[str, str]:
        token = await self._get_token()
        fname = os.path.basename(file_path)
        with open(file_path, "rb") as f:
            async with httpx.AsyncClient() as cli:
                r = await cli.post(
                    f"{self.feishu_base}/open-apis/im/v1/files",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"file_type": "stream", "file_name": fname},
                    files={"file": (fname, f, "application/octet-stream")},
                )
                data = r.json()
                if data.get("code") != 0:
                    return "", ""
                d = data.get("data", {})
                return d.get("file_key", ""), fname

    async def _add_reaction(self, message_id: str, emoji_type: str) -> str:
        token = await self._get_token()
        async with httpx.AsyncClient() as cli:
            r = await cli.post(
                f"{self.feishu_base}/open-apis/im/v1/messages/{message_id}/reactions",
                headers={"Authorization": f"Bearer {token}"},
                json={"reaction_type": {"emoji_type": emoji_type}},
            )
            data = r.json()
            if data.get("code") != 0:
                print(f"[{self.name}] Reaction error: {data}")
                return ""
            return data.get("data", {}).get("reaction_id", "")

    async def _remove_reaction(self, message_id: str, reaction_id: str):
        if not reaction_id:
            return
        token = await self._get_token()
        async with httpx.AsyncClient() as cli:
            r = await cli.delete(
                f"{self.feishu_base}/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            data = r.json()
            if data.get("code") != 0:
                print(f"[{self.name}] Remove reaction error: {data}")

    async def _get_mentions(self, message_id: str) -> set[str]:
        try:
            token = await self._get_token()
            async with httpx.AsyncClient() as cli:
                r = await cli.get(
                    f"{self.feishu_base}/open-apis/im/v1/messages/{message_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            data = r.json()
            if data.get("code") != 0:
                return set()
            items = data.get("data", {}).get("items", [])
            if not items:
                return set()
            ids = set()
            for m in items[0].get("mentions", []):
                if isinstance(m, str):
                    ids.add(m)
                elif isinstance(m, dict):
                    id_field = m.get("id")
                    if isinstance(id_field, dict):
                        oid = id_field.get("open_id", "")
                        if oid:
                            ids.add(oid)
                    elif isinstance(id_field, str):
                        ids.add(id_field)
                    if m.get("open_id"):
                        ids.add(m["open_id"])
            return ids
        except Exception:
            return set()

    def _resolve_sender(self, sender_id: str) -> str:
        if not sender_id:
            return ""
        for s in self._siblings:
            if s["open_id"] == sender_id:
                return s["display_name"]
        return ""

    def _replace_at_tags(self, text: str) -> str:
        text = text.replace("@所有人", '<at user_id="all">所有人</at>')
        for s in self._siblings:
            name = s["display_name"]
            at_tag = f'<at user_id="{s["open_id"]}">{name}</at>'
            text = text.replace(f"@{name}", at_tag)
        return text

    def _persist_session_ids(self):
        save_json(self._session_file, self._session_ids)

    def _resolve_session_id(self, session_key: str) -> tuple[str, bool]:
        """Return (session_id, is_resume). Uses persisted random UUID v4 so
        restarts don't collide with orphan Claude CLI processes."""
        stored = self._session_ids.get(session_key)
        if isinstance(stored, str) and stored:
            return stored, True
        # Corrupted or missing — generate a fresh random ID
        return str(uuid.uuid4()), False

    # -- Claude CLI --

    async def _ensure_daemon(self) -> ClaudeDaemon:
        """Ensure Claude daemon is running for this bot."""
        if self._daemon is None or not self._daemon.is_running:
            system_prompt = f"你的名字叫{self.display_name}。{self.system_prompt}"
            self._daemon = ClaudeDaemon(
                bot_name=self.name,
                cli_cfg=self.cli_cfg,
                system_prompt=system_prompt,
            )
            await self._daemon.start()
        return self._daemon

    def _build_claude_args(self, session_key: str, user_msg: str,
                           session_id: str) -> tuple[list[str], int, str, dict]:
        session_name = f"bridge-{self.name}-{session_key.replace(':', '-').replace('/', '-')}"
        system_prompt = f"你的名字叫{self.display_name}。{self.system_prompt}"
        timeout = self.cli_cfg.get("timeout_seconds", 300)

        perm_mode = self.cli_cfg.get("permission_mode", "default")
        base = ["claude", "-p", user_msg,
                "--name", session_name,
                "--output-format", "text"]
        # Map bridge config values to Claude CLI args
        if perm_mode == "bypass-permissions":
            base.append("--dangerously-skip-permissions")
        else:
            # Normalize kebab-case → camelCase for --permission-mode
            mode_map = {"accept-edits": "acceptEdits", "dont-ask": "dontAsk"}
            cli_mode = mode_map.get(perm_mode, perm_mode)
            base += ["--permission-mode", cli_mode]
        if self.cli_cfg.get("model"):
            base += ["--model", self.cli_cfg["model"]]
        for d in self.cli_cfg.get("add_dirs", []):
            base += ["--add-dir", d]
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        base += ["--add-dir", str(MEDIA_DIR)]
        if self.cli_cfg.get("max_turns"):
            base += ["--max-turns", str(self.cli_cfg["max_turns"])]
        if self.cli_cfg.get("max_budget_usd"):
            base += ["--max-budget-usd", str(self.cli_cfg["max_budget_usd"])]
        env = os.environ.copy()
        env["USERPROFILE"] = str(APP_DIR)
        env.update(self.cli_cfg.get("env", {}))

        return base, timeout, system_prompt, env

    def _call_claude_sync(self, session_key: str, user_msg: str,
                          session_id: str, is_resume: bool) -> str:
        base, timeout, system_prompt, env = \
            self._build_claude_args(session_key, user_msg, session_id)

        def run(args):
            try:
                p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                                   env=env, cwd=str(CLI_SESSIONS_DIR), timeout=timeout)
                return p.returncode, (p.stdout or ""), (p.stderr or "")
            except subprocess.TimeoutExpired:
                return -1, "", "timeout"
            except FileNotFoundError:
                return -1, "", "claude binary not found"
            except Exception as e:
                return -1, "", str(e)

        if is_resume:
            rc, stdout, stderr = run(base + ["--resume", session_id])
        else:
            rc, stdout, stderr = run(
                base + ["--session-id", session_id, "--append-system-prompt", system_prompt])

        if rc != 0:
            print(f"[{self.name}] CLI error (exit {rc}): {stderr[:300]}")
            return f"[错误] Claude CLI 异常 (退出码 {rc})"
        if not stdout:
            print(f"[{self.name}] CLI empty stdout, stderr={stderr[:200]}")
            return "[错误] Claude CLI 返回为空"
        return stdout.strip()

    async def _call_claude_async(self, session_key: str, user_msg: str,
                                 session_id: str, is_resume: bool) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, functools.partial(self._call_claude_sync, session_key, user_msg,
                                    session_id, is_resume)
        )

    @staticmethod
    async def _noop(_unused: str):
        pass

    async def _call_claude_stream(self, session_key: str, user_msg: str,
                                  on_chunk, session_id: str, is_resume: bool,
                                  *, chat_id: str = "",
                                  message_id: str = "") -> tuple[str, bool]:
        """Run Claude CLI, calling await on_chunk(accumulated_text) per line.
        Returns (output, ok). ok=False means the CLI exited non-zero or empty —
        caller should discard the session and retry with a fresh ID."""
        base, timeout, system_prompt, env = \
            self._build_claude_args(session_key, user_msg, session_id)

        if is_resume:
            args = base + ["--resume", session_id]
        else:
            args = base + ["--session-id", session_id,
                           "--append-system-prompt", system_prompt]

        need_approval = self.cli_cfg.get("permission_mode", "default") != "bypass-permissions"
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if need_approval else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env, cwd=str(CLI_SESSIONS_DIR),
        )

        async def _read_stderr():
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    print(f"[{self.name}] {text}")

        stderr_task = asyncio.create_task(_read_stderr())

        accumulated = ""
        approval_handled_at = 0
        try:
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                if not line:
                    break
                accumulated += line.decode("utf-8", errors="replace")

                # Detect permission prompt (only check new text)
                if need_approval and message_id:
                    new_text = accumulated[approval_handled_at:]
                    if self._is_permission_prompt(new_text):
                        lines = accumulated.strip().split("\n")
                        prompt_text = "\n".join(lines[-6:])
                        choice = await self._send_approval_card_and_wait(
                            chat_id, message_id,
                            "Claude 需要您的确认",
                            prompt_text[:2000],
                        )
                        resp = "y\n" if choice == "allow" else "n\n"
                        proc.stdin.write(resp.encode())
                        await proc.stdin.drain()
                        approval_handled_at = len(accumulated)
                        continue

                if accumulated.strip():
                    await on_chunk(accumulated)
        except asyncio.TimeoutError:
            pass
        finally:
            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass

        await proc.wait()
        ok = proc.returncode == 0 and bool(accumulated.strip())
        return accumulated.strip(), ok

    @staticmethod
    def _is_permission_prompt(text: str) -> bool:
        """Detect if text ends with a Claude Code permission prompt."""
        import re
        return bool(re.search(
            r'(?:Do you want to proceed|proceed\?|\(y/n\)|'
            r'是否(?:允许|继续|执行|确认)|需要.*(?:确认|权限|批准))',
            text, re.IGNORECASE,
        ))

    async def _stream_reply(self, chat_id: str, message_id: str,
                            session_key: str, user_msg: str,
                            session_id: str, is_resume: bool) -> tuple[str, bool, str]:
        """Send placeholder then stream-edit it with Claude's live output.
        Returns (final_text, ok, placeholder_id)."""
        throttle = self.cli_cfg.get("stream_throttle_ms", 500) / 1000.0

        # Send initial placeholder as a reply
        placeholder_id = await self._send_reply(message_id, "text",
            json.dumps({"text": "⏳"}, ensure_ascii=False))
        if not placeholder_id:
            print(f"[{self.name}] Stream placeholder failed, falling back")
            reply, ok = await self._call_claude_stream(
                session_key, user_msg, self._noop, session_id, is_resume,
                chat_id=chat_id, message_id=message_id,
            )
            reply = self._replace_at_tags(reply)
            if ok:
                await self._reply_text(message_id, reply)
            return reply, ok, ""

        last_edit = 0.0

        async def _on_chunk(accumulated: str):
            nonlocal last_edit
            now = time.time()
            if now - last_edit < throttle:
                return
            last_edit = now
            text = accumulated.strip()
            await self._edit_message(placeholder_id, "text",
                json.dumps({"text": text + " ✏️"}, ensure_ascii=False))

        final, ok = await self._call_claude_stream(session_key, user_msg, _on_chunk,
                                                   session_id, is_resume,
                                                   chat_id=chat_id,
                                                   message_id=message_id)
        final = self._replace_at_tags(final)

        # Final edit — detect formatting
        if ok:
            msg_type, card_content = self._parse_structured_reply(final)
            if msg_type and card_content:
                await self._edit_message(placeholder_id, msg_type, card_content)
            elif self._has_markdown_formatting(final):
                card = self._markdown_to_card(final)
                await self._edit_message(placeholder_id, "interactive",
                                          json.dumps(card, ensure_ascii=False))
            else:
                await self._edit_message(placeholder_id, "text",
                    json.dumps({"text": final}, ensure_ascii=False))
        else:
            # On failure, delete placeholder — caller will retry or report error
            await self._delete_message(placeholder_id)
        return final, ok, placeholder_id

    async def _send_approval_card_and_wait(self, chat_id: str, message_id: str,
                                            title: str, description: str,
                                            timeout: int = 300) -> str:
        """Send an approval card with Allow/Deny buttons and wait for click."""
        self._approval_counter += 1
        approval_id = f"{self.name}_{self._approval_counter}_{int(time.time())}"

        event = asyncio.Event()
        self._pending_approvals[approval_id] = event

        card = {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"content": title, "tag": "plain_text"}},
            "elements": [
                {"tag": "div", "text": {"content": description, "tag": "lark_md"}},
                {"tag": "action", "actions": [
                    {"tag": "button", "text": {"content": "允许", "tag": "plain_text"},
                     "type": "primary",
                     "value": json.dumps({"approval_id": approval_id, "choice": "allow"})},
                    {"tag": "button", "text": {"content": "拒绝", "tag": "plain_text"},
                     "type": "danger",
                     "value": json.dumps({"approval_id": approval_id, "choice": "deny"})},
                ]},
            ],
        }
        await self._send_reply(message_id, "interactive",
                               json.dumps(card, ensure_ascii=False))

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._approval_results[approval_id] = "timeout"

        self._pending_approvals.pop(approval_id, None)
        return self._approval_results.pop(approval_id, "timeout")

    async def _download_resource(self, message_id: str, file_key: str, file_type: str,
                                  save_dir: str) -> str | None:
        """Download media from Feishu, return local path or None."""
        token = await self._get_token()
        async with httpx.AsyncClient(timeout=30) as cli:
            r = await cli.get(
                f"{self.feishu_base}/open-apis/im/v1/messages/{message_id}"
                f"/resources/{file_key}",
                headers={"Authorization": f"Bearer {token}"},
                params={"type": file_type},
            )
            if r.status_code != 200:
                print(f"[{self.name}] Download {file_type} failed: {r.status_code}")
                return None
            content_type = r.headers.get("content-type", "")
            ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
                   "image/webp": ".webp", "application/pdf": ".pdf",
                   "text/plain": ".txt", "application/octet-stream": ".bin"}
            suffix = ext.get(content_type.split(";")[0].strip(), "")
            fname = f"{file_key}{suffix}"
            path = os.path.join(save_dir, fname)
            with open(path, "wb") as f:
                f.write(r.content)
            print(f"[{self.name}] Downloaded {file_type}: {fname} ({len(r.content)} bytes)")
            return path

    @staticmethod
    def _extract_media_keys(event: dict) -> list[tuple[str, str]]:
        """Parse message content for image/file/media keys. Returns [(type, key), ...]."""
        content_str = event.get("content", "")
        try:
            content = json.loads(content_str) if content_str else {}
        except json.JSONDecodeError:
            return []
        keys = []
        if "image_key" in content:
            keys.append(("image", content["image_key"]))
        if "file_key" in content:
            keys.append(("file", content["file_key"]))
        if "image_keys" in content:
            for k in content["image_keys"]:
                keys.append(("image", k))
        return keys

    async def _build_media_message(self, message_id: str, event: dict,
                                    msg_type: str) -> str | None:
        """Download attached media and return a prompt referencing local files."""
        keys = self._extract_media_keys(event)
        if not keys:
            # Try content text fallback (e.g. image with caption)
            content = event.get("content", "").strip()
            if content and not content.startswith("{"):
                return content
            return None

        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        paths = []
        for ftype, fkey in keys:
            path = await self._download_resource(message_id, fkey, ftype, str(MEDIA_DIR))
            if path:
                paths.append((ftype, path))

        if not paths:
            return None

        labels = []
        for ftype, path in paths:
            labels.append(f"  [{ftype}] {path}")
        media_lines = "\n".join(labels)

        text_hint = ""
        content = event.get("content", "").strip()
        if content and not content.startswith("{"):
            text_hint = f"\n附言: {content}"

        return f"用户发送了以下文件，你可以用 Read 工具查看:\n{media_lines}{text_hint}"

    async def _handle_event(self, event: dict):
        if not isinstance(event, dict):
            return
        msg_type = event.get("message_type", "")

        chat_id = event.get("chat_id", "")
        chat_type = event.get("chat_type", "p2p")
        message_id = event.get("message_id", "")
        sender_id = event.get("sender_id", "")
        if not sender_id:
            sender = event.get("sender", {})
            if isinstance(sender, dict):
                sid = sender.get("sender_id", {})
                if isinstance(sid, dict):
                    sender_id = sid.get("open_id", "")
                elif isinstance(sid, str):
                    sender_id = sid
        content = event.get("content", "").strip()

        if not message_id:
            return
        if message_id in self.last_msg_ids or message_id in self._processing:
            return
        self._processing.add(message_id)

        try:
            await self._handle_event_inner(message_id, chat_id, chat_type,
                                            sender_id, content, msg_type, event)
        finally:
            self._processing.discard(message_id)
            # Persist after processing completes — guarantees no replay on crash
            self.last_msg_ids[message_id] = time.time()
            if len(self.last_msg_ids) > 500:
                cutoff = time.time() - 3600
                self.last_msg_ids = {k: v for k, v in self.last_msg_ids.items() if v > cutoff}
            save_json(self.msg_file, self.last_msg_ids)

    async def _rotate_session(self, session_key: str, session_id: str):
        """Clean old session files from a previous bridge run if needed.
        Only compacts when max_context_messages is set and the session is active."""
        encoded = str(CLI_SESSIONS_DIR).replace(":", "").replace("\\", "-").replace("/", "-")
        session_file = APP_DIR / ".claude" / "projects" / encoded / f"{session_id}.jsonl"
        if not session_file.exists():
            return

        try:
            with open(session_file, "r", encoding="utf-8") as f:
                lines = sum(1 for _ in f)
        except Exception:
            lines = 0

        # If this session_id is not the one we have stored (stale from previous
        # run or a failed resume), delete it unconditionally.
        stored_id = self._session_ids.get(session_key)
        if stored_id != session_id:
            self._delete_session_files(session_file.parent, session_id)
            if lines > 0:
                print(f"[{self.name}] Cleaned old session ({lines} lines)")
            return

        # Active session: compact if it exceeds threshold
        if not self.max_context or lines < self.max_context:
            return

        print(f"[{self.name}] Compacting session ({lines} lines)...")
        prompt = (
            "请用一段话总结以上所有对话的关键信息，包括：已做出的决定、进行中的任务、"
            "重要上下文。不要遗漏任何可能影响后续对话的信息。只输出总结本身。"
        )
        base, timeout, system_prompt, env = \
            self._build_claude_args(session_key, prompt, session_id)
        args = base + ["--resume", session_id]
        try:
            p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                               env=env, cwd=str(CLI_SESSIONS_DIR), timeout=timeout)
            summary = (p.stdout or "").strip()
            if summary and not summary.startswith("[错误]"):
                prev = self._session_summaries.get(session_key, "")
                self._session_summaries[session_key] = (
                    prev + "\n\n" + summary).strip() if prev else summary
                print(f"[{self.name}] Summary saved: {len(summary)} chars")
        except Exception as e:
            print(f"[{self.name}] Summary failed: {e}")

        self._delete_session_files(session_file.parent, session_id)
        self._session_ids.pop(session_key, None)
        self._persist_session_ids()
        print(f"[{self.name}] Session compacted")

    @staticmethod
    def _delete_session_files(parent: Path, session_id: str):
        """Delete session file and its subdirectories (tool-results, subagents)."""
        for p in parent.glob(session_id + "*"):
            try:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
            except OSError:
                pass

    async def _handle_event_inner(self, message_id, chat_id, chat_type, sender_id,
                                   content, msg_type="text", event=None):
        session_key = f"{chat_type}:{chat_id}"

        # --- Build user message (no session access needed) ---
        if msg_type == "text":
            if not content:
                return
            user_msg = content
        elif msg_type in ("image", "file", "media", "audio"):
            user_msg = await self._build_media_message(message_id, event or {}, msg_type)
            if not user_msg:
                return
        else:
            return

        sender_name = self._resolve_sender(sender_id)
        if sender_name:
            user_msg = f"[发送者: {sender_name}] {user_msg}"

        summary = self._session_summaries.pop(session_key, "")
        if summary:
            user_msg = f"[历史对话摘要]\n{summary}\n\n---\n[当前消息]\n{user_msg}"

        print(f"[{self.name}] {msg_type} {chat_type}:{chat_id[:12]}... | {user_msg[:80]}")

        if chat_type == "group" and self._bot_open_id:
            mentioned_ids = await self._get_mentions(message_id)
            is_at_all = "all" in mentioned_ids or "@_all" in content or "@所有人" in content
            if self._bot_open_id not in mentioned_ids and not is_at_all:
                return

        # --- Session operations — serialized per session_key ---
        lock = self._session_locks.setdefault(session_key, asyncio.Lock())
        async with lock:
            session_id, is_resume = self._resolve_session_id(session_key)
            await self._rotate_session(session_key, session_id)

            reaction = self.cli_cfg.get("reaction_emoji", "Typing")
            reaction_id = await self._add_reaction(message_id, reaction)

            try:
                placeholder_id = ""
                if self.cli_cfg.get("stream_edit", False):
                    reply, ok, placeholder_id = await self._stream_reply(
                        chat_id, message_id, session_key, user_msg,
                        session_id, is_resume,
                    )
                else:
                    reply, ok = await self._call_claude_stream(
                        session_key, user_msg,
                        self._noop, session_id, is_resume,
                        chat_id=chat_id, message_id=message_id,
                    )
                    reply = self._replace_at_tags(reply)
                    if ok:
                        await self._reply_text(message_id, reply)

                # If resume failed (orphan process held the lock), retry
                # with a fresh random session ID.
                if not ok and is_resume:
                    print(f"[{self.name}] Resume failed, retrying with fresh session")
                    # Clean up failed stream placeholder if still present
                    if placeholder_id:
                        await self._delete_message(placeholder_id)
                    del self._session_ids[session_key]
                    session_id = str(uuid.uuid4())
                    is_resume = False
                    await self._rotate_session(session_key, session_id)

                    if self.cli_cfg.get("stream_edit", False):
                        reply, ok, _ = await self._stream_reply(
                            chat_id, message_id, session_key, user_msg,
                            session_id, is_resume,
                        )
                    else:
                        reply, ok = await self._call_claude_stream(
                            session_key, user_msg,
                            self._noop, session_id, is_resume,
                            chat_id=chat_id, message_id=message_id,
                        )
                        reply = self._replace_at_tags(reply)
                        if ok:
                            await self._reply_text(message_id, reply)

                if ok and not is_resume:
                    self._session_ids[session_key] = session_id
                    self._persist_session_ids()
            finally:
                await self._remove_reaction(message_id, reaction_id)

        print(f"[{self.name}] REPLY: {message_id}")

    # -- Event stream (lark-cli subprocess) --

    def _handle_card_action(self, data) -> object:
        """Handle card.action.trigger via lark_oapi SDK WebSocket."""
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTrigger, P2CardActionTriggerResponse,
            CallBackCard, CallBackToast,
        )
        event = data.event if hasattr(data, 'event') else None
        action = getattr(event, 'action', None) if event else None
        value = getattr(action, 'value', {}) if action else {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = {}

        print(f"[{self.name}] Card action: {value!r}")

        # Handle approval buttons (from _send_approval_card_and_wait)
        approval_id = value.get("approval_id", "")
        choice = value.get("choice", "")
        if approval_id and approval_id in self._pending_approvals:
            self._approval_results[approval_id] = choice
            self._pending_approvals[approval_id].set()
            print(f"[{self.name}] Approval: {approval_id} -> {choice}")
            label = "已允许" if choice == "allow" else "已拒绝"
            color = "green" if choice == "allow" else "red"
            resp = P2CardActionTriggerResponse()
            resp.card = CallBackCard()
            resp.card.type = "raw"
            resp.card.data = {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"content": f"Claude 权限请求 — {label}", "tag": "plain_text"},
                    "template": color,
                },
                "elements": [
                    {"tag": "div", "text": {"content": f"用户选择：**{label}**", "tag": "lark_md"}},
                ],
            }
            return resp

        # Handle generic card buttons — update card + dispatch to Claude
        action_name = value.get("action", "") or value.get("choice", "") or "clicked"
        is_positive = action_name in ("test_pass", "pass", "allow", "approve", "yes", "ok",
                                       "accept", "confirm", "agree")
        label = "已通过" if is_positive else "未通过"
        color = "green" if is_positive else "red"

        # Update the card
        resp = P2CardActionTriggerResponse()
        resp.card = CallBackCard()
        resp.card.type = "raw"
        resp.card.data = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"content": f"测试结果 — {label}", "tag": "plain_text"},
                "template": color,
            },
            "elements": [
                {"tag": "div", "text": {"content": f"用户操作：**{action_name}** → {label}", "tag": "lark_md"}},
            ],
        }

        # Dispatch card action to Claude (p2p skips mention check)
        ctx = getattr(event, 'context', None)
        chat_id = getattr(ctx, 'open_chat_id', '') if ctx else ''
        message_id = getattr(ctx, 'open_message_id', '') if ctx else ''
        if chat_id and hasattr(self, '_main_loop'):
            asyncio.run_coroutine_threadsafe(
                self._handle_event_inner(
                    message_id or f"card-{int(time.time())}",
                    chat_id, "p2p", "",
                    f"[卡片交互] 用户点击了按钮: {action_name} ({label})",
                ),
                self._main_loop,
            )

        return resp

    @staticmethod
    def _start_all_feishu_ws(bots: list, main_loop: asyncio.AbstractEventLoop):
        """Run all lark_oapi SDK clients on one dedicated event loop/thread.
        Bypasses client.start() to avoid its blocking _select() call."""
        import threading
        import lark_oapi as lark
        import lark_oapi.ws.client as ws_mod
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
        from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

        shared_loop = asyncio.new_event_loop()
        ready = threading.Event()

        # Store main_loop on each bot so handlers can dispatch async work
        for bot in bots:
            bot._main_loop = main_loop

        async def _connect_all():
            for bot in bots:
                def _make_on_message(b):
                    def _on_message(data: P2ImMessageReceiveV1):
                        ev = data.event
                        msg = ev.message
                        sender = ev.sender
                        sender_id = ""
                        if sender and sender.sender_id:
                            sender_id = sender.sender_id.open_id or ""
                        event_dict = {
                            "message_id": msg.message_id or "",
                            "chat_id": msg.chat_id or "",
                            "chat_type": msg.chat_type or "p2p",
                            "message_type": msg.message_type or "text",
                            "content": msg.content or "",
                            "sender_id": sender_id,
                        }
                        asyncio.run_coroutine_threadsafe(
                            b._handle_event(event_dict), main_loop)
                        return None
                    return _on_message

                def _make_on_card(b):
                    def _on_card_action(data: P2CardActionTrigger):
                        return b._handle_card_action(data)
                    return _on_card_action

                handler = lark.EventDispatcherHandler.builder("", "") \
                    .register_p2_im_message_receive_v1(_make_on_message(bot)) \
                    .register_p2_card_action_trigger(_make_on_card(bot)) \
                    .register_p2_im_message_reaction_created_v1(lambda _: None) \
                    .register_p2_im_message_reaction_deleted_v1(lambda _: None) \
                    .build()

                client = lark.ws.Client(
                    app_id=bot.feishu_app_id,
                    app_secret=bot.feishu_app_secret,
                    event_handler=handler,
                    log_level=lark.LogLevel.WARNING,
                    auto_reconnect=True,
                )
                print(f"[{bot.name}] Feishu WS connecting...")
                try:
                    await client._connect()
                    shared_loop.create_task(client._ping_loop())
                    print(f"[{bot.name}] Feishu WS connected")
                except Exception as e:
                    print(f"[{bot.name}] Feishu WS error: {e}")

        def _run():
            asyncio.set_event_loop(shared_loop)
            ws_mod.loop = shared_loop
            shared_loop.run_until_complete(_connect_all())
            ready.set()
            shared_loop.run_forever()

        def _run():
            asyncio.set_event_loop(shared_loop)
            ws_mod.loop = shared_loop
            shared_loop.run_until_complete(_connect_all())
            ready.set()
            shared_loop.run_forever()

        t = threading.Thread(target=_run, name="feishu-ws", daemon=True)
        t.start()
        ready.wait(timeout=30)
        # Return immediately — daemon thread keeps running in background.
        # Bridge.run() uses shutdown_event to stay alive.


# ===========================================================================
# Bridge
# ===========================================================================

class Bridge:
    def __init__(self, gui_server=None):
        cfg = load_config()
        self.bots: list[BotRunner] = []
        self.gui_server = gui_server

        _sync_profiles(cfg["bots"])

        for name, bot_cfg in cfg["bots"].items():
            self.bots.append(BotRunner(name, bot_cfg))

    async def run(self):
        print("=" * 55)
        print(f"Claude <-> Feishu Bridge — {len(self.bots)} bot(s)")
        for bot in self.bots:
            print(f"  [{bot.name}] app={bot.feishu_app_id[:14]}...")
        print("=" * 55)

        all_ids: set[str] = set()
        for bot in self.bots:
            try:
                oid = await bot._get_bot_open_id()
                bot._bot_open_id = oid
                all_ids.add(oid)
                print(f"  [{bot.name}] open_id={oid}")
            except Exception as e:
                print(f"  [{bot.name}] WARN: cannot resolve identity: {e}")
        for bot in self.bots:
            bot._all_bot_ids = all_ids
        for bot in self.bots:
            bot._siblings = [
                {"display_name": s.display_name, "open_id": s._bot_open_id}
                for s in self.bots
                if s.name != bot.name and s._bot_open_id
            ]

        tasks = []
        if self.gui_server:
            tasks.append(asyncio.create_task(self.gui_server.serve()))
        # Start Feishu WS (returns immediately, daemon thread keeps running)
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, BotRunner._start_all_feishu_ws, self.bots, loop)

        # Block until cancelled (Ctrl+C raises KeyboardInterrupt in main)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            print("[Bridge] All bots stopped.")


def _cleanup_stale_bridge_sessions():
    """Kill orphan bridge-related Claude CLI processes from previous runs.

    Only targets processes with ``--name bridge-`` in their command line —
    normal Claude CLI sessions are left alone. Session files are UUID-named
    (random v4), so stale files from old runs don't cause collisions; we just
    need to clean up any orphan processes that might hold locks.
    """
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"name='claude.exe'\" | "
                 "Where-Object { $_.CommandLine -like '*--name bridge-*' } | "
                 "ForEach-Object { $_.ProcessId }"],
                capture_output=True, text=True, timeout=10,
            )
            pids = [p.strip() for p in r.stdout.splitlines() if p.strip().isdigit()]
            if pids:
                pid_args = []
                for p in pids:
                    pid_args.extend(["/PID", p])
                subprocess.run(
                    ["taskkill", "/F"] + pid_args,
                    capture_output=True, timeout=10,
                )
                print(f"[Bridge] Cleaned {len(pids)} orphan bridge Claude process(es)")
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="Claude <-> Feishu Bridge")
    parser.add_argument("--gui", action="store_true", help="Start web dashboard")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        print(f"Config not found: {CONFIG_PATH}")
        sys.exit(1)

    if not shutil.which(LARK_CLI):
        print(f"ERROR: {LARK_CLI} not found in PATH. Install: npm install -g @larksuite/cli")
        sys.exit(1)

    CLI_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # Clean stale bridge session files (not normal Claude sessions)
    _cleanup_stale_bridge_sessions()
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    gui_server = None
    if args.gui:
        from importlib.machinery import SourceFileLoader
        import uvicorn

        gui_path = str(APP_DIR / "bridge-gui.py")
        gui_module = SourceFileLoader("bridge_gui", gui_path).load_module()
        gui_app = gui_module.create_app(embedded=True)
        gui_module.install_log_tee()

        config = uvicorn.Config(gui_app, host=args.host, port=args.port, log_level="warning")
        gui_server = uvicorn.Server(config)
        print(f"[Bridge] GUI dashboard: http://{args.host}:{args.port}")

    bridge = Bridge(gui_server=gui_server)
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        print("\n[Bridge] Goodbye.")


if __name__ == "__main__":
    main()
