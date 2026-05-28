"""
Claude <-> Feishu Bridge — Multi-Bot Edition
Each bot runs independently via @larksuite/cli event daemon + its own Claude backend.
"""
import json
import os
import shutil
import sys
import time
import asyncio
import functools
from pathlib import Path

if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True)

import httpx
from anthropic import Anthropic

CONFIG_PATH = Path(__file__).parent / "bridge-config.json"
DATA_DIR = Path(__file__).parent

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
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def resolve_claude(bot_name: str, bot_cfg: dict, all_cfg: dict) -> dict:
    """Resolve Claude config for a bot, falling back to env vars then sibling bots.
    If a bot has a partial claude block, it's deep-merged with the fallback base."""
    own = bot_cfg.get("claude") or {}

    # Find fallback base
    base = {}
    env_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
    env_url = os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("CLAUDE_BASE_URL")
    if env_key and env_url:
        base = {
            "api_key": env_key,
            "base_url": env_url,
            "models": {"default": "mimo-v2.5", "thinking": "mimo-v2.5-pro"},
            "max_tokens": 8192,
            "system_prompt": "你是一个通过飞书与用户交流的AI助手。回答简洁清晰。",
        }
    else:
        for name, cfg in all_cfg.get("bots", {}).items():
            if name != bot_name and "claude" in cfg and cfg["claude"]:
                base = cfg["claude"]
                break

    if not own and not base:
        raise RuntimeError(f"Bot '{bot_name}' has no Claude config and no env vars or sibling to inherit from")

    # Deep merge: own + base (own wins)
    merged = dict(base)
    merged.update(own)
    if "models" in own and "models" in base:
        merged["models"] = {**base["models"], **own["models"]}
    return merged


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


# ═══════════════════════════════════════════════════════════════
# BotRunner — one per bot, fully independent
# ═══════════════════════════════════════════════════════════════

