# API v1 / 接口 v1

Default: `http://127.0.0.1:8767`. All requests require `Authorization: Bearer <ACMA_CONTROLLER_TOKEN>`. No CORS or remote binding. HTTP 200 means request handling succeeded; inspect the enclosed execution state.

默认仅本地监听、每个请求都需要令牌。HTTP 200 不等于动作或用户目标成功。

| Method | Path | Meaning / 含义 |
| --- | --- | --- |
| GET | `/v1/state` | Mode and unresolved operations / 模式与未决操作 |
| GET | `/v1/tools` | Tool descriptions and argument schemas / 工具目录与参数 |
| GET | `/v1/operations/<id>` | Existing guest action or VM receipt, no replay / 原操作回执 |
| GET | `/v1/captures/<action-id>` | Registered successful PNG only / 已登记截图 |
| POST | `/v1/control` | `pause`, `resume`, `stop`: admission, not VM power / 控制准入，不是电源 |
| POST | `/v1/call` | Advertised tool invocation / 调用工具 |

## Requests / 请求

```json
{"request_id":"resume-001","command":"resume"}
```

```json
{"request_id":"read-vm-001","tool":"vm_status","arguments":{}}
```

```json
{"request_id":"windows-001","tool":"list_windows","arguments":{"app":""}}
```

```json
{"request_id":"window-001","tool":"get_window_state","arguments":{"pid":1234,"window_id":5678,"tree_format":"elements","include_screenshot":true}}
```

```json
{"request_id":"click-001","tool":"click","arguments":{"pid":1234,"window_id":5678,"element_token":"<fresh token from that window>"}}
```

Pixel actions use **original capture pixel coordinates** and a separate fresh `capture` observation action ID. They are not normalized 0–1000 or window-relative screenshot coordinates. Use `drag` with `scope:"desktop"` for native desktop input; PID/HWND still ground the observed target. `set_value` is not exposed.

像素操作用原始截图像素坐标，外层提供 `capture` 观察编号；不能直接传归一化或窗口局部坐标。桌面拖动使用 `drag` 与 `scope:"desktop"`。

```json
{"request_id":"drag-001","tool":"drag","capture":"<observation action id>","arguments":{"pid":1234,"window_id":5678,"scope":"desktop","from_x":100,"from_y":200,"to_x":400,"to_y":200,"duration_ms":800}}
```

```json
{"request_id":"command-001","tool":"run_command","arguments":{"script":"Get-ChildItem -LiteralPath . | Select-Object Name","timeout_ms":10000,"output_limit":8192}}
```

Guest PowerShell has the guest account's permissions; the starting directory is not a sandbox. Python integrators can submit an explicit task ID via `controller.submit(spec, task_id)` and set queue deadlines via `controller.store.set_task_deadline(task_id, unix_timestamp)`. Planning and countdown UI are outside this repository.

客户机命令的工作目录不是沙箱；Python 集成方可设置任务编号和队列截止时间。规划和倒计时 UI 不在此仓库。

## Results / 结果

POST returns `request_id` and `result`. Guest calls return queue rows (`id`, `state`, `spec`, `result`); writes may include `post_readback`. VM calls return `operation_id`, `state`, identity, time and before/after evidence. Evidence remains backend-specific instead of pretending all platform effects are equally observable.

| State | Interpretation / 解释 |
| --- | --- |
| queued / running | Query original ID / 尚未结束，查询原编号 |
| succeeded | Adapter execution condition confirmed, not semantic goal completion / 执行条件已确认，不等于目标完成 |
| unverified | Delivery/effect insufficiently confirmed / 投递或效果未充分确认 |
| refused | Execution guards rejected request / 执行检查拒绝 |
| unknown | May have executed; reconcile and never automatically repeat / 可能已执行，须对账 |
| failed / cancelled / stopped / timed_out | Inspect receipt and dispatched evidence / 检查具体回执与派发事实 |

`post_readback.verification = "observation_only_not_goal_completion"` means a fresh window was read, not that a form was submitted, a message sent or a file saved. UIA/screenshots cannot guarantee universal control readability or lack of occlusion.

## Idempotency and waiting / 防重与等待

- POST requires a stable `request_id`. Identical reuse returns the stored response only; different arguments are rejected. Pending/interrupted requests are never replayed. A new request ID is a new command, not a safe retry.
- `wait_seconds` (default/max 120) bounds waiting for a guest result, not the life of an already dispatched command. Query `id` when `wait_expired` is returned.
- `vm_operation_status` uses its own 20-second bounded wait. Pausing stops waiting and future writes, not an already accepted Hyper-V operation.
- Startup is paused. The first guest operation after resume/lifecycle change performs fresh transport checks. An offline guest is an unsent refusal; host VM tools remain available.
- Unknown guest actions block mutations. Python operators can explicitly reconcile using `controller.store.resolve(id, outcome, evidence)` after checking evidence. There is no model tool to guess away unknown state.

同编号防重不是“相同参数永远只执行一次”。HTTP 超时后不要生成新编号盲目重发；暂停不会撤回已派发动作。未知状态必须通过真实证据对账。

## Compatibility / 兼容范围

`0.1.0` uses `/v1`; retrieve actual tool definitions from the running service. Future incompatible wire changes use a new API version. Core APIs remain experimental. One controller process owns one VM. No model-provider, planning, persona, memory or chat-history endpoints are included.

## Batch and view tools / 批量与截图视图

```json
{"request_id":"batch-001","tool":"execute_batch","arguments":{"actions":[{"tool":"find_elements","arguments":{"pid":1234,"window_id":5678,"query":"Search"}},{"tool":"click","arguments":{"pid":1234,"window_id":5678,"element_token":{"$result":0,"path":"/matches/0/element_token"}}}]}}
```

Earlier results use zero-based indices and JSON pointers. A missing/ambiguous result must be handled by the caller; never guess an index from an incomplete search. Every resolved child still passes the normal tool and stale-reference guards. A batch is not a transaction: inspect individual steps and continue from observed state, not by replaying the whole batch.

引用索引从0开始，路径为JSON pointer；动态引用不会绕过子工具校验。一次匹配多项不代表第一个一定正确，调用方必须根据目标选择。批次非事务，部分完成后不能整批重发。

```json
{"request_id":"view-001","tool":"screenshot_view","arguments":{"capture":"<registered-source-action-id>","region":[100,200,500,600],"max_width":1280,"max_height":960}}
```

```json
{"request_id":"view-click-001","tool":"click","capture":"<same-source-action-id>","arguments":{"pid":1234,"window_id":5678,"coordinate_space":"view_pixels","view_id":"<view-id>","x":50,"y":20}}
```

The crop above maps view origin to source pixel (100,200); when unscaled, view (50,20) maps to source (150,220). Tool guards still require the source capture to be fresh and belong to that PID/HWND. Views neither recapture the desktop nor renew freshness. Without a view, captured window coordinates are relative to the original window screenshot, and desktop coordinates to the original full-desktop screenshot; the native adapter verifies the mapping to physical pixels. Normalized 0–1000 coordinates are not accepted by this SDK.

局部图本身不是新捕获，不延长源截图有效期；窗口坐标相对窗口原图、桌面坐标相对桌面原图，物理屏幕换算由已有适配器验证。归一化坐标不是本SDK接口。
