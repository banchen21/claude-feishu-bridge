# Claude <-> Feishu Bridge

将 Claude Code CLI 接入飞书群聊，支持多机器人协作、卡片交互和会话持久化。

## 架构

```
bridge.py [--gui]
├── Web Dashboard (FastAPI + WebSocket)        ← http://127.0.0.1:8080
├── lark_oapi SDK WebSocket (dedicated thread)  ← 飞书长连接
│   ├── honglong:  messages + card actions
│   ├── xiaohong:  messages + card actions
│   └── xiaolong:  messages + card actions
└── Claude CLI 子进程 (per message)
```

- 每个 Bot 独立：自己的飞书应用凭证、WebSocket 连接、Claude 会话
- 统一使用 lark_oapi SDK 长连接，一个线程管理所有 WebSocket
- 会话通过 UUID 确定性生成，重启不丢失
- 机器人间可互相 @ 协作
- 群聊中仅响应 @提及或 @所有人，私聊全部响应
- 支持图片/文件下载，Claude 可直接 Read 附件
- 支持卡片交互回调（按钮点击 → 更新卡片 + 发送给 Claude 处理）

## 依赖

| 类型 | 名称 | 用途 |
|------|------|------|
| CLI | [Claude Code](https://docs.anthropic.com/en/docs/claude-code) | AI 引擎 |
| CLI | [lark-cli](https://www.npmjs.com/package/@larksuite/cli) | Profile 管理 |
| Python | [lark-oapi](https://pypi.org/project/lark-oapi/) | 飞书 WebSocket 事件流 + 卡片回调 |
| Python | httpx | 飞书 REST API |
| Python | fastapi + uvicorn | Web 管理面板 (`--gui`) |

## 快速开始

### 1. 安装依赖

```bash
# exe 用户跳过此步

git clone git@github.com:banchen21/claude-feishu-bridge.git
cd claude-feishu-bridge
pip install httpx fastapi uvicorn lark-oapi
npm install -g @larksuite/cli
```

### 2. 飞书开放平台

为每个 Bot 创建企业自建应用：

1. [飞书开放平台](https://open.feishu.cn) → 创建企业自建应用
2. 权限：`im:message`、`im:message:read`、`im:message.reactions:write_only`
3. 开启机器人能力，发布版本并审批
4. 事件订阅 → 选择「长连接」方式 → 添加 `im.message.receive_v1`

### 3. 配置

```bash
cp bridge-config.example.json bridge-config.json
```

编辑 `bridge-config.json`：

```json
{
  "bots": {
    "honglong": {
      "display_name": "红龙",
      "feishu_app_id": "cli_xxxxxxxxxxxx",
      "feishu_app_secret": "YOUR_SECRET",
      "claude": {
        "system_prompt": "你是红龙，飞书群里的AI助手。回答简洁清晰。"
      },
      "claude_cli": {
        "permission_mode": "auto",
        "add_dirs": ["C:\\Users\\yourname\\Desktop"],
        "max_turns": 20,
        "timeout_seconds": 120,
        "reaction_emoji": "Typing"
      }
    }
  }
}
```

### 4. 启动

```bash
# exe
bridge.exe
bridge.exe --gui          # 带 Web 管理面板

# 源码
python bridge.py
python bridge.py --gui
```

看到 `Feishu WS connected` 即连接成功。

## 配置参考

### `bridge-config.json`

| 字段 | 类型 | 说明 |
|------|------|------|
| `bots.<key>` | object | Bot 标识 |
| `display_name` | string | 群内显示名称 |
| `feishu_app_id` | string | 飞书应用 App ID |
| `feishu_app_secret` | string | 飞书应用 App Secret |
| `claude.system_prompt` | string | 系统提示词（必填） |
| `claude_cli.permission_mode` | string | 权限模式，建议 `auto` |
| `claude_cli.model` | string | 模型覆盖（可选，默认环境变量） |
| `claude_cli.add_dirs` | string[] | 允许 Claude 访问的目录 |
| `claude_cli.max_turns` | int | 最大对话轮次，默认 20 |
| `claude_cli.timeout_seconds` | int | 超时秒数，默认 120 |
| `claude_cli.reaction_emoji` | string | 处理中表情，默认 `Typing` |
| `claude_cli.stream_edit` | bool | 流式编辑模式，默认 false |
| `max_context_messages` | int | 消息去重上限，默认 20 |

### Claude Code 环境变量

项目目录下 `.claude/settings.json` 示例（DeepSeek 后端）：

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

## 命令行

```
python bridge.py [--gui] [--port 8080] [--host 127.0.0.1]
```

| 参数 | 说明 |
|------|------|
| `--gui` | 启动 Web 管理面板 |
| `--port` | 面板端口，默认 8080 |
| `--host` | 绑定地址，默认 127.0.0.1 |

Web 面板提供：Dashboard 总览、Bot 增删改、Profile 管理、实时日志流。

## 文件结构

```
claude-feishu-bridge/
├── bridge.py                  # 主程序
├── bridge-gui.py              # Web 管理面板
├── bridge.spec                # PyInstaller 打包配置
├── bridge-config.json         # 配置（含密钥，gitignore）
├── bridge-config.example.json # 配置模板
├── .claude/settings.json      # Claude Code 项目级设置
├── .cli-sessions/             # Claude CLI 会话工作目录
├── .bridge-media/             # 下载的媒体文件
└── .gitignore
```

## License

MIT

---

<div align="center">

**[73Info 柒叁信息](https://73info.cn)** — 开发者资源发现 · 需求对接 · 定制协作平台

*需要 AI 开发？来 73Info 找到靠谱的开发者。*

</div>
