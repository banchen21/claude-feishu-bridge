# Claude <-> Feishu Bridge

将 Claude Code CLI 接入飞书群聊，支持多机器人协作、Agent 工具调用和会话持久化。

## 架构

```
bridge.exe [--gui]
├─ Web Dashboard (FastAPI + WebSocket)  ← --gui 模式
│   └─ http://127.0.0.1:8080
├─ BotRunner "honglong" (红龙)     BotRunner "xiaohong" (小红)     BotRunner "xiaolong" (小龙)
│   ├─ lark-cli event consume      ├─ lark-cli event consume      ├─ lark-cli event consume
│   │   └─ WebSocket ─ 飞书事件     │   └─ WebSocket ─ 飞书事件     │   └─ WebSocket ─ 飞书事件
│   └─ claude -p --resume          └─ claude -p --resume          └─ claude -p --resume
│       └─ subprocess 阻塞调用          └─ subprocess 阻塞调用          └─ subprocess 阻塞调用
```

- 每个 Bot 独立：自己的飞书应用、lark-cli 事件流、Claude CLI 会话
- 会话通过 UUID5（`bridge.{name}.{chat}`）确定性生成，重启不丢失
- 机器人间可互相 @ 协作，自动转换为飞书 `<at>` 标签
- **lark-cli profile 自动注册** — 只需配置 `bridge-config.json`，启动时自动创建

## 安装

### 方式一：下载 exe（推荐）

从 [Releases](https://github.com/banchen21/claude-feishu-bridge/releases) 下载 `bridge.exe`，放置到任意目录。

### 方式二：源码运行

```bash
git clone git@github.com:banchen21/claude-feishu-bridge.git
cd claude-feishu-bridge
pip install httpx fastapi uvicorn
```

## 依赖

| 类型 | 名称 | 用途 | 安装 |
|---|---|---|---|
| Python | httpx | 飞书 REST API 客户端 | `pip install httpx`（exe 已内置） |
| Python | fastapi, uvicorn | Web 管理面板（`--gui` 模式） | `pip install fastapi uvicorn`（exe 已内置） |
| CLI | Claude Code | AI 引擎 | [官方安装](https://docs.anthropic.com/en/docs/claude-code) |
| CLI | lark-cli | 飞书事件订阅 | `npm install -g @larksuite/cli` |

## 快速开始

### 1. 飞书开放平台配置

为每个机器人创建企业自建应用：

1. 在 [飞书开放平台](https://open.feishu.cn) 创建企业自建应用
2. 添加权限：`im:message`、`im:message:read`、`im:message.reactions:write_only`
3. 开启机器人能力，发布版本并审批

### 2. 项目配置

```bash
# exe 用户：在 exe 同目录下创建
# 源码用户：
cd claude-feishu-bridge
cp bridge-config.example.json bridge-config.json
```

编辑 `bridge-config.json`，填入飞书 App ID / Secret。支持多个 Bot 协作：

```json
{
  "bots": {
    "honglong": {
      "display_name": "红龙",
      "feishu_profile": "bridge",
      "feishu_app_id": "cli_xxxxxxxxxxxx",
      "feishu_app_secret": "YOUR_SECRET",
      "claude": {
        "system_prompt": "你是红龙，擅长技术问题的AI助手。"
      },
      "claude_cli": {
        "permission_mode": "auto",
        "allowed_tools": ["Bash", "Edit", "Read", "Write", "Glob", "Grep"],
        "add_dirs": ["C:\\Users\\yourname\\Desktop"],
        "max_turns": 20,
        "timeout_seconds": 120
      }
    },
    "xiaohong": {
      "display_name": "小红",
      "feishu_profile": "xiaohong",
      "feishu_app_id": "cli_xxxxxxxxxxxx",
      "feishu_app_secret": "YOUR_SECRET",
      "claude": {
        "system_prompt": "你是小红，负责运营工作。"
      },
      "claude_cli": {
        "permission_mode": "auto",
        "allowed_tools": ["Bash", "Edit", "Read", "Write", "Glob", "Grep"],
        "add_dirs": ["C:\\Users\\yourname\\Desktop"],
        "max_turns": 20,
        "timeout_seconds": 120
      }
    }
  }
}
```

**注意：** `feishu_profile` 无需手动创建，bridge 启动时会自动注册。

### 3. Claude Code 设置

项目根目录的 `.claude/settings.json` 会自动被 Claude CLI 发现，示例（DeepSeek 后端）：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
    "ANTHROPIC_MODEL": "deepseek-v4-pro[1m]",
    "CLAUDE_CODE_EFFORT_LEVEL": "max"
  }
}
```

`ANTHROPIC_AUTH_TOKEN` 从系统环境变量读取。

### 4. 启动

```bash
# exe
bridge.exe
bridge.exe --gui

# 源码
python bridge.py
python bridge.py --gui
```

看到 `[honglong] ready event_key=im.message.receive_v1` 即表示连接成功。

Web 管理面板地址：http://127.0.0.1:8080

## 配置参考

### `bridge-config.json`

| 字段 | 说明 |
|---|---|
| `bots.<name>` | 机器人 key，用于会话隔离 |
| `display_name` | 群内显示名称 |
| `feishu_profile` | lark-cli profile 名（自动注册） |
| `feishu_app_id` / `feishu_app_secret` | 飞书应用凭证 |
| `claude.system_prompt` | 系统提示词（必填） |
| `claude_cli.permission_mode` | 权限模式，建议 `"auto"` |
| `claude_cli.allowed_tools` | 允许的工具列表 |
| `claude_cli.add_dirs` | 允许访问的目录 |
| `claude_cli.max_turns` | 最大对话轮次 |
| `claude_cli.timeout_seconds` | 超时秒数 |
| `claude_cli.reaction_emoji` | 处理中表情，默认 `"Typing"` |

### 交互流程

```
用户发消息 → lark-cli WebSocket 推送事件 → 去重检查
  → @提及检测（群聊） → 添加"Typing"表情
  → claude -p --resume <uuid> → 移除表情 → 回复消息
  → 自动将 @名字 转为飞书 <at> 标签
```

## 文件结构

```
claude-feishu-bridge/
├── bridge.py                  # 主程序
├── bridge.exe                 # 打包的可执行文件（Release 下载）
├── bridge-gui.py              # Web 管理面板
├── bridge-config.json         # 机器人配置（gitignore，含密钥）
├── bridge-config.example.json # 配置模板
├── .claude/settings.json      # Claude Code 项目级设置
├── .cli-sessions/             # Claude CLI 会话工作目录
└── .gitignore
```

## License

MIT

---

<div align="center">

🌐 **[73Info 柒叁信息](https://73info.cn)** — 开发者资源发现 · 需求对接 · 定制协作平台

*需要 AI 开发？来 73Info 找到靠谱的开发者。*

</div>
