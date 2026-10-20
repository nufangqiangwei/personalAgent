# 一期真实交付验收（T100）

- 记录时间：2026-10-20（Asia/Shanghai）
- 状态：**未运行（NOT RUN）**
- 结论：**一期真实模式交付未验收。** 本文件当前不能作为“真实模式可用”的证据；
  在 `code/backend/tests/live/test_phase1_delivery.py` 真实运行通过并在此记录结果之前，
  一期发布不得宣称真实链路已完成。

### 2026-10-20 本次授权更新

用户已显式授权需要时调用当前启动的 DeepSeek Harness 实例，并确认本次测试的
**模型与付费搜索合计费用上限为人民币 25 元**。重试同样计入此总额，不能按用例重置。
先前阶段真实请求为 0；其后已完成两次独立 headless CLI 小探针，均得到真实模型回复。该探针不是本文件的两个完整流程验收；用量、未核验账单及保守预留见 [CLI 使用约定](harness-cli-usage.md)。下面原有“尚未获授权”的说明仅代表此前记录，不再是当前阻断。真实适配器、采用证据和完整交付链路仍须实际实现并验证。
在能可靠限制最坏费用前不发送计费请求；额度授权不构成采用门禁已通过的证据。

## 1. 为什么未运行

1. 用户授权与费用范围已更新：需要时使用当前实例，真实测试合计不超过人民币 25 元；此项已不再阻断；
2. 项目真实网关/provider 与搜索凭据尚未装配；独立 CLI 能通过 Harness 自有凭据服务使用 Web Key，不代表项目网关已完成接入；
3. T021 真实运行时采用门禁尚未通过：
   - `docs/runtime-adoption.json` 不存在，`adoption_evidence()` 返回 `None`；
   - `--mode live` 在 `PERSONAL_AGENT_RUNTIME_ADOPTED != 1` 时被
     `bootstrap/run.py` 直接拒绝；
   - 固定 SDK/runtime 现已安装，真实接线及采用仍未通过；
4. 通过阅读当前实现确认，live 服务链路目前**无法启动或完成交付**（详见 §2），
   因此即使现在打开开关也只会失败，而不会产生可记录的真实证据。

## 2. 当前实现事实（阻断项）

以下均为代码事实，不是运行结果：

| 阻断 | 位置 | 说明 |
|---|---|---|
| 模型 transport 不可用 | `adapters/harness/runtime.py` 的 `LiveRuntime.model_transport` | 无条件抛 `RUNTIME_INCOMPATIBLE`；`build_context()` 在 live 模式构造 `ModelGateway` 时会读取它，因此 live 链无法建立 |
| 真实 session 未实现 | `adapters/harness/runtime.py` 的 `LiveRuntime.create_session` | 即使有采用证据也抛 `RUNTIME_ADAPTER_INCOMPLETE` |
| 真实工具不可用 | `bootstrap/lifespan.py` | live 模式的 `tools` 是 `UnavailableToolService`，`ToolTask`（技术选型流程的 `fetch_pricing`）无法执行 |
| 采用记录缺失/路径不一致 | `adapters/harness/runtime.py` | `adoption_evidence()` 实际读取 `code/backend/docs/runtime-adoption.json`（`ADOPTION_RECORD`），而 `docs/runtime-adoption.md` 写的是 `docs/runtime-adoption.json`；运行前必须放在代码实际读取的位置，或先修正该路径 |
| SDK 安装状态（已解除） | `pyproject.toml` 的 `live` extra | 2026-10-20 已安装固定 SDK/runtime 0.2.1rc2；可 import 不等于真实执行能力已采用 |

## 3. 运行前必须完成的配置

1. **用户已显式授权 live 执行，本次合计上限人民币 25 元**；仍需执行代码保证最坏费用预留。
2. **T021 采用门禁通过**：`code/backend/docs/runtime-adoption.json`（`ADOPTION_RECORD`）
   含 `"adopted": true`、全部 `checks` 为 `true`，并记录版本、profile 与能力位。
