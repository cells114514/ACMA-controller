# Security and operating boundaries / 安全与运行边界

- The HTTP server binds only to `127.0.0.1` and requires a bearer token for every endpoint. Browser-origin requests are rejected. It is not an Internet-facing service. / 服务仅监听回环地址，每个接口都需要令牌，拒绝浏览器跨源请求，不是公网服务。
- One VM is explicitly registered per process. SSH uses a pinned host key and verifies VM ID, guest computer and user SID on every response. / 每进程绑定一个 VM，SSH 固定主机密钥并核对响应身份。
- The host executes only the packaged fixed Hyper-V adapter. `run_command` executes PowerShell inside the guest, not on the host. The workspace sets the initial directory; **it is not a PowerShell security sandbox**. Guest commands have the guest account's permissions. / 宿主只执行内置 Hyper-V 适配器；客户机命令的工作目录不是安全沙箱，其权限等于客户机账户权限。
- The application denylist blocks selected input/launch operations; it does not redact observations or restrict the capabilities of guest PowerShell. / 应用黑名单不隐藏读取结果，也不限制 PowerShell 能力。
- Never retry unknown results. Keep stable HTTP request IDs; a lost response may follow a successful action. Reconcile the operation receipt instead of sending a new mutation. / 未知结果不要重试，回执丢失不代表执行失败。
- Checkpoint restore can discard newer guest data. Restore only on an explicit owner request, only while Off. There is no force-off/reset tool. / 恢复检查点可能丢弃新数据，只能由明确请求触发且 VM 必须已关闭。
- During Windows updates, observe only. The controller cannot reliably infer all update states from Hyper-V power status; the calling agent/operator must establish that updates have ended before lifecycle changes. / 系统更新期间只读；仅靠电源状态无法可靠识别所有更新阶段。
- Keep configuration, key material, screenshots, SQLite databases and logs outside Git. Do not expose bearer tokens in commands pasted into issues. / 配置、密钥、截图和日志不要提交或贴到公开问题中。
- Report security issues privately to the repository owner through GitHub before publishing exploitable details. / 安全问题请先通过 GitHub 私下联系维护者。
