# 使用方式与配置

## 费用与 token

LocalPilot 的用途是把现有普通 Chat 接到自己的电脑，在网页完成编码任务。它不启动 Codex、不切换 Work，也没有主动调用模型生成 API 的客户端。工具执行和状态管理在本机完成；模型推理由 ChatGPT 提供。

这样可以避免另行接入一个按 token 计费的编码 API，也减少为了本地操作而打开其他编码产品的需要。它并没有让推理不再使用 token，也没有测得统一的“节省百分比”。按需读取技能、分页读取文件和截断输出可以限制上下文量，但实际消耗取决于模型与任务。

普通 Chat 是否已经包含在套餐内、开发者模式是否可用、相关功能是否计入共享用量，应以你的账号为准。企业合约中的 Chat 和 Work 还可能影响同一额度池，不能只按标签判断账单。[OpenAI 用量说明](https://learn.chatgpt.com/docs/enterprise/chatgpt-work-usage-and-cost)、[token 与额度说明](https://learn.chatgpt.com/docs/pricing)

因此本项目所说“免费”，指不额外收软件费、不主动发起额外模型 API 调用；不代表无需 ChatGPT 权益、没有用量限制或绕过计费。

## 权限与配置

配置文件为 `~/.config/localpilot/config.json`。`init` 生成范围受限的起始配置，已有文件不覆盖。先把 examples 中的路径改成自己实际存在的项目目录，再参考配置字段；不要直接覆盖已有配置而丢掉自己的 workspace。

| 设置 | 默认或含义 |
|---|---|
| `workspaces` | ID 到目录的映射；工具里的 `project` 是 ID，不是固定路径 |
| `permission_mode=workspace` | 文件工具限制在已配置目录；支持项目内相对路径 |
| `permission_mode=full_machine` | 文件工具可按本机账号权限访问其他目录，自动提供 `machine=/` |
| `shell_permission_mode=restricted` | shell 使用 macOS 限制环境 |
| `shell_network=false` | shell 不允许联网；安装依赖等会失败，需要按实际用途修改 |
| `shell_permission_mode=full_account` | 正常账号 shell 能力；需同时显式设 full_machine 和 shell_network=true |
| `browser.enabled=true` | 开启 Chrome 页面快照、点击、输入、导航、截图 |
| `mcp_bridge.enabled=true` | 允许桥接已配置的 MCP；在全机模式默认开启，示例配置显式关闭 |

[项目范围示例](../examples/config.workspace.json) · [完整账号示例](../examples/config.full-machine.json)

完整账号模式会允许 shell 使用当前账号可访问的文件、网络和进程，适合确认这些访问符合用途的个人部署。它不会获得 root、绕过 macOS 隐私授权或替代 ChatGPT 的确认。文件层保护运行目录和状态，但完整账号 shell 本身不是额外隔离环境。

## 本地技能

本机存在的 `~/.agents/skills`、`~/.codex/skills`、`~/.claude/skills` 等目录会被发现，也会扫描项目技能与 AGENTS.md。`find_skills` 选择候选，`read_local_skill` 读取正文和包内引用；索引不是已经加载正文。技能不会自行扩张文件授权，任务只加载相关内容。

可以直接说：“按项目规则修复问题，先选择并读取适用技能及引用文件”。仓库的 calculator 示例包含一个可复现的技能与引用文件。

## 浏览器与登录态

LocalPilot 使用独立的持久 Chrome 配置，或接入显式配置的 CDP 地址。默认配置不是你日常 Chrome 的主配置。第一次可在 LocalPilot 打开的 Chrome 窗口登录，之后复用该配置及有效登录态；网站会话过期仍需登录。

可选的 `clone_logins` 用于按用户要求导入现有 Chrome 登录，要求源 Chrome 退出且目标配置未被占用，不应在每次任务开始时执行。系统加密、网站策略和账号登录有效期仍可能影响复用。不要公开调试端口或上传浏览器配置目录。

## 图片和原生生图

`read_image` 返回 MCP 图片内容块：图片字节经 Base64 传输，不是要求模型阅读一大段 Base64 文本。基础 `edit_image` 提供裁剪、缩放、旋转、翻转和调色；这些操作不调用生图模型。

`save_chat_image` 保存 ChatGPT 交付的真实文件参数，并读回确认。`download_image` 支持可访问的 HTTPS 图片地址。生成式编辑依赖当前 Chat 是否提供生图工具、是否传入正确参考图、是否返回可下载文件；准备流程或保存成功不能证明创作保真度。`prepare_image_workflow` 可组织参考图和后续保存，组件存活及宿主支持仍是前提。

## 任务计划与继续执行

多步骤任务用 create_task 保存目标和验收，run_task_step 执行动作，inspect_task_step 留下读取回执，review_task 和 finish_task 检查真实结果。相同 action_id 重放不重复执行；任务暂停阻止新动作。图片暂停后不再提交文件，已经执行的外部动作不能自动撤销。

自动续跑借助存活面板向宿主请求后续消息，受次数和时间限制；不是能接管普通 Chat 的模型循环。关闭页面后无法依靠面板发起新一轮。已经启动的独立 shell 作业可继续并保存结果，恢复时用原 task_id/job_id 查回执。

## 启动停止与升级

```bash
.venv/bin/python scripts/localpilot.py status
.venv/bin/python scripts/localpilot.py stop
.venv/bin/python scripts/localpilot.py connect --tunnel-id tunnel_YOUR_ID
```

升级源码后，先暂停重要任务，再执行：

```bash
git pull
.venv/bin/python scripts/localpilot.py stop
.venv/bin/python scripts/localpilot.py install-runtime
.venv/bin/python scripts/localpilot.py connect --tunnel-id tunnel_YOUR_ID
```

如有工具定义变更，再到 ChatGPT 连接设置刷新元数据、核对工具，并在新 Chat 验证。运行目录为 `~/.local/share/localpilot/runtime`；SQLite、作业回执和 stderr 位于 `~/.local/state/localpilot`。该版本没有安装开机启动项。

## 可选插件包

网页连接在完成 Tunnel 接入后即可调用工具。`plugins/localpilot` 是带 local-control 技能的个人插件模板；本仓库不包含任何人的有效 `.app.json`。

如果你的宿主支持本地插件包，先注册自己的 MCP 连接，再将其真实 App ID 填入 `.app.json`：

```bash
cp plugins/localpilot/.app.json.example plugins/localpilot/.app.json
# 本机编辑 .app.json，将占位符改为你自己的连接 ID
```

`.app.json` 被 Git 忽略。模板只用于支持该打包流程的宿主；不要把模板中的占位符直接拿去连接，也不要认为 GitHub 仓库地址能替代 Tunnel。