class BotRunner:
    def __init__(self, name: str, config: dict):
        self.name = name
        cc = config["claude"]

        self.claude = Anthropic(api_key=cc["api_key"], base_url=cc["base_url"])
        self.models = cc["models"]
        self.max_tokens = cc["max_tokens"]
        self.system_prompt = cc["system_prompt"]
        self.display_name = config.get("display_name", name)

        self.feishu_profile = config["feishu_profile"]
        self.feishu_app_id = config["feishu_app_id"]
        self.feishu_app_secret = config["feishu_app_secret"]
        self.feishu_base = "https://open.feishu.cn"

        self.max_context = config.get("max_context_messages", 20)

        self.session_file = DATA_DIR / f"bridge-sessions-{name}.json"
        self.msg_file = DATA_DIR / f"bridge-last-msg-{name}.json"

        self.sessions = load_json(self.session_file, {})
        self.last_msg_ids: dict[str, float] = load_json(self.msg_file, {})
        self._tenant_token: str | None = None
        self._token_expire: float = 0
        self._bot_open_id: str | None = None
        self._all_bot_ids: set[str] = set()  # set by Bridge after all bots resolve identity
        self._siblings: list[dict] = []  # other bots: [{display_name, open_id}]

    # ── Feishu REST ──

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
                    # also try direct open_id on mention object
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
        return ""  # unknown user — don't label

    def _replace_at_tags(self, text: str) -> str:
        """Replace @display_name mentions with proper Feishu <at> tags."""
        for s in self._siblings:
            name = s["display_name"]
            at_tag = f'<at user_id="{s["open_id"]}">{name}</at>'
            text = text.replace(f"@{name}", at_tag)
        return text

    # ── Claude ──

    def _call_claude_sync(self, session_key: str, user_msg: str) -> str:
        if session_key not in self.sessions:
            self.sessions[session_key] = []
        history = self.sessions[session_key]
        history.append({"role": "user", "content": user_msg})
        if len(history) > self.max_context * 2:
            history = history[-self.max_context * 2:]

        messages = [{"role": m["role"], "content": m["content"]} for m in history]

        think_kw = ["thinking", "分析", "思考", "规划", "架构", "debug", "调试"]
        model = self.models["thinking"] if any(k in user_msg.lower() for k in think_kw) else self.models["default"]

        try:
            resp = self.claude.messages.create(
                model=model,
                max_tokens=self.max_tokens,
                system=f"你的名字叫{self.display_name}。{self.system_prompt}",
                messages=messages,
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            history.append({"role": "assistant", "content": text})
            self.sessions[session_key] = history[-self.max_context * 2:]
            save_json(self.session_file, self.sessions)
            return text
        except Exception as e:
            err = f"[{self.name}] Claude Error: {e}"
            print(err)
            return err

    async def _call_claude_async(self, session_key: str, user_msg: str) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, functools.partial(self._call_claude_sync, session_key, user_msg)
        )

    # ── Event handler ──

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
        # lark-cli may nest sender_id inside sender object
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

        # Group messages: only respond if this bot was @mentioned
        if chat_type == "group" and self._bot_open_id:
            mentioned_ids = await self._get_mentions(message_id)
            if self._bot_open_id not in mentioned_ids:
                return

        if message_id in self.last_msg_ids:
            return
        self.last_msg_ids[message_id] = time.time()
        save_json(self.msg_file, self.last_msg_ids)

        if len(self.last_msg_ids) > 1000:
            sorted_ids = sorted(self.last_msg_ids.items(), key=lambda x: x[1], reverse=True)
            self.last_msg_ids = dict(sorted_ids[:500])

        print(f"[{self.name}] {chat_type}:{chat_id[:12]}... | {content[:80]}")

        # Enrich message with sender context
        sender_name = self._resolve_sender(sender_id)
        if sender_name:
            content = f"[发送者: {sender_name}] {content}"

        session_key = f"{chat_type}:{chat_id}"
        reply = await self._call_claude_async(session_key, content)
        reply = self._replace_at_tags(reply)

        for i in range(0, len(reply), 7000):
            await self._reply_text(message_id, reply[i:i + 7000])

        print(f"[{self.name}] REPLY: {reply[:80]}...")

    # ── Event stream (lark-cli subprocess) ──

    async def run(self):
        """Connect to Feishu via lark-cli and process events. Reconnects on failure."""
        # Identity already resolved by Bridge.run() — _bot_open_id and _all_bot_ids are set
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
                    text = line.decode().rstrip()
                    if text:
                        print(f"[{self.name}] {text}")

            stderr_task = asyncio.create_task(read_stderr())

            try:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    try:
                        event = json.loads(line.decode())
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


# ═══════════════════════════════════════════════════════════════
# Bridge — orchestrates all bots
# ═══════════════════════════════════════════════════════════════

class Bridge:
    def __init__(self):
        cfg = load_config()
        self.bots: list[BotRunner] = []
        for name, bot_cfg in cfg["bots"].items():
            self.bots.append(BotRunner(name, bot_cfg))

    async def run(self):
        print("=" * 55)
        print(f"Claude <-> Feishu Bridge — {len(self.bots)} bot(s)")
        for bot in self.bots:
            print(f"  [{bot.name}] profile={bot.feishu_profile} "
                  f"models={bot.models['default']}/{bot.models['thinking']}")
        print("=" * 55)

        # Resolve all bot identities first so each can skip siblings
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
        # Set sibling info for each bot (for @mention in replies)
        for bot in self.bots:
            bot._siblings = [
                {"display_name": s.display_name, "open_id": s._bot_open_id}
                for s in self.bots
                if s.name != bot.name and s._bot_open_id
            ]

        tasks = [asyncio.create_task(bot.run()) for bot in self.bots]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            # If any bot exits unexpectedly, report it
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
    if not CONFIG_PATH.exists():
        print(f"Config not found: {CONFIG_PATH}")
        sys.exit(1)

    if not shutil.which(LARK_CLI):
        print(f"ERROR: {LARK_CLI} not found in PATH.")
        print("Install: npm install -g @larksuite/cli")
        sys.exit(1)

    bridge = Bridge()
    try:
        asyncio.run(bridge.run())
    except KeyboardInterrupt:
        print("\n[Bridge] Goodbye.")


if __name__ == "__main__":
    main()
