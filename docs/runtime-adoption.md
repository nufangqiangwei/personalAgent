# Runtime adoption evidence (T021)

记录日期：2026-10-20（Asia/Shanghai）。最新结果见本文件末尾“CLI 接线候选采用复验”。

## 结论

**未通过。** 2026-10-20 固定安装包采用检查为 3 PASS / 1 FAIL，正式网关和受管工具桥接尚未挂载。真实模式保持禁用：`--mode live` 要求当前 CLI/profile 指纹匹配的 v2 证据，单设
`PERSONAL_AGENT_RUNTIME_ADOPTED=1` 不再足以通过 Python 启动门禁；适配器
`LiveRuntime.capabilities()` 在缺少证据或实际 binding 时把所有能力位报告为 `false`，
`execution_available=false`，不会创建 session、不会调用供应商。

Mock 模式的全部离线测试通过，这只证明适配器边界与业务契约，**不构成**真实能力证据。

**2026-10-20 新载体决定**：主入口固定为 headless CLI，见 [使用约定](harness-cli-usage.md)。独立 CLI 两次真实模型探针已通过，但下列项目采用检查仍未通过；没有生成 adopted=true 的记录，项目真实模式仍禁用。旧 SDK/wheel 检查表和 JSON 示例是现有门禁格式，不可用 CLI 小探针直接勾选；T121/T129 须同步版本化采用记录及 CLI 协议语义后，才可启用。

## 候选依赖

| 包 | 版本 | 状态 |
|---|---|---|
| `deepseek-harness-sdk` | 0.2.1rc2 | 2026-10-20 已经 uv frozen 安装；未通过采用门禁 |
| `deepseek-harness-runtime-bin` | 0.2.1rc2 | 已安装；未启用真实 Loop |

轮子指纹见 `docs/dependency-baseline.md`。

2026-10-20 用户已授权需要时使用当前 DeepSeek Harness 实例，模型、付费搜索及重试合计不超过人民币 25 元。现有实例包版本为 0.2.1-rc.2，监听 127.0.0.1:8730；根路径未认证 HTTP 探测为 401，未发送 prompt，计费请求 0、费用 0 元。SDK 安装和版本相同不能替代 provider/guard/无旁路等能力证据，真实适配器及受限 profile 接线仍待实现。

## 历史 SDK 记录格式（已废弃，不再接受）

