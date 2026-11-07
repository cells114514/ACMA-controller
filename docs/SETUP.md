# Setup / 安装与绑定

## 中文

### 1. 只使用虚拟机管理接口

宿主机须为 Windows，已启用 Hyper-V 和管理 PowerShell 模块，并已有目标虚拟机。安装 Python 3.11+。用有 Hyper-V 权限的账户运行服务；通常需要管理员终端或相应的 Hyper-V 管理权限。

安装本项目，复制根目录 `config.example.json` 为不提交的 `config.local.json`，填写真实 VM ID、名称和客户机账户/计算机名/工作目录。VM ID 可在宿主只读查询：

```powershell
Get-VM | Select-Object Name, Id, State
```

管理电源与检查点不依赖 SSH、客户机桌面在线或模型密钥。`vm_status` 的 IP 是 Hyper-V 报告值，可能为空或尚未更新。

### 2. 增加客户机桌面与命令接口

这部分是显式部署，不会随 Python 安装自动执行：

1. 在客户机安装自己核验过的 Windows OpenSSH Server 和 Cua Driver（基线 0.38.0），保持客户机交互桌面已登录。Driver 必须在该用户的交互会话运行，不能只在 SSH/Session 0 里启动。高完整性目标需要兼容的 Driver 权限配置；不能承诺所有程序 UIA 都可读。
2. 将包内 `acma_controller/scripts` 复制到客户机临时目录。可以用 `python -c "import acma_controller,pathlib; print(pathlib.Path(acma_controller.__file__).parent/'scripts')"` 找到安装位置。
3. 在客户机本地以预期客户机用户提升权限运行 `prepare_guest.ps1 -VMId <目标ID> -Driver <已安装的EXE绝对路径> -DriverSha256 <独立核对的SHA256>`。它只准备受保护桥接文件与绑定，不安装软件、不设置免密、不更改认证、不启动 Driver、不覆写已有绑定。核对输出的 VM ID、计算机名和 SID。
4. 为控制器单独创建 SSH 密钥。按微软 OpenSSH 说明配置公钥文件，使用 `restrict` 和 forced command。带空格的默认路径示例如下（替换真实公钥）：

   ```text
   restrict,command="powershell.exe -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File \"C:/Program Files/ACMA-controller/bridge/guest_cua_ssh_entry.ps1\"" ssh-ed25519 <CONTROLLER-PUBLIC-KEY>
   ```

   检查 `authorized_keys` 的实际解析结果，必须强制进入这个受保护脚本；不要开放普通远程 Shell，也不要使用用户可写的提权入口。命令接口创建本账户的 Windows 计划任务，需要相应权限。

5. 在客户机本地或已有可信通道核对 SSH **服务器公钥**；不要盲目信任扫描到的 key。将 `examples/connection.example.json` 的真实副本保存到私有数据目录 `connection/connection.json`。三个密钥相关文件路径必须位于同一私有 `connection` 目录。
6. `known_hosts` 仅写入已核对的 `acma-<小写VM-ID> ssh-ed25519 <真实公钥>`；`ssh_config` 可以为空。设置私钥文件为只有所有者可读。`host_key` 应为公钥类型和内容，不包含尾部注释。
7. 先测试 `health_report`、`list_apps`、`list_windows` 等只读工具，再在专用测试窗口验证输入。不要直接用真实聊天发送等任务做首次测试。

Windows SSH 的配置和 ACL 请参照 [Microsoft OpenSSH Server configuration](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh-server-configuration)。Cua 的部署/权限请参照[上游文档](https://cua.ai/docs/cua-driver)。本项目不会修改 SSH 主机密钥，也不会绕过锁屏或认证。

自动 IP 恢复优先验证已配置地址；失效时，只在 Hyper-V Default Switch 的有限邻居中尝试，并保持原 SSH host key、VM ID、计算机名、SID 绑定。自定义交换机使用显式地址配置，不声称支持任意网络发现。

## English

### Host-only VM tools

Use a Windows host with Hyper-V management cmdlets, an existing VM, Python 3.11+, and the permissions required to manage that VM. Copy `config.example.json` to an untracked local file and set the VM GUID/name and guest identity/workspace. `Get-VM | Select-Object Name, Id, State` is read-only. VM tools work while the guest is Off and do not require SSH or a model key. Hyper-V-reported addresses can be empty or stale.

### Guest desktop and commands

1. Separately install and verify Windows OpenSSH Server and Cua Driver (protocol baseline 0.38.0). Run Driver in the registered user's logged-in interactive desktop, not only SSH/Session 0. Elevated targets may need compatible Driver privileges.
2. Copy the packaged `scripts` directory into the guest. Locate it with `python -c "import acma_controller,pathlib; print(pathlib.Path(acma_controller.__file__).parent/'scripts')"`.
3. Locally elevate as the intended guest user and run `prepare_guest.ps1` with the actual VM ID, installed Driver path and independently verified SHA256. It creates protected bridge files/binding/workspace only; no downloads, SSH changes, passwordless-login changes, daemon launches or binding overwrite.
4. Configure a dedicated SSH public key with `restrict` and a forced command invoking `guest_cua_ssh_entry.ps1` through Windows PowerShell. Quote paths correctly; a protected path without spaces simplifies review. Never use a user-writable elevated entry point. The command runner creates scheduled tasks as this guest account and requires the corresponding permission.
5. Independently verify the guest SSH public host key. Place a real copy of `examples/connection.example.json` at `<private-data>/connection/connection.json`. All private-key/known-hosts/SSH-config files must be inside that private connection directory. Keep the private key owner-readable only.
6. Pin `known_hosts` to exactly `acma-<lowercase-VM-ID> ssh-ed25519 <verified-key>`. `ssh_config` may be empty. The `host_key` JSON field is the key type and key content without a comment.
7. Start with read-only calls, then dedicated test windows. No lock-screen bypass or authentication changes are provided.

See [Microsoft's SSH configuration](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh-server-configuration) and [Cua documentation](https://cua.ai/docs/cua-driver). Reconnection first verifies the configured address; bounded automatic discovery supports Hyper-V Default Switch neighbors only and never replaces SSH/guest identity pins. Custom switches require explicit addresses.
