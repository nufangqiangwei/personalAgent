# Runtime adoption evidence (T021)

记录日期：2026-10-19（Asia/Shanghai）。

## 结论

**未通过 / 未运行。** 真实模式保持禁用：`--mode live` 在
`PERSONAL_AGENT_RUNTIME_ADOPTED != 1` 时直接拒绝启动；适配器
`LiveRuntime.capabilities()` 在缺少证据时把所有能力位报告为 `false`，
`execution_available=false`，不会创建 session、不会调用供应商。

Mock 模式的全部离线测试通过，这只证明适配器边界与业务契约，**不构成**真实能力证据。

## 候选依赖

| 包 | 版本 | 状态 |
|---|---|---|
| `deepseek-harness-sdk` | 0.2.1rc2 | 未安装（`--extra live` 未执行） |
| `deepseek-harness-runtime-bin` | 0.2.1rc2 | 由 SDK 传递依赖，未安装 |

轮子指纹见 `docs/dependency-baseline.md`。

## 必须通过的检查（全部为真才算采用）

采用记录写入 `docs/runtime-adoption.json`，字段 `adopted: true` 且下列
`checks` 全为 `true` 才会被 `adoption_evidence()` 接受：

| 检查 | 说明 | 结果 |
|---|---|---|
| `windows_start` | Windows x64 上 Proactor 策略下可启动 bundled runtime | 未运行 |
| `wheel_interfaces` | 实际 wheel 的 capabilities/create_session/submit/events/wait_result/request_stop/reconcile/restore_session/dispose 与端口映射一致 | 未运行 |
| `tool_scope_denied` | 受限 profile + guard 拒绝 scope 内后注册的越权工具 | 未运行 |
| `gateway_no_bypass` | 所有模型请求（含压缩/澄清/重试）经唯一本地网关，无 bypass | 未运行 |
| `events_redacted` | 事件白名单与脱敏在落盘前完成 | 未运行 |
| `no_reasoning_persisted` | 供应商持久日志不含逐字内部推理；否则改用无持久化 profile | 未运行 |
| `unknown_blocks` | 断线/超时进入 UNKNOWN 并跨重启阻塞，仅可信证据解锁 | 未运行 |
| `restore_missing_session_fails` | 缺失/不兼容 session 不被冒充恢复 | 未运行 |

记录格式：

```json
{
  "adopted": true,
  "checked_at": "2026-10-19T12:00:00Z",
  "versions": {"sdk_version": "0.2.1rc2", "runtime_version": "0.2.1rc2", "platform": "windows-x64"},
  "profile": {"name": "personal-agent-restricted", "hash": "sha256:..."},
  "supports_resume": false,
  "tool_policy_enforced": true,
  "model_gateway_enforced": true,
  "persistence_mode": "none",
  "checks": {
    "windows_start": true,
    "wheel_interfaces": true,
    "tool_scope_denied": true,
    "gateway_no_bypass": true,
    "events_redacted": true,
    "no_reasoning_persisted": true,
    "unknown_blocks": true,
    "restore_missing_session_fails": true
  }
}
```

## 运行方式（需用户显式授权与费用确认）

```powershell
uv sync --project code/backend --python 3.12 --extra live
$env:PERSONAL_AGENT_LIVE = "1"
uv run --project code/backend python -m pytest code/backend/tests/live -m live --live
```

未获得授权前不得运行，也不得把 Mock 结果写成采用证据。若任一检查失败：
修正设计或保持 Mock 开发路径，真实模式继续禁用，不得发布为"真实模式可用"。

## 取消与恢复边界

协议未提供 `session/cancel` 或 `session/close`，关闭本地进程不构成远端终止证据，
因此适配器只做协作式停止（撤销新工具/模型权限并返回 `requested`），
`remote_cancel_verifiable` 永远为 `false`；迟到结果只能追加隔离事实，不能改变终态。
