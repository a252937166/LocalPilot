# 从零创建 Tunnel，接入网页 Chat

完整顺序：**创建 Tunnel → 关联 ChatGPT 工作区 → 本机运行 tunnel-client → 连接 LocalPilot Agent → ChatGPT 添加 Tunnel 连接 → 普通 Chat 验收。**

这是每位使用者自己的私有部署。不要使用他人的 Tunnel ID 或运行密钥。本文结合本项目的安装脚本与 2026-09-12 核对的官方文档；账号界面的名称可能随版本不同。

## 1. 先确认两组权限

| 位置 | 需要的能力 |
|---|---|
| OpenAI Platform 组织 | 查看及使用 Tunnel：Tunnels Read + Use；创建、修改另需 Manage |
| 目标 ChatGPT 工作区 | 开发者模式；团队账号可能需要工作区管理员开启 |

这两组权限独立。Platform 的项目权限不等于组织 Tunnel 权限，订阅 ChatGPT 也不自动证明你有所有开发者入口。[官方权限说明](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

## 2. 创建私有 Tunnel

打开 [OpenAI Platform → Tunnels](https://platform.openai.com/settings/organization/tunnels)，先切到准备使用的组织，创建新的 Tunnel。名称可用 `LocalPilot - My Mac`。

在 Tunnel 的组织／工作区关联中加入实际使用的 **ChatGPT 工作区**，然后保存并复制 `tunnel_id`。它通常形如 `tunnel_...`。仅关联 Platform 组织，可能不会出现在 ChatGPT 的 Tunnel 列表中。

```text
Platform：Tunnels
  └─ LocalPilot - My Mac
       ├─ Platform organization：你的组织
       ├─ ChatGPT workspace：你实际聊天的工作区
       └─ Tunnel ID：tunnel_YOUR_ID
```

这是字段示意，不是账号后台截图。教程不会展示作者的工作区、Tunnel 标识或密钥。

## 3. 准备运行密钥

在目标 Platform 组织中准备一个具有 **Tunnels Read + Use** 的运行 API key。组织的角色和密钥权限都要允许该操作；界面若要求管理员处理，就由该组织管理员配置。运行 Agent 不需要把 Tunnel 管理权限交给模型。

完成 README 的本机初始化后，在 **Mac 本机终端** 执行：

```bash
.venv/bin/python scripts/localpilot.py save-key
```

按隐藏提示粘贴运行密钥。它写入 `~/.config/localpilot/tunnel-runtime.key`，权限为 `0600`；不要写进项目文件、Chat 消息、截图或 GitHub。这里的 key 只供 `tunnel-client` 验证隧道身份，本项目不拿它调用模型 API。

## 4. 在本机连接 Agent

```bash
brew install openai/tools/tunnel-client
.venv/bin/python scripts/localpilot.py connect --tunnel-id tunnel_YOUR_ID
.venv/bin/python scripts/localpilot.py status
```

本项目已验证 `tunnel-client 0.0.14`。安装来源是 [OpenAI 的 Homebrew tap](https://github.com/openai/homebrew-tools/blob/main/Formula/tunnel-client.rb)。脚本对应的原生命令为：

```bash
tunnel-client runtimes connect \
  --alias localpilot \
  --tunnel-id tunnel_YOUR_ID \
  --profile localpilot \
  --mcp-command "$HOME/.local/share/localpilot/runtime/start-agent" \
  --runtime-api-key "file:$HOME/.config/localpilot/tunnel-runtime.key"
```

状态结果应包含：

```json
{"process_running": true, "healthy": true, "ready": true}
```

`healthy` 只说明进程健康，`ready` 才表示已经准备处理请求。安装脚本会一起检查这三个值。保留本机开机、联网；没有自动安装开机启动项。

## 5. 在 ChatGPT 创建自己的连接

在 ChatGPT **设置 → 安全与登录 → 开发者模式** 开启入口；前往 [Plugins](https://chatgpt.com/plugins)，点击新增，填写：

| 字段 | 示例 |
|---|---|
| 名称 | LocalPilot |
| 描述 | 通过自然对话操作我的 Mac：项目文件、shell、技能、图片与浏览器 |
| Connection / 连接 | Tunnel |
| Tunnel | 选择刚创建的 Tunnel，或按界面要求填写其 ID |

LocalPilot 的本地 stdio 服务不额外要求 OAuth；私有入口使用 Tunnel 的组织／工作区权限。创建后核对发现的工具和相应读写确认设置。[官方连接流程](https://developers.openai.com/plugins/deploy/connect-chatgpt)

当前面板声明了受限 CSP。如账号显示“在开发者模式下强制执行 CSP”选项，保持开启；不需要为展示面板放开任意外部域名。

## 6. 在普通 Chat 自测

新建对话，确认是 **Chat / 聊天**，从输入框工具菜单搜索并添加 LocalPilot。不要把“进入工作模式”当成安装的必要步骤。

先发“用 LocalPilot 查看这台电脑的版本和 project 目录”，再运行 [README 的计算器示例](../README.zh-CN.md#快速开始)。应看到实际文件结果和命令退出码；任务面板可查看计划和回执，普通操作不需要先点击面板。

## 卡住时排查

| 现象 | 检查位置 |
|---|---|
| Tunnels access required | Platform 是否选对组织，当前用户是否有组织级 Read／Manage／Use 权限 |
| ChatGPT 看不到 Tunnel | Tunnel 是否关联当前 ChatGPT 工作区，当前用户是否有 Read + Use |
| 没有开发者模式 | 账号资格或工作区策略；安装本机代码无法创建这个权限 |
| healthy=true，ready=false | `scripts/localpilot.py status`、本机 stderr 日志、网络与密钥权限 |
| 新 Chat 可用，旧 Chat 没工具 | 刷新连接元数据并重新开 Chat 加入连接；这是宿主工具加载范围的问题 |
| Failed to fetch template | 同时检查 Tunnel 可用性和 ChatGPT 页面网络；若网页要求人工验证，应在网页完成 |
| “连接中”或按钮超时 | 查看 Agent 是否在线、面板是否绑定当前 task_id，再刷新；不要把消息未确认当作一定没有投递 |

进一步诊断：`tunnel-client runtimes status localpilot`、`tunnel-client doctor --profile localpilot --explain`。分享日志前去除密钥、文件内容及个人标识。
