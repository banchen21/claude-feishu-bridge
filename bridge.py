"""
Claude <-> Feishu Bridge — Multi-Bot Edition
Each bot runs independently via @larksuite/cli event daemon + Claude Code CLI.
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
from pathlib import Path

if sys.platform == "win32":
    import io
    _orig_stdout = sys.stdout
    sys.stdout = io.TextIOWrapper(_orig_stdout.buffer, encoding='utf-8', line_buffering=True)

import httpx

if getattr(sys, 'frozen', False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent

CONFIG_PATH = APP_DIR / "bridge-config.json"
DATA_DIR = APP_DIR
CLI_SESSIONS_DIR = DATA_DIR / ".cli-sessions"  # dedicated cwd for Claude CLI subprocess

# Resolve lark-cli binary (handle Windows .cmd extension)
_LARK_CLI = shutil.which("lark-cli")
if not _LARK_CLI:
    _LARK_CLI = shutil.which("lark-cli.cmd")
if not _LARK_CLI:
    _npm_root = os.path.expandvars(r"%APPDATA%\npm")
    for _candidate in ["lark-cli.cmd", "lark-cli"]:
        _p = os.path.join(_npm_root, _candidate)
        if os.path.exists(_p):
            _LARK_CLI = _p
            break
if not _LARK_CLI:
    _LARK_CLI = "lark-cli"
LARK_CLI = _LARK_CLI


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


def resolve_claude(bot_name: str, bot_cfg: dict, all_cfg: dict) -> dict:
    """Extract the claude block. system_prompt is required."""
    cc = bot_cfg.get("claude") or {}
    if not cc.get("system_prompt"):
        raise RuntimeError(f"Bot '{bot_name}' has no claude.system_prompt configured")
    return cc


def load_config():
    """Load config, normalising old flat format to new bots dict. Resolves Claude inheritance."""
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # Backward compat: flat format -> single "default" bot
    if "bots" not in cfg:
        cfg = {
            "bots": {
                "default": {
                    "feishu_profile": "bridge",
                    "feishu_app_id": cfg["feishu"]["app_id"],
                    "feishu_app_secret": cfg["feishu"]["app_secret"],
                    "claude": cfg["claude"],
                    "max_context_messages": cfg.get("bridge", {}).get("max_context_messages", 20),
                }
            }
        }

    # Resolve Claude config for bots that don't specify one
    for name, bot_cfg in cfg["bots"].items():
        bot_cfg["claude"] = resolve_claude(name, bot_cfg, cfg)

    return cfg


# ===========================================================================
# BotRunner — one per bot, fully independent
# ===========================================================================

class BotRunner:
    def __init__(self, name: str, config: dict):
        self.name = name
        cc = config["claude"]
        self.cli_cfg = config.get("claude_cli", {})
        self.system_prompt = cc["system_prompt"]
        self.display_name = config.get("display_name", name)

        self.feishu_profile = config["feishu_profile"]
        self.feishu_app_id = config["feishu_app_id"]
        self.feishu_app_secret = config["feishu_app_secret"]
        self.feishu_base = "https://open.feishu.cn"

        self.msg_file = DATA_DIR / f"bridge-last-msg-{name}.json"
        self._init_sessions: set[str] = set()
        self._processing: set[str] = set()
        self.last_msg_ids: dict[str, float] = load_json(self.msg_file, {})
        self._tenant_token: str | None = None
        self._token_expire: float = 0
        self._bot_open_id: str | None = None
        self._all_bot_ids: set[str] = set()
        self._siblings: list[dict] = []

    # -- Feishu REST --

    async def _get_token(self) -> str:
        now = time.time()
        if self._tenant_token and now < self._token_expire:
            return self._tenant_token
        async with httpx.AsyncClient() as cli:
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

    async def _reply_text(self, message_id: str, text: str):
        token = await self._get_token()
        content = json.dumps({"text": text}, ensure_ascii=False)
        async with httpx.AsyncClient() as cli:
            r = await cli.post(
                f"{self.feishu_base}/open-apis/im/v1/messages/{message_id}/reply",
                headers={"Authorization": f"Bearer {token}"},
                json={"content": content, "msg_type": "text"},
            )
            data = r.json()
            if data.get("code") != 0:
                print(f"[{self.name}] Reply error: {data}")

    async def _add_reaction(self, message_id: str, emoji_type: str) -> str:
        """Add an emoji reaction. Returns reaction_id (empty on failure)."""
        token = await self._get_token()
        async with httpx.AsyncClient() as cli:
            r = await cli.post(
                f"{self.feishu_base}/open-apis/im/v1/messages/{message_id}/reactions",
                headers={"Authorization": f"Bearer {token}"},
                json={"reaction_type": {"emoji_type": emoji_type}},
            )
            data = r.json()
            if data.get("code") != 0:
                print(f"[{self.name}] Reaction error ({emoji_type}): {data}")
                return ""
            reaction_id = data.get("data", {}).get("reaction_id", "")
            print(f"[{self.name}] Reaction added: {emoji_type} id={reaction_id[:8]}...")
            return reaction_id

    async def _remove_reaction(self, message_id: str, reaction_id: str):
        """Remove a previously added reaction."""
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
        """Return the set of open_ids @mentioned in a message."""
        try:
            token = await self._get_token()
            async with httpx.AsyncClient() as cli:
                r = await cli.get(
                    f"{self.feishu_base}/open-apis/im/v1/messages/{message_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            data = r.json()
            if data.get("code") != 0:
                print(f"[{self.name}] mentions API error: code={data.get('code')} msg={data.get('msg')}")
                return set()
            items = data.get("data", {}).get("items", [])
            if not items:
                return set()
            mentions_raw = items[0].get("mentions", [])
            ids = set()
            for m in mentions_raw:
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
                    direct_oid = m.get("open_id", "")
                    if direct_oid:
                        ids.add(direct_oid)
            return ids
        except Exception as e:
            print(f"[{self.name}] mentions API failed: {e}")
            return set()

    def _resolve_sender(self, sender_id: str) -> str:
        """Resolve sender_id to a display name. Returns empty string for unknown users."""
        if not sender_id:
            return ""
        for s in self._siblings:
            if s["open_id"] == sender_id:
                return s["display_name"]
        return ""

    def _replace_at_tags(self, text: str) -> str:
        """Replace @display_name mentions with proper Feishu <at> tags."""
        for s in self._siblings:
            name = s["display_name"]
            at_tag = f'<at user_id="{s["open_id"]}">{name}</at>'
            text = text.replace(f"@{name}", at_tag)
        return text

    # -- Claude CLI --

    def _call_claude_sync(self, session_key: str, user_msg: str) -> str:
        """Call Claude CLI. Creates the session on first use, resumes thereafter."""
        session_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"bridge.{self.name}.{session_key}"))
        session_name = f"bridge-{self.name}-{session_key.replace(':', '-').replace('/', '-')}"
        system_prompt = f"你的名字叫{self.display_name}。{self.system_prompt}"
        timeout = self.cli_cfg.get("timeout_seconds", 120)

        base = ["claude", "-p", user_msg,
                "--name", session_name,
                "--permission-mode", self.cli_cfg.get("permission_mode", "auto"),
                "--output-format", "text"]
        if self.cli_cfg.get("model"):
            base += ["--model", self.cli_cfg["model"]]
        if self.cli_cfg.get("allowed_tools"):
            base += ["--allowedTools", ",".join(self.cli_cfg["allowed_tools"])]
        for d in self.cli_cfg.get("add_dirs", []):
            base += ["--add-dir", d]
        if self.cli_cfg.get("max_turns"):
            base += ["--max-turns", str(self.cli_cfg["max_turns"])]
        if self.cli_cfg.get("max_budget_usd"):
            base += ["--max-budget-usd", str(self.cli_cfg["max_budget_usd"])]
        env = os.environ.copy()
        env.update(self.cli_cfg.get("env", {}))

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

        if session_key in self._init_sessions:
            rc, stdout, stderr = run(base + ["--resume", session_id])
        else:
            rc, stdout, stderr = run(base + ["--resume", session_id])
            if rc != 0:
                rc, stdout, stderr = run(
                    base + ["--session-id", session_id, "--append-system-prompt", system_prompt])
            self._init_sessions.add(session_key)

        if rc != 0:
            print(f"[{self.name}] CLI error (exit {rc}): {stderr[:300]}")
            return f"[错误] Claude CLI 异常 (退出码 {rc})"
        if not stdout:
            print(f"[{self.name}] CLI empty stdout, stderr={stderr[:200]}")
            return "[错误] Claude CLI 返回为空"
        text = stdout.strip()
        return text

    async def _call_claude_async(self, session_key: str, user_msg: str) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, functools.partial(self._call_claude_sync, session_key, user_msg)
        )

    # -- Event handler --

    async def _handle_event(self, event: dict):
        if not isinstance(event, dict):
            return
        msg_type = event.get("message_type", "")
        if msg_type != "text":
            return

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

        if not content or not message_id:
            return

        # Dedup
        if message_id in self.last_msg_ids or message_id in self._processing:
            return
        self._processing.add(message_id)
        self.last_msg_ids[message_id] = time.time()
        save_json(self.msg_file, self.last_msg_ids)

        if len(self.last_msg_ids) > 1000:
            sorted_ids = sorted(self.last_msg_ids.items(), key=lambda x: x[1], reverse=True)
            self.last_msg_ids = dict(sorted_ids[:500])

        try:
            await self._handle_event_inner(message_id, chat_id, chat_type, sender_id, content)
        finally:
            self._processing.discard(message_id)

    async def _handle_event_inner(self, message_id, chat_id, chat_type, sender_id, content):
        sender_name = self._resolve_sender(sender_id)
        enriched = f"[发送者: {sender_name}] {content}" if sender_name else content

        print(f"[{self.name}] {chat_type}:{chat_id[:12]}... | {enriched[:80]}")

        session_key = f"{chat_type}:{chat_id}"

        # Group messages: only reply if @mentioned
        if chat_type == "group" and self._bot_open_id:
            mentioned_ids = await self._get_mentions(message_id)
            if self._bot_open_id not in mentioned_ids:
                return

        reaction = self.cli_cfg.get("reaction_emoji", "Typing")
        reaction_id = await self._add_reaction(message_id, reaction)
        reply = await self._call_claude_async(session_key, content)
        reply = self._replace_at_tags(reply)

        for i in range(0, len(reply), 7000):
            await self._reply_text(message_id, reply[i:i + 7000])
        await self._remove_reaction(message_id, reaction_id)

        print(f"[{self.name}] REPLY: {reply[:80]}...")

    # -- Event stream (lark-cli subprocess) --

    async def run(self):
        """Connect to Feishu via lark-cli and process events. Reconnects on failure."""
        print(f"[{self.name}] Starting event stream, profile={self.feishu_profile}")

        while True:
            proc = await asyncio.create_subprocess_exec(
                LARK_CLI, "event", "consume", "im.message.receive_v1",
                "--profile", self.feishu_profile,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            print(f"[{self.name}] CLI consumer started (pid={proc.pid})")

            async def read_stderr():
                while True:
                    line = await proc.stderr.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if text:
                        print(f"[{self.name}] {text}")

            stderr_task = asyncio.create_task(read_stderr())

            try:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    try:
                        event = json.loads(line.decode("utf-8"))
                        await self._handle_event(event)
                    except json.JSONDecodeError:
                        pass
                    except Exception as e:
                        print(f"[{self.name}] Handler error: {e}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[{self.name}] Stream error: {e}")
            finally:
                stderr_task.cancel()
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                print(f"[{self.name}] CLI consumer stopped")

            await asyncio.sleep(5)


# ===========================================================================
# Profile auto-registration
# ===========================================================================

def _get_profiles() -> dict[str, bool]:
    """Return dict of {profile_name: is_active} from lark-cli."""
    try:
        r = subprocess.run(
            [LARK_CLI, "profile", "list"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return {}
        profiles = json.loads(r.stdout)
        return {p["name"]: p.get("active", False) for p in profiles}
    except Exception:
        return {}


def _sync_profiles(bots_cfg: dict):
    """Ensure every bot's feishu_profile exists in lark-cli. Auto-create if missing."""
    existing = _get_profiles()

    for name, bot_cfg in bots_cfg.items():
        profile = bot_cfg.get("feishu_profile", "")
        app_id = bot_cfg.get("feishu_app_id", "")
        app_secret = bot_cfg.get("feishu_app_secret", "")
        if not profile or not app_id or not app_secret:
            continue
        if profile in existing:
            print(f"  [OK] profile '{profile}' exists")
            continue

        print(f"  [{name}] Registering profile '{profile}' (app: {app_id[:14]}...) ...")
        r = subprocess.run(
            [LARK_CLI, "profile", "add", "--name", profile, "--app-id", app_id,
             "--brand", "feishu", "--use", "--app-secret-stdin"],
            input=app_secret, capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            print(f"  [{name}] WARN: profile register failed: {r.stderr or r.stdout}")
        else:
            print(f"  [{name}] profile '{profile}' registered")


# ===========================================================================
# Bridge — orchestrates all bots
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
            model_info = bot.cli_cfg.get("model", "default")
            print(f"  [{bot.name}] profile={bot.feishu_profile} model={model_info}")
        print("=" * 55)

        # Resolve all bot identities
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
        tasks += [asyncio.create_task(bot.run()) for bot in self.bots]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception():
                    print(f"[Bridge] Bot task crashed: {t.exception()}")
        except asyncio.CancelledError:
            pass
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            print("[Bridge] All bots stopped.")


def main():
    parser = argparse.ArgumentParser(description="Claude <-> Feishu Bridge")
    parser.add_argument("--gui", action="store_true", help="Start web dashboard alongside the bridge")
    parser.add_argument("--port", type=int, default=8080, help="GUI port (default: 8080)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="GUI bind address (default: 127.0.0.1)")
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        print(f"Config not found: {CONFIG_PATH}")
        sys.exit(1)

    if not shutil.which(LARK_CLI):
        print(f"ERROR: {LARK_CLI} not found in PATH.")
        print("Install: npm install -g @larksuite/cli")
        sys.exit(1)

    CLI_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

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
