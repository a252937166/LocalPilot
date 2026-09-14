# LocalPilot 配置与使用：让普通 ChatGPT 操作本机项目

LocalPilot 把普通 ChatGPT 网页对话连接到自己的 Mac，让模型读取项目、加载本地技能、修改代码、运行测试，并返回真实执行结果。平时直接用自然语言发任务，不需要先操作面板。

本文对应 **LocalPilot 0.7.6**，更新于 **2026-09-14**。已完成完整验证的平台是 **macOS**。公开源码：[a252937166/LocalPilot](https://github.com/a252937166/LocalPilot)。

## 1. 能做什么，需要准备什么

| 能力 | 示例 |
| --- | --- |
| 本地编码 | 读取项目规则，修复代码，运行项目测试 |
| 文件与命令 | 搜索、读写、替换、补丁、Shell、退出码与输出 |
| 本地 skills | 按任务发现技能，读取 SKILL.md 及包内参考文件 |
| 图片 | 理解本机图片，裁剪、缩放、旋转、调色，保存宿主交付的图片文件 |
| 浏览器 | 页面快照、导航、点击、输入、截图，复用持久配置中的有效登录态 |
| 多步骤任务 | 保存计划和回执，检查验收条件，暂停、恢复与有条件续跑 |

准备一台联网的 Mac、Python 3.12、Homebrew；浏览器功能另需 Google Chrome。ChatGPT 账号或工作区需支持开发者模式和自定义 MCP 连接，OpenAI Platform 组织需允许使用 Tunnel。

**费用说明：** LocalPilot 不额外收取软件费，不启动 Codex，不要求切到 Work，也不主动调用额外模型生成 API。模型仍由 ChatGPT 提供，普通 Chat 的资格、订阅和额度以账号政策为准；这不等于零 token、无限使用或所有免费账号均可用。[OpenAI 用量说明](https://learn.chatgpt.com/docs/enterprise/chatgpt-work-usage-and-cost)

每位使用者部署自己的 Agent 和 Tunnel。下载源码不会连接作者的电脑，也不需要获得作者的密钥。

## 2. 下载源码并安装本机服务

可以下载本文配套的 `LocalPilot-v0.7.6-wiki-20260914.zip`，也可以从 GitHub 获取源码。二选一即可。

从 ZIP 开始：

```bash
cd ~/Downloads
unzip LocalPilot-v0.7.6-wiki-20260914.zip
cd LocalPilot
```

从 GitHub 开始：

```bash
git clone https://github.com/a252937166/LocalPilot.git
cd LocalPilot
```

在 LocalPilot 目录中继续执行：

```bash
# 已安装 Python 3.12 时可跳过第一行
brew install python@3.12
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
brew install openai/tools/tunnel-client

# 先用自带的小项目完成连接验收
.venv/bin/python scripts/localpilot.py init \
  --workspace "$PWD/examples/calculator" --device-label "My Mac"
.venv/bin/python scripts/localpilot.py install-runtime
```

`init` 默认只授权所选项目，Shell 网络默认关闭。工具里的 `project` 是工作区 ID，对应上面指定的真实路径。已有 `~/.config/localpilot/config.json` 时会保留原配置；若要改工作区，需要编辑现有文件。

## 3. 创建 Tunnel，并让本机上线

```text
普通 ChatGPT Chat
  → OpenAI Secure MCP Tunnel
  → 本机 tunnel-client
  → LocalPilot Agent
  → 本机文件、Shell、技能和浏览器
```

1. 打开 [OpenAI Platform → Tunnels](https://platform.openai.com/settings/organization/tunnels)，切到准备使用的组织。
2. 创建 Tunnel，名称可填 `LocalPilot - My Mac`。
3. 关联实际使用的 **ChatGPT 工作区**，保存并复制 `tunnel_id`。只关联 Platform 组织，可能不会出现在 ChatGPT 的列表中。
4. 准备具备 **Tunnels Read + Use** 权限的运行 API key。创建、修改 Tunnel 还需相应 **Manage** 权限；组织角色和 key 都必须具备所需权限。

ChatGPT 开发者模式权限与 Platform 的组织 Tunnel 权限是两回事。遇到权限入口缺失，应由相应工作区或组织管理员处理。[官方 Tunnel 文档](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

在 Mac 本机终端、LocalPilot 目录执行：

```bash
.venv/bin/python scripts/localpilot.py save-key
```

按终端隐藏提示输入运行 key。它保存在仓库之外的 `~/.config/localpilot/tunnel-runtime.key`，文件权限为 `0600`。这个 key 只用于 Tunnel 身份验证；不要粘贴到 Chat 或 Wiki，也不要放进源码 ZIP。

把下面的 `tunnel_YOUR_ID` 换成自己的 Tunnel ID：

```bash
.venv/bin/python scripts/localpilot.py connect --tunnel-id tunnel_YOUR_ID
.venv/bin/python scripts/localpilot.py status
```

状态中三个字段必须均为 `true`：

```json
{"process_running": true, "healthy": true, "ready": true}
```

`ready` 表示已经准备处理请求。Mac 需要保持开机和联网；当前版本不会自动安装开机启动项。

## 4. 添加到 ChatGPT，配置图标

1. 在 ChatGPT **设置 → 安全与登录** 中开启开发者模式，具体入口取决于账号界面。
2. 打开 [ChatGPT Plugins](https://chatgpt.com/plugins)，点击新增连接。
3. 名称填 `LocalPilot`，描述可写“通过自然对话操作我的 Mac：文件、Shell、技能、图片与浏览器”。
4. 在创建表单的 **图标（可选）** 处上传 `plugins/localpilot/assets/localpilot-web-icon.png`，再选择 **Tunnel** 并选中自己的 Tunnel。
5. 创建连接，核对发现的工具及读写操作确认设置。
6. 新建普通 **Chat / 聊天**，在工具菜单中搜索并添加 LocalPilot。

网页 MCP 连接完成后即可使用，不需要进入“工作 / Work”。[官方连接与刷新流程](https://developers.openai.com/plugins/deploy/connect-chatgpt)

网页专用图标是 `plugins/localpilot/assets/localpilot-web-icon.png`，尺寸 256×256，大小 5,738 字节，是浅灰底的黑色结扣和小云朵。2026-09-14 实测的网页创建表单只接受最大 10 KB 的 PNG，推荐 256×256 或更大。`localpilot-icon.png` 是本地插件包使用的较大原图，不要把它上传到这个表单。

已安装连接不会因修改本地 manifest 自动更换图标。当前管理菜单只有修改名称和描述，没有更换图标入口；需要图标时，可在创建表单上传小图标并复用原 Tunnel。保留旧连接可以继续使用既有对话，新对话选择带图标的新连接。本机 MCP 初始化也提供小图标元数据，但不能代替网页连接的图标上传。

对于支持本地插件包的宿主，`plugins/localpilot/.codex-plugin/plugin.json` 已配置以下字段：

```json
{
  "interface": {
    "logo": "./assets/localpilot-icon.png",
    "composerIcon": "./assets/localpilot-icon.png"
  }
}
```

这段只展示图标字段，不应覆盖完整 manifest；路径相对于 `plugins/localpilot`。该目录还包含 `local-control` 技能。安装这个可选插件包前，复制 `.app.json.example` 为 `.app.json`，再填入自己已注册的 MCP App ID。真实 `.app.json` 不包含在共享包内。

**网页 MCP 连接和本地技能插件包是两个配置层次，不需要为了普通 Chat 工具调用额外安装后者。** [插件打包与图标字段](https://developers.openai.com/plugins/build/plugins#manifest-fields)

## 5. 用一个真实任务确认连接成功

完成 calculator 工作区初始化后，在普通 Chat 中发送：

> 用 LocalPilot 修复 project 工作区 calculator.py 的 add。先读取项目规则，选择并读取适用的本地技能及引用文件。只修改 calculator.py，保持 verify.py 不变。制定计划，实际执行 python3 verify.py，读取退出码和 stdout，通过验收后再完成。全程留在普通 Chat。

示例故意包含加法错误。正确修复后，实际执行测试应输出：

```text
3 checks passed
LOCALPILOT_DEMO_PASS
```

退出码应为 `0`，`verify.py` 应保持原样。不要只以模型说“修好了”作为依据，要核对本机文件、测试输出和执行回执。

![LocalPilot 任务面板：公开演示数据](images/task-panel.png)

![展开执行回执：公开演示数据](images/execution-receipt.png)

上面两张图是实际组件使用公开演示数据渲染的示意图，不是某次 ChatGPT 聊天的原始验收凭证。面板可以查看计划和执行详情，日常直接用自然语言发任务即可。

## 6. 日常怎么使用

### 修改真实项目

把配置文件中的 `workspaces.project` 指向实际项目目录，重启连接，然后发送：

> 查看 project 项目的结构、AGENTS.md 和适用技能。定位测试失败原因，修复相关代码，运行项目原有测试，说明实际修改和验证结果。

多项目可以在 `workspaces` 中配置多个 ID，每个 ID 对应一个实际目录。

### 使用本地技能

LocalPilot 会发现项目技能和配置的用户技能根目录，常用位置包括 `~/.agents/skills`、`~/.codex/skills`、`~/.claude/skills`。`find_skills` 匹配候选，`read_local_skill` 读取正文与包内引用；看到索引不等于已加载正文。

> 按项目规则处理这个问题，先查找并读取相关本地技能，必要时继续读取技能引用的参考文件。

技能不会自行扩大目录授权。文件、网络能力仍受配置及操作系统权限约束。

### 读取或处理本机图片

> 读取 project 目录里的 sample.png，顺时针旋转 90 度，保存为 sample-rotated.png，保留原图并读回核对。

`read_image` 通过 MCP 图片内容块传输图片，内部使用 Base64 编码；模型接收的是图片，不需要阅读长串 Base64 文本。基础裁剪、缩放、旋转和调色由本机完成。重新创作图片则依赖当前 Chat 的原生生图工具与文件交付，不能把基础图片处理测试当成生成式编辑验证。

### 控制浏览器并复用登录态

配置中启用 `browser.enabled` 后，可以说：

> 用 LocalPilot 浏览器打开我指定的页面，读取信息。若需要登录，让我在 LocalPilot 的 Chrome 窗口中完成登录。

默认使用独立的持久 Chrome 配置，不会每次新建无状态配置。第一次在这个窗口登录后，后续可复用仍然有效的登录态。它并不默认等同于日常 Chrome 的主配置；不要为复用登录随意关闭主浏览器或上传浏览器配置目录。网站会话过期后仍需重新登录。

0.7.6 的 `file://` 需要本机绝对路径：workspace 模式限制在项目目录，拒绝保留目录、符号链接和硬链接；full_machine 可访问其他目录，但检查 LocalPilot 受保护路径。检测到当前主页面或 frame 是受限本机 URL 时，拒绝快照、截图和 JS 调用，并隐藏该标签标题；截图和快照重试会复查跳转后的页面。可以关闭该标签，或导航到正常网页。这是工具入口与结果检查，不是 Chrome 进程的操作系统沙箱，不限制所有网页子资源。

## 7. 权限和网络在哪里配置

配置文件是 `~/.config/localpilot/config.json`。保留自己的工作区映射，参考示例逐项调整。

| 配置 | 含义 |
| --- | --- |
| `workspaces` | 工作区 ID 与目录映射 |
| `permission_mode=workspace` | 文件工具限制在配置的项目目录 |
| `permission_mode=full_machine` | 文件工具按当前账号权限访问其他目录，提供 `machine` 工作区 |
| `shell_network=false` | Shell 禁止联网，安装依赖或调用在线服务可能失败 |
| `shell_permission_mode=full_account` | 当前账号的 Shell 能力，需要同时显式开启全机访问和网络 |
| `browser.enabled=true` | 开启浏览器操作 |
| `mcp_bridge.enabled=true` | 开启其他已配置 MCP 服务的桥接 |

项目范围示例为 `examples/config.workspace.json`，完整账号示例为 `examples/config.full-machine.json`。后者允许范围明显更大，按自己的用途选择，不要把他人的机器路径直接覆盖到自己的配置。

全机访问不等于 root，不会绕过 macOS 隐私授权、站点登录或 ChatGPT 确认。公司内网服务仍依赖公司网络/VPN；`shell_network=true` 不会自动恢复内网 DNS 或完成 SSO 登录。

## 8. 启停、升级与恢复任务

在源码目录执行：

```bash
.venv/bin/python scripts/localpilot.py status
.venv/bin/python scripts/localpilot.py stop
.venv/bin/python scripts/localpilot.py connect --tunnel-id tunnel_YOUR_ID
```

升级前先完成或暂停重要任务。Git 安装可以 `git pull`；ZIP 安装则解压新包到新的源码目录，建立虚拟环境并安装依赖。随后依次执行 `stop`、`install-runtime`、`connect`。已有配置和状态在仓库之外，不要把这些目录加入分享包。

工具定义变化后，在 ChatGPT 连接页点击刷新，核对工具，再开新 Chat 测试。只有文档或图标变化时，不需要为了它们重启 Shell 作业。

多步骤任务继续使用原 task_id：

> 恢复任务 task_id。先读取已有计划、文件和回执，再执行剩余步骤，避免重复已经成功的写操作。

自动续跑依赖存活的面板和宿主消息能力，不能保证普通 Chat 永不中断，也不是原生 Stop hook。关闭页面后面板无法发起新一轮；已启动的独立 Shell 作业是否继续，应通过原 job_id 查询。

## 9. 常见问题

| 现象 | 优先检查 |
| --- | --- |
| 没有开发者模式 | ChatGPT 账号资格及工作区策略 |
| Tunnels access required | Platform 组织、角色、Read/Manage/Use 权限 |
| ChatGPT 找不到 Tunnel | 是否关联实际聊天的 ChatGPT 工作区 |
| `healthy=true` 但 `ready=false` | 本机连接状态、网络、运行 key 权限、Agent 日志 |
| 未授权目录 | workspace 映射、permission_mode、系统访问权限 |
| 依赖安装或 SSO 超时 | Shell 网络、公司网络/VPN、目标域名解析与登录状态 |
| 新 Chat 有工具，旧 Chat 没有 | 刷新连接元数据，在新普通 Chat 中重新添加 LocalPilot |
| Failed to fetch template 或一直连接中 | Tunnel、ChatGPT 页面网络、人工验证、组件绑定的任务 ID |
| 模型说完成，但文件或测试不对 | 读回真实文件和回执，通过 review_task 检查验收 |
| 模型说没有生图工具 | 当前对话是否实际提供原生生图能力及可保存文件 |

诊断命令：

```bash
tunnel-client runtimes status localpilot
tunnel-client doctor --profile localpilot --explain
```

运行目录是 `~/.local/share/localpilot/runtime`，配置目录是 `~/.config/localpilot`，任务数据库和日志在 `~/.local/state/localpilot`。分享错误信息前去除密钥、个人路径和私人内容。

## 10. 源码附件与验证范围

配套 ZIP 包含 Agent、安装脚本、插件技能与图标、示例配置、calculator 项目、中英文 README 和本文；不包含真实运行 key、有效连接 `.app.json`、浏览器登录态、任务数据库、私人聊天和历史调试输出。每个安装者都需创建自己的 Tunnel。

0.7.6 已通过 667/667 项本地回归，包括 66 项真实 Chrome 浏览器断言、25 项本地 URL 权限及跳转时序断言、8 项会话持久化断言和 40 项模拟宿主面板断言。完整源码包已包含浏览器修复、MCP 初始化图标及网页专用小图标。此前普通 Chat 验收覆盖技能加载、文件修改、图片旋转、实际命令执行与任务完成；本次未重新执行普通 Chat 或一小时无人干预验收。详见[验证说明](VERIFICATION.zh-CN.md)。

项目是独立实现，不代表 OpenAI 官方插件。源码仓库尚未选定开源许可证；第三方组件保留各自许可证。