3. **安装锁定依赖**：

   ```powershell
   uv sync --project code/backend --python 3.12 --extra live
   ```

4. **配置凭据（只允许环境变量或系统凭据存储）**：

   | 变量 | 用途 |
   |---|---|
   | `PERSONAL_AGENT_LIVE` | 必须为 `1`；conftest 只在它和 `--live` 同时存在时执行 live 用例 |
   | `PERSONAL_AGENT_RUNTIME_ADOPTED` | 必须为 `1`；生产入口 `bootstrap.run --mode live` 也以此放行 |
   | `PERSONAL_AGENT_MODEL_API_KEY` | 模型 provider 凭据（`bootstrap/credentials.py` 的 `model`） |
   | `PERSONAL_AGENT_BRAVE_API_KEY` | 搜索 provider 凭据（`bootstrap/credentials.py` 的 `search`） |
   | `PERSONAL_AGENT_MODEL` | 可选，模型 id；默认 `deepseek-v4-flash` |
   | `PERSONAL_AGENT_LIVE_TIMEOUT_SECONDS` | 可选，单流程最长等待秒数；默认 `900` |

   凭据不得写入源码、测试夹具、日志、错误响应或提交历史。系统凭据存储入口见
   `bootstrap/credentials.py` 与 `/settings/credentials`；本用例不读取密钥内容。

## 4. 费用范围（运行前必须确认）

两个种子流程各受自身 `execution_policy` 限制：

| 维度 | 每流程上限 | 两流程合计上限 |
|---|---|---|
| `max_model_requests` | 30 | 60 |
| `max_tokens` | 100000 | 200000 |
| `max_tool_calls` | 60 | 120 |
| `max_search_calls` | 20 | 40 |
| `max_page_reads` | 40 | 80 |

运行时会话内部的压缩/重试也必须经过同一 `ModelGateway` 并计入预算。用户已确认本次
所有测试合计人民币 25 元；实际采用的请求/token/搜索范围必须收紧到此金额内，不能
因上表默认资源上限更高而扩大金额授权。独立 CLI 小探针的两次请求与保守费用预留同样计入该总额，不能清零后另给交付验收 25 元。

## 5. 运行命令（显式 live 授权后）

```powershell
# 1) 安装锁定依赖
uv sync --project code/backend --python 3.12 --extra live

# 2) 设置授权、采用记录与凭据（凭据只来自环境/系统凭据存储）
$env:PERSONAL_AGENT_LIVE = "1"
$env:PERSONAL_AGENT_RUNTIME_ADOPTED = "1"
$env:PERSONAL_AGENT_MODEL_API_KEY = "<provider credential>"
$env:PERSONAL_AGENT_BRAVE_API_KEY = "<search credential>"
$env:PERSONAL_AGENT_LIVE_TIMEOUT_SECONDS = "900"

# 3) 运行 T100 验收（会真实访问 provider 与公开网页）
uv run --project code/backend --python 3.12 pytest `
  code/backend/tests/live/test_phase1_delivery.py -m live --live -ra
```

说明：该模块通过应用自身的 HTTP 端点（与 UI 使用同一服务链路）驱动两个种子流程；
不含浏览器自动化。需求字段由服务端 `RequirementService` 依据用户明确输入生成，草稿、
摘要哈希、确认与启动都走真实端点；聊天抽取链路由 T025/T044 的用例覆盖，不纳入本门禁，
以免用模型抽取的随机性替代交付判据。前端 E2E 不能替代本验收，本验收也不以 Mock 运行替代。

## 6. 未授权时的默认验证（必须仍是 skipped）

```powershell
uv run --project code/backend --python 3.12 pytest `
  code/backend/tests/live/test_phase1_delivery.py -q
```

预期：`3 skipped`（`live` marker 未启用），不是 error，也不得执行任何真实调用。
本轮实测结果：`3 skipped`（约 1.5s）。

## 7. 通过判据（每个流程都必须满足）

1. **流程完成**：`POST /api/v1/runs` 创建执行，运行最终为 `SUCCEEDED`，产物可读；
   `completeness` 为 `COMPLETE` 或 `PARTIAL`（实际值记录在结果中）。
