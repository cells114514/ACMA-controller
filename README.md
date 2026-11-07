# ACMA Controller

**面向 Agent 的 Windows 虚拟机控制接口与执行服务。**
**A Windows VM control interface and execution service for agents.**

[中文](#中文) · [English](#english) · [API](docs/API.md) · [Setup / 部署](docs/SETUP.md) · [Security / 边界](SECURITY.md)

> **0.1.0 · Experimental / 实验版本** — Windows host + Windows guest, Hyper-V lifecycle management, Cua Driver desktop execution. Standard-library Python runtime; no model API key. / Windows 宿主与客户机，Python 运行层仅用标准库，无需模型密钥。

## 中文

### 定位

Agent 决定做什么，Controller 执行并返回实际结果与可观察证据。它不规划任务，不维护聊天、角色、长期记忆或应用技能，也不自行判断用户目标是否完成。

```text
你的 Agent / 自动化程序
       │ Python SDK / 带鉴权的本地 HTTP
       ▼
ACMA Controller
 ├─ 固定 VM 身份 · 队列 · 暂停/恢复/停止 · SQLite 回执
 ├─ 客户机 PowerShell / 桌面操作 · UIA / 截图 / 操作后回读
 └─ 宿主 Hyper-V 固定适配器 · 电源 / 运行状态 / 检查点
       │ SSH 主机密钥固定 + 客户机身份核验
       ▼
Windows 客户机 · 受保护桥接脚本 · 交互会话中的 Cua Driver
```

### 功能与边界

| 接口 | 能力 |
| --- | --- |
| 桌面原子操作 | 应用/窗口枚举、UIA/截图、窗口激活、点击/双击/右键、文本、按键/快捷键、滚动、拖动、真实鼠标移动 |
| 元素检索 | 本地确定性 `find_elements`，不调用模型，明确返回采集不完整状态 |
| 滚入可视区 | `scroll_to_element`，最多六次滚轮手势并新读几何；不保证元素内部任意文字或遮挡情况 |
| 客户机命令 | 专用起始目录中的 PowerShell；**目录不是安全沙箱**，命令拥有客户机账户权限 |
| Hyper-V | 状态/IP、开机、正常关机/重启、保存/恢复运行状态、检查点创建/列表/精确 ID 恢复 |
| 执行管理 | 单 worker、持久回执、防止未知结果自动重发、暂停/恢复/停止、显式对账 |
| 回读与证据 | 桌面动作后读取窗口状态；观察到变化不等于用户目标完成 |

不开放 `set_value`、任意宿主 Shell、强制断电/Reset、锁屏绕过。高权限窗口、UIA 缺失、开始菜单磁贴拖动等仍有平台兼容边界。

### 快速开始：仅虚拟机管理

需要 Python 3.11+、Windows Hyper-V 管理组件、相应权限和已有 VM：

```powershell
git clone https://github.com/cells114514/ACMA-controller.git
cd ACMA-controller
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
Copy-Item config.example.json config.local.json
# 编辑 config.local.json，填写自己的 VM GUID、名称和客户机身份。
```

Python SDK（下面只读，不开关机）：

```python
import os
os.environ['ACMA_CONTROLLER_CONFIG'] = r'C:\path\to\config.local.json'
from acma_controller import Controller

with Controller() as controller:
    print(controller.call('vm_status', {}))
    print(controller.call('vm_list_checkpoints', {}))
```

在导入前设置配置；每进程只绑定一台 VM。变更前先调用 `controller.resume()`。`vm_*` 不需要客户机开机或 SSH 在线。桌面/命令接口还需要[部署客户机桥接](docs/SETUP.md)，安装 Python 包不等于已经完成客户机安装。

### 本地 HTTP 服务

设置随机令牌，不要将真实值贴到问题或提交到 Git：

```powershell
$env:ACMA_CONTROLLER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
.\.venv\Scripts\python.exe -m acma_controller --config config.local.json serve
```

默认监听 `127.0.0.1:8767`，启动时暂停输入，所有接口需要 `Authorization: Bearer <令牌>`，拒绝浏览器 Origin，不是公网服务。`python examples/client.py` 只读查看状态。通过 `GET /v1/tools` 读取工具定义，自行适配模型框架；本项目未绑定特定 Agent SDK，也没有宣称已提供 MCP 服务。

### 执行不等于完成

1. **派发**：控制器是否送出了动作。
2. **执行结果**：回执是否确认操作终态；`unknown` 不代表失败，也不能自动重试。
3. **目标完成**：由上层 Agent 根据回执、回读和截图判断；本项目没有语义上的 `finish`。

生命周期工具返回操作编号。`vm_operation_status` 一次最多等待 20 秒，终态立即返回；内部每 250 毫秒只读本地文件，不调用模型或重复查询 Hyper-V。等待超时不取消已派发动作。HTTP 超时后，应查原编号或用相同 `request_id` 取已有结果，不要换编号盲目重发。

### 验证与许可

这是独立提取后的首版。原 ACMA 在固定 Windows/Hyper-V 环境中验证过桌面/命令路径和真实开机、Standard 检查点创建、正常重启；**独立包在你的 VM 上仍需重新配置与验收**。CI 的模拟、协议、队列及边界测试不计作真实 VM 验收。独立包尚不承诺检查点恢复数据一致性、保存/恢复运行状态、所有应用兼容或完整故障恢复。

```powershell
python -m unittest discover -s tests
```

配置、密钥、截图、日志、SQLite 和虚拟机磁盘不在仓库中。见[安全边界](SECURITY.md)与[第三方说明](THIRD_PARTY_NOTICES.md)。本仓库代码采用 [MIT](LICENSE)，Cua Driver 独立安装并遵守其具体版本许可。

## English

### Purpose

The agent decides what to do; the controller executes and returns observable facts. It does not plan tasks, manage chats/personas/memory/skills, or independently decide whether the user's goal is complete.

It exposes a model-independent Python SDK and authenticated loopback HTTP interface over a durable execution queue, a pinned SSH guest bridge, Cua Driver and a fixed-target Hyper-V adapter. One process registers one VM. No model API key or third-party runtime Python dependency is required.

### Features and boundaries

| Interface | Capability |
| --- | --- |
| Desktop primitives | App/window enumeration, UIA/screenshots, activation, clicks, typing, keys, scrolling, dragging and real pointer movement |
| Element search | Deterministic local `find_elements`; incomplete collection is explicit, not proof of absence |
| Scroll into view | At most six wheel gestures using fresh UIA geometry; geometric visibility does not prove lack of occlusion |
| Guest commands | PowerShell starting in the configured workspace; **the directory is not a security sandbox** |
| Hyper-V | Status/addresses, start, graceful shutdown/restart, save/resume state, checkpoint creation/listing/exact-ID restore |
| Execution management | Single worker, persistent receipts, pause/resume/stop, reconciliation and no automatic replay of unknown actions |
| Evidence | Post-action window observations, separately from semantic goal completion |

No exposed `set_value`, arbitrary host shell, force-off/reset, lock-screen bypass, model planner or bundled MCP server. Application UIA/focus compatibility is not universal; Start-menu tile dragging remains platform-sensitive.

### Quick start: host-only VM tools

Use Windows with Hyper-V management components, appropriate permissions, Python 3.11+ and an existing VM:

```powershell
git clone https://github.com/cells114514/ACMA-controller.git
cd ACMA-controller
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
Copy-Item config.example.json config.local.json
# Edit the local file with your VM GUID/name and guest identity.
```

Read-only SDK example:

```python
import os
os.environ['ACMA_CONTROLLER_CONFIG'] = r'C:\path\to\config.local.json'
from acma_controller import Controller

with Controller() as controller:
    print(controller.call('vm_status', {}))
    print(controller.call('vm_list_checkpoints', {}))
```

Set configuration before import. Call `controller.resume()` before mutations. VM tools work while the guest is Off and do not require SSH. Desktop/command tools additionally require a separately deployed guest bridge and interactive Driver session: follow [SETUP.md](docs/SETUP.md).

### Local HTTP service

```powershell
$env:ACMA_CONTROLLER_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
.\.venv\Scripts\python.exe -m acma_controller --config config.local.json serve
```

The server binds to `127.0.0.1:8767`, starts paused and requires a bearer token on every request. Browser-origin requests are rejected. Never publish the token. `python examples/client.py` reads service status. Retrieve schemas from `GET /v1/tools` and map them to your agent framework; see [API.md](docs/API.md).

### Execution is not goal completion

- **Dispatch:** whether an action was sent.
- **Execution:** what the terminal receipt and fresh readback establish. Unknown does not mean failed or safe to retry.
- **Goal completion:** your agent's assessment using the evidence; there is no semantic `finish` tool here.

Lifecycle calls return an operation ID. `vm_operation_status` waits up to 20 seconds, returning immediately on a terminal receipt. Its 250 ms polling reads a local file without model calls or repeated Hyper-V queries. Waiting timeout does not cancel dispatched actions. After a lost HTTP response, inspect the original operation or reuse the same request ID to retrieve an existing result; never blindly submit a new mutation.

### Validation and license

This is the first standalone extraction. The original ACMA integration exercised desktop/command paths and real start/checkpoint-create/graceful-restart operations on one Windows/Hyper-V setup. **The standalone package still needs configuration and acceptance on your VM.** CI uses fake backends and protocol/boundary tests, not real VM acceptance. Checkpoint restoration integrity, saved-state recovery, universal app compatibility and complete failure recovery are not claimed as validated.

```powershell
python -m unittest discover -s tests
```

No personal configuration, credentials, screenshots, logs, databases or VM images are shipped. See [security boundaries](SECURITY.md) and [third-party notices](THIRD_PARTY_NOTICES.md). ACMA-controller is [MIT licensed](LICENSE); Cua Driver is installed separately under its applicable upstream terms.