以下为此前 SDK/wheel 时代的检查及记录示例，保留以解释历史；当前 `adoption_evidence()` 拒绝这份旧结构，不得手工复制示例作为采用结果。当前检查名与 v2 约束见文末。

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
$env:PERSONAL_AGENT_LIVE = "1"
$env:UV_OFFLINE = "1"
$env:UV_NO_SYNC = "1"
# 先设置 PERSONAL_AGENT_DSH_CLI_PATH 为已安装且校验版本的 CLI 入口，构建策略插件。
npm run build --prefix code/runtime-policy
uv run --project code/backend --python 3.12 python -m pytest code/backend/tests/live/test_runtime_adoption.py --live -q -s --tb=short
```

未获得授权前不得运行，也不得把 Mock 结果写成采用证据。若任一检查失败：
修正设计或保持 Mock 开发路径，真实模式继续禁用，不得发布为"真实模式可用"。

## 取消与恢复边界

协议未提供 `session/cancel` 或 `session/close`，关闭本地进程不构成远端终止证据，
因此适配器只做协作式停止（撤销新工具/模型权限并返回 `requested`），
`remote_cancel_verifiable` 永远为 `false`；迟到结果只能追加隔离事实，不能改变终态。

## 2026-10-20 独立 headless CLI 探针（不等于采用）

用户确认 Web 中保存的是 API Key。默认 headless 在隔离 home 下返回 MISSING_CREDENTIAL；仅启用本次调用的 Harness 凭据服务并引用已有存储位置后，两次请求返回 CLI_SMOKE_OK、退出 0。第二次用量为 313 非缓存输入 + 1280 缓存输入 + 7 输出，合计 1600 tokens，工具/thinking/持久会话日志均为 0，Web 原进程保持运行。未读取或导出 Key、未使用网页 token。

见 [CLI 使用约定](harness-cli-usage.md) 及 [机器可读证据](audits/2026-10-20-headless-cli-smoke.json)。费用不是 0：平台账单未核验，第一次按上界保留 2.01 元、第二次按全部输入未命中缓存的峰值价估计 0.003242 元，保守占用 2.013242 元，小于用户 25 元测试总授权。npx 包装器存在缓存/权限失败，成功探针采用已安装且校验版本的本地入口，仍有非致命 EPERM 诊断。

正式项目的 CLI transport、受限工具、统一网关、停止/核验和恢复未完成，不以本次直连诊断代替完整采用或两流程交付验收。

## 2026-10-20 CLI 接线候选采用复验（最新）

本轮新增 `cli.py` 的固定 Node24/CLI 版本校验、stdin、隔离 home/workspace、环境白名单、并行消费 stdout/stderr、流量上界及严格 JSON 完成判定；`cli_runtime.py` 映射本地 session、accepted、后台结果、事件游标、停止隔离、核验和销毁。输出还须通过本地 JSON Schema；业务语义仍由引擎校验。无持久化 profile 不提供原 Session 恢复。`LiveRuntime` 已委托该端口映射，但默认 composition 尚未提供正式 provider/tool binding，因此仍拒绝真实执行。

先有失败回归，再修复官方 guard 的两个实际不兼容：`ToolExecution.name` 而非 `tool`；返回拒绝理由字符串而非布尔值。`restrict` 必须挂到 Agent scope，不能挂全局。实际安装的 Cordis/ToolRuntime 验证同 scope 后注册 `auto_fix` 被拒绝且执行体未进入，允许工具正常执行。该单独工具服务验证不代表它已与生产 Loop/MCP 组合挂载。

| 检查 | 结果与边界 |
|---|---|
| 固定安装的官方 Loop / CLI 端口 / exit 0 / 缓存用量 | PASS，模型观测由本地 `llm/stream` fixture 提供，未调用供应商 |
| scope 内后注册越权拒绝 | PASS，真实 Cordis 与 ToolRuntime，包含允许工具的正例 |
| 完成释放 / 超时 UNKNOWN 跨新 Database 实例 / 无证据不解锁 | PASS，实际 CLI 子进程接入临时数据库中的测试 transport；不代替正式全模型桥接 |
| 白名单事件 / 不保存原始输出 / 已知 jsonl 会话文件检测 | PASS，注入的内部推理只在子进程内存及丢弃流中；关闭持久化并在清理 home 前检查，不靠删除后文件不存在倒推 |
| 原 Session 恢复 | 缺失即 SESSION_MISSING，需显式新 attempt/Session；不使用 `--session-id` 冒充恢复 |
| 正式 provider→ModelGateway 与受管工具/MCP | **FAIL**，`build_context(live)` 能诊断但仍返回禁用能力及 UnavailableToolService |
| 所有模型生成/压缩/重试无旁路 | **NOT RUN**，须先挂载正式桥接，不能凭 fixture 或 capability 声明通过 |

完整候选命令实际结果为 **3 PASS / 1 FAIL，11.24 秒，退出 1**。失败保留为普通断言，未 skip/xfail。默认不带双 live 开关时该文件 **4 SKIP**。审计见 [CLI 候选采用证据](audits/2026-10-20-cli-adoption-candidate.json)；测试先把白名单报告写到 pytest 临时目录，再经审阅保存本文件引用的审计副本，不自动改生产采用状态。早期一次报告直写 docs 遇 PermissionError 已改成该输出方式；没有改 ACL。

当前证据结构为 `schema_version=2`、`carrier=headless-cli`；`versions` 精确为 `sdk_version=not-used/runtime_version=0.2.1-rc.2/platform=windows-x64`，同时匹配 `profile_sha256` 与 `cli_fingerprint`。必须检查 `windows_start/cli_protocol/cli_exit_code/tool_scope_denied/gateway_no_bypass/events_redacted/no_reasoning_persisted/unknown_blocks/trusted_completion_unlocks/restore_missing_session_fails/gateway_binding_mounted/managed_tools_mounted` 全为布尔 true。未知或 null 均不接受；旧 `wheel_interfaces` 不转换为 CLI 通过。CLI 指纹当前绑定入口、包清单及 Node 版本，完整依赖闭包/分发指纹仍属 T134/T136，不宣称已覆盖。采用测试仍需补全正式无旁路与真实供应商验证，尚无成功生产记录写入路径，也没有生成 `docs/runtime-adoption.json`。

T021/T104/T121/T129/T100/T132 保持未勾选，总任务仍 94/138；按 implement 非并行门禁失败规则停止后续实施。下一步须挂载实际 `llm/stream` provider→ModelGateway，逐次绑定 owner/attempt/epoch/预留及结算、撤销权限并保留 UNKNOWN；装配 Brave/httpx/Playwright/提取正文与受管 MCP，先持久化 SessionLink，再复跑完整采用。不能把整个 Loop 外包一个模型请求当作逐模型纳管。

本轮固定观测通过的真实依赖测试发送供应商请求 **0 次**，新增付费 **0 元**；原两次付费探针的保守占用 **2.013242 元** 原样继承，总授权 **25 元**，余量上界 **22.986758 元**。没有读取凭据存储、重启 Web 或重复索要已有授权；8730 仍由 PID26948 监听。没有新迁移或锁文件变更，未执行用户数据库迁移，未提交或发布。

## 2026-10-20 收尾：本机模型网关代理与真实 CLI 桥接（采用仍未通过）

本轮把上一节列出的“实际 provider→ModelGateway”做成可执行代码，但**没有**完成 `lifespan` 正式装配与受管工具，因此采用记录仍为 `adopted=false`，真实模式继续禁用。

新增/修改：

| 文件 | 作用 |
|---|---|
| `code/backend/src/personal_agent/adapters/harness/model_proxy.py` | 仅回环、按 attempt 令牌绑定的 Messages 协议代理；每次 CLI 模型请求先占持久槽，再以单次非流式请求转发供应商，成功回复重建为 Anthropic 事件流；失败/超时保留 UNKNOWN，不回显供应商错误正文 |
| `code/backend/src/personal_agent/adapters/harness/model_provider.py` | 唯一持有供应商地址与凭据的客户端（`PERSONAL_AGENT_MODEL_API_KEY` / `PERSONAL_AGENT_MODEL_BASE_URL`）；用量含缓存读写；非 2xx/超时只返回白名单错误 |
| `code/backend/src/personal_agent/adapters/harness/model_gateway.py` | 抽出 `exchange()`，复用占槽/超时/UNKNOWN/可信结算；`request()` 行为不变 |
| `code/backend/src/personal_agent/adapters/harness/patch.py` | 生成受限 headless patch：provider `baseURL` 指向本机代理、`apiKeyEnv` 为每 attempt 令牌、禁用持久化/压缩/重试/默认工具；地址与令牌只经每 attempt 环境传入 |
| `code/backend/src/personal_agent/adapters/harness/cli.py` | 任务环境白名单；白名单外的变量（如供应商 Key）直接 `RUNTIME_ENV_REJECTED` |
| `code/runtime-policy/src/{index,guard}.ts` | 修复两个真实不兼容，见下 |

运行真实 CLI 时发现并修复的两个插件缺陷（此前 fixture 复刻了错误假设）：

1. 固定版本 `agent/created` 的载荷是 `{agent, source, signal}`，不是 agent 本身；旧代码读取 `payload.ctx.tools` 直接崩溃。
2. `tools.restrict()` 拒绝尚未注册的全局工具名，空工具表会让 CLI 启动失败。现在只收紧**当前已存在**的 allowlist 名称，`guard` 仍拒绝所有越权名（含随后在同 scope 注册的工具），删除工具时也不放松。

验证（均为固定本机观测/本机 provider，供应商请求 0 次、新增付费 0 元）：

| 检查 | 结果 |
|---|---|
| 真实 CLI 经代理完成一次模型调用 `tests/live/test_cli_gateway_bridge.py` | **1 PASS，4.64 秒**；官方 Loop 实际经本机代理，网关记录 1 个 COMPLETED、13 tokens |
| 代理契约 `tests/contract/test_model_proxy.py` | 6 PASS：令牌/撤销、单槽并发 409、SSE 结构、结算用量、失败保留 UNKNOWN、模型越权 403 |
| 任务环境白名单 | 1 PASS：仅允许网关地址/令牌/profile 路径 |
| `code/scripts/check.ps1` | **PASS，7 组检查整体退出 0**；后端 342、前端 47、策略 20 |
| ruff / mypy | PASS / PASS，108 个源码文件 |
| 采用门禁复跑 | **3 PASS / 1 FAIL，15.09 秒**；失败仍为 `gateway_binding_mounted`、`managed_tools_mounted` 与未运行的 `gateway_no_bypass` |

仍未完成，不能勾选 T021/T121/T129：

- `bootstrap/lifespan.py` 尚未装配代理、`CliRuntime` 的每 attempt 令牌与真实 transport；当前 `build_context(live)` 仍以禁用能力启动。
- 受管 MCP 工具（Brave/httpx/Playwright/提取正文）尚未挂载；`ToolService` 的 scope 令牌还未绑定到 CLI 的 AgentTask 工具调用。
- `gateway_no_bypass` 仍需在正式装配后对所有生成/压缩/重试逐一验证，不能凭代理单测或 capability 声明通过。
- `docs/runtime-adoption.json` 未生成；没有任何 adopted=true 记录。

三个锁文件指纹与本轮前一致，无新迁移、未改依赖、未提交或发布。