2. **官方 Loop**：
   - 运行时是 `LiveRuntime` 适配器，`MockRuntime` 未参与；
   - `capabilities().sdk_version` 与 `pyproject.toml` 锁定的 `deepseek-harness-sdk`
     版本一致，`model_gateway_enforced` 与 `tool_policy_enforced` 为 `true`，
     `persistence_mode != "unknown"`；
   - 每个 `AgentTask` 至少创建一个 Harness session 并至少提交一次任务，
     所有 `AgentTask` 与 Output 步骤状态为 `SUCCEEDED`；
   - 采用记录的全部 checks 为 `true`（尤其 `wheel_interfaces`、`gateway_no_bypass`）。
3. **所有模型请求纳管**：
   - 该 run 至少有一条 `model_requests` 记录；全部为 `COMPLETED`，带
     `completion_evidence_ref`、`ended_at` 与 `actual_tokens`；
   - 网关准入的 request id 与持久化记录一致；provider transport 的每次调用都能对应到
     一条网关请求；不存在“无网关请求的 transport 调用”；
   - 运行结束后不存在 `RESERVED` / `IN_FLIGHT` / `UNKNOWN` 请求；
   - 同一时刻最多一个 transport 调用在途。
4. **真实来源**：
   - 该 run 至少一条 `sources` 记录；
   - URL 为公开 `http/https`，不是 `example.org`/`example.net`/`vendor.example` 等占位域，
     也不是 loopback/私网地址；
   - 每条来源有标题、非空 `content_hash`、`fetched_at` 落在本次 run 的时间窗内，
     且 `availability == "AVAILABLE"`。
5. **可获取 Markdown 文档**：
   - `GET /api/v1/runs/{id}/artifacts` 返回 `format == "markdown"`、`access_state ==
     "AVAILABLE"` 的产物，且出现在 run 快照的 `artifact_ids` 中；
   - `GET /api/v1/artifacts/{id}` 返回 200，`document` 含标题、全部方向与真实来源；
   - `GET /api/v1/artifacts/{id}/content` 返回 200、`text/markdown`、UTF-8，正文以
     `# ` 开头并标注“完成程度：”，`sha256` 与服务端记录一致；
   - 受管 blob 文件在数据目录中存在，且磁盘内容哈希与响应一致；
   - 文档来源中至少一个 host 与已记录的 `sources` host 相同。

任一判据失败即 T100 失败；失败原因和未满足项必须原样记录，不得改写成通过。

## 8. 结果记录规则

- 运行成功后，在本文件追加：日期、commit、`--extra live` 安装结果、两个流程的
  run id/状态/completeness、模型请求数、来源数、产物 id、失败项（若有）。
- 运行失败：状态写 **FAILED**，保留失败判据与错误码；修复后重跑，不能引用旧 Mock 结果。
- 未运行：保持 **NOT RUN**，并保留 §2 的阻断项。
- **Mock 证据不是替代品**：离线测试通过只证明边界与契约；本文件未通过前，
  一期发布不得声称真实模式完成。

## 9. 相关文件

- `code/backend/tests/live/test_phase1_delivery.py`：本验收的唯一执行入口（live marker）。
- `code/backend/tests/conftest.py`：`--live` 与 `PERSONAL_AGENT_LIVE=1` 的跳过逻辑；本模块只
  在 live 启用后临时恢复真实 socket，默认离线守卫不变。
- `code/backend/src/personal_agent/adapters/harness/runtime.py`：官方 Loop 适配器与
  `adoption_evidence()` 的实际读取路径。
- `code/backend/src/personal_agent/adapters/harness/model_gateway.py`：唯一模型网关与
  `model_requests` 持久记录。
- `code/backend/src/personal_agent/bootstrap/lifespan.py`：live 模式装配（含 §2 的阻断项）。
- `docs/runtime-adoption.md` / `docs/runtime-adoption.json`：T021 采用证据（尚未通过）。

