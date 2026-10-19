# REST / SSE / UI Contract v1

这是实施契约，非已运行API。基础路径 `/api/v1`，JSON UTF-8，UUID标识，UTC时间，未知字段默认拒绝。下列字段用 `?` 表示可空/可省略，其余必填。枚举/实体约束见 ../data-model.md。生产同源本机会话，写请求校验Origin和CSRF；不建设账号、租户或角色。

本文补齐一期四组契约：草稿额度追加（独立账本）、首次配置、备份/恢复、维护。四组均只使用一期实体与单用户工作区，不引入匹配、探索、沉淀或长期偏好实体；草稿额度追加不是运行求助，备份/恢复与维护也不自动启动或续跑研究。

## 通用约定

- 写请求使用 `Idempotency-Key`；键按工作区+路由+规范请求hash去重，重复同请求返回原资源，同键异请求409。执行启动/分支/额度追加的去重记录保留至关联业务记录删除；不能因浏览器重试产生新run或重复追加额度。
- 可变资源返回ETag=revision；更新/确认/恢复需 `If-Match`，缺失428、不匹配412。不可变版本使用definition_hash。
- 列表 `{items,next_cursor}`，limit默认20最大100；快照包含 `last_event_sequence`。分页稳定排序created_at+id。
- 首次配置完成前只开放 `/health`、`/policies`、`/setup/*`、`/settings/credentials` 和只读查询；其余写操作返回409 `SETUP_REQUIRED`。`completed` 由服务端按数据库迁移版本、数据目录可写、`execution_available` 与政策确认事实计算，客户端不能自行置真。
- 草稿额度追加使用仅 `draft_id` 非空的独立账本，必须显式调用追加端点并携带 `explicit_grant:true`；不创建run、不生成确认、不自动调用模型或启动研究，也不能用run求助的 `GRANT_AND_CONTINUE` 代替。两个账本不共享、不互转额度。
- 维护模式、备份、恢复与迁移只能由显式用户操作触发；备份在验证为可恢复前不得称为可用，恢复不得信任客户端声明的“已验证/已应用”，必须由服务端校验清单hash、目标内容和版本兼容性。
- 凭据只写不读：任何响应只返回 `configured/source/fingerprint/last_checked_at`，不得回显密钥、环境变量值或可用凭据；密钥只允许来自环境变量或系统凭据存储。
- 错误 `{error:{code,message,retryable,details,request_id}}`；details仅含可公开字段。400格式、403范围拒绝、404不存在、409状态/忙/保存冲突或重复键、410删除或过期、412旧revision、422语义失败、428缺少If-Match、503依赖不可用。所有错误均脱敏。
- 重要错误code：INPUT_INCOMPLETE、UNACCEPTED_SUGGESTION、CONFIRMATION_STALE、VERSION_CHANGED、RUN_BUSY、MODEL_BUSY、MODEL_OUTCOME_UNKNOWN、PREREQUISITE_INVALID、START_POINT_REQUIRES_CONFIRMATION、BUDGET_ASSISTANCE_REQUIRED、DRAFT_BUDGET_NOT_EXHAUSTED、DRAFT_BUDGET_GRANT_INVALID、DRAFT_BUDGET_CONFLICT、DRAFT_STATE_INVALID、SETUP_STATE_STALE、SIDE_EFFECT_UNRESOLVED、RUNTIME_INCOMPATIBLE、ARTIFACT_UNAVAILABLE、DEFINITION_INVALID、SETUP_REQUIRED、SETUP_INCOMPLETE、POLICY_CHANGED、POLICY_ACKNOWLEDGEMENT_REQUIRED、CREDENTIAL_INVALID、CREDENTIAL_UNAVAILABLE、MAINTENANCE_REQUIRED、MAINTENANCE_BUSY、MAINTENANCE_TRANSITION_BLOCKED、MAINTENANCE_REVISION_STALE、BACKUP_NOT_FOUND、BACKUP_INCOMPATIBLE、BACKUP_VERIFICATION_FAILED、BACKUP_EXPIRED、BACKUP_DELETED、MANIFEST_HASH_MISMATCH、RESTORE_NOT_FOUND、RESTORE_BLOCKED、RESTORE_CONFLICT、IDEMPOTENCY_CONFLICT。

## 接口目录

| 方法/路径 | 请求 | 成功响应和规则 |
|---|---|---|
| GET /health | 无 | 200 `{status,mode,dependency_checks,execution_available}`；不包含凭据。进程存活与真实runtime可用分开 |
| GET /policies | 无 | 200 `{version,hash,retention,budget_defaults,allowed_actions,storage_scope}`，首次保存/执行前UI展示 |
| GET /setup/status | 无 | 200 SetupStatus；返回迁移/数据目录/运行时/政策确认/凭据健康状态；不含秘密；completed由服务端核验 |
| POST /setup/acknowledgements | `{policy_version,policy_hash,data_policy_acknowledged:true,budget_policy_acknowledged:true,explicit_confirm:true}` | 200 SetupStatus；政策hash必须与当前政策一致；确认只记录ack事实，不自行置completed |
| GET /settings/credentials | 无 | 200 `{items:[{provider,configured,source,last_checked_at,error_code?}],checked_at}`；只返回健康状态和指纹 |
| PUT /settings/credentials/{provider} | `{source:"ENV"或"OS_CREDENTIAL_STORE",secret?,explicit_save:true}` | 200凭据健康；secret只写不读、不得回显或记日志；ENV来源不接受secret |
| DELETE /settings/credentials/{provider} | `{source,explicit_remove:true}` | 200 `{provider,configured:false,source}`；仅删除系统凭据存储项，不改环境变量 |
| GET /workflows | cursor?,limit? | 最新可执行版本列表，含用途/边界/必要输入/主要步骤/产出；空库items=[]，不推荐虚构工作流 |
| GET /workflows/{id} | 无 | `{id,latest_version,disabled,revision}` |
| GET /workflows/{id}/versions | cursor?,limit? | 全历史及草稿、时间、变更摘要 |
| GET /workflows/{id}/versions/{version_id} | 无 | 完整定义、hash、验证报告、可执行标识 |
| POST /workflows/{id}/versions | `{base_version_id,definition,change_summary,confirm_executable}` | 201新版本；检查base与当前编辑基线冲突409；不覆盖旧版本；无内容变化返回原版本不虚增 |
| POST /workflows/{id}/disable | `{reason}` | 200停用；保留已有运行和历史 |
| POST /requirements | `{workflow_version_id}` | 201 draft COLLECTING；仅选流不启动研究 |
| GET /requirements/{id} | 无 | draft DTO，含messages、来源、建议、缺失、冲突及完整摘要；`budget_summary`仅摘要，明细走budget端点 |
| GET /requirements/{id}/budget | 无 | 200 DraftBudgetSnapshot；草稿独立账本的limits/consumed/reserved/remaining、耗尽维度及派生求助状态 |
| POST /requirements/{id}/budget-grants | `{draft_revision,approved_increment:{model_requests?,tokens?},explicit_grant:true,acknowledge_cost_and_ledger:true,note?}` | 201 `{grant_id,ledger_scope:"DRAFT",budget,research_started:false,confirmation_id:null}`；仅触顶后显式追加；不创建run或确认、不自动研究 |
| POST /requirements/{id}/messages | `{text}` | 202 `{message_id,analysis_operation_id,status}`；需LLM时经过统一门。忙可记录消息但只作待分析；不自动启动研究 |
| GET /operations/{id} | 无 | `{id,status,result_ref?,error?}`；状态PENDING/RUNNING/WAITING_MODEL/SUCCEEDED/FAILED/UNKNOWN，仅查看不触发重试 |
| POST /requirements/{id}/suggestions/{sid}/accept | `{draft_revision}` | 200更新draft；接受建议后重新渲染摘要，仍需最终确认 |
| POST /requirements/{id}/selection | `{workflow_version_id}` | 200改选后重检，适用旧值标待重新确认，旧确认失效 |
| POST /requirements/{id}/confirm | `{draft_revision,summary_hash,workflow_version_id,budget_policy_hash,data_policy_version,action:"START"或"BRANCH",explicit_start_confirmation:true}` | 201 `{confirmation_id,confirmed_input_hash}`；必要项完整/无冲突/版本仍一致才可确认；BRANCH必须来自关联proposal的draft；仅明确用户动作调用 |
| POST /runs | `{confirmation_id}` | 201 RunSnapshot；仅START确认，事务检查确认、抢槽与创建。忙409不排队、不自动切换；双击同key同run；同confirmation不得创建两个run |
| GET /runs | root_run_id?,cursor?,limit? | 历史，含状态/实际版本/父根/产物摘要 |
| GET /runs/{id} | 无 | RunSnapshot（下述） |
| GET /runs/{id}/steps | 无 | 各节点状态、尝试历史、来源引用、可选重跑起点及阻塞说明 |
| GET /runs/{id}/history | 无 | 根/父/子分支、分叉节点、要求差异和各分支产物，不要求用户操作Git |
| POST /runs/{id}/pause | `{reason?}` | 202 PAUSING或已停状态；停止新增行动，未收敛保留draining |
| POST /runs/{id}/cancel | `{reason?}` | 202 CANCELLING或已取消状态；已取消不可恢复；未知动作不伪称终止 |
| POST /runs/{id}/resume | `{expected_checkpoint_hash,explicit_continue:true}` | 202原run；仅PAUSED/INTERRUPTED且检查通过。上限等待不能以此绕过求助，返回409 |
| POST /runs/{id}/branch-proposals | `{selected_step_id,reason}` | 201 proposal+派生draft；复制输入为待确认，不复制成功attempt |
| GET /branch-proposals/{id} | 无 | 起点/差异/受影响节点/失效成果/核验报告/状态；新draft可使用上述聊天端点修改 |
| POST /branch-proposals/{id}/validate | `{draft_revision,selected_step_id}` | 200 READY/BLOCKED及报告；确定性检查+保守影响分析，不自动前移 |
| POST /branch-proposals/{id}/confirm | `{confirmation_id,effective_step_id,report_hash,explicit_start_confirmation:true}` | 201新RunSnapshot；必须为关联draft的BRANCH确认；核验起点与输入确认、预算、副作用、槽位；改起点需显式确认；同proposal只能创建一个run |
| GET /runs/{id}/assistance | 无 | 当前及历史求助完整内容 |
| POST /runs/{id}/assistance/{aid}/decisions | `{decision,approved_increment?,continue_authorized,evidence_refs?,note?}` | 201决策及run快照；decision=DISCUSS/GRANT_AND_CONTINUE/PAUSE/CANCEL/CHANGE_REQUIREMENTS/RECONCILE；仅授权且阻塞已解才推进 |
| GET /runs/{id}/side-effects | 无 | intent、目标摘要、核验状态、证据，不暴露凭据 |
| POST /runs/{id}/side-effects/{oid}/reconcile | `{evidence_refs,decision_note}` | 200核验结果；服务端校验真实目标，不能接受客户端任意APPLIED覆盖状态 |
| GET /runs/{id}/artifacts | 无 | 文档、完整程度、实际版本、来源/缺口、可访问状态 |
| GET /artifacts/{id} | 无 | 元数据、可读Markdown或受控内容链接 |
| GET /artifacts/{id}/content | 无 | 200 text/markdown; UTF-8；hash/ETag/安全文件名，下载重试不重做研究；删除410 |
| GET /data-items | type?,cursor?,limit? | 用途/到期/关联引用/删除影响，支持用户管理保存内容 |
| POST /deletions | `{item_ids,acknowledge_resume_impact:true}` | 202删除记录；立即不可访问不可复用，异步物理清理和备份同步；处理中可轮询operations |
| GET /maintenance/status | 无 | 200 MaintenanceStatus；mode、revision、draining、in_flight_actions、unknown_model_request、pending_writes、quiescent、migration_version、allowed_actions |
| POST /maintenance/enter | `{reason,explicit_confirm:true}` | 202 ENTERING或MAINTENANCE；停止新增行动并等待在途收敛，不伪称已停止；收敛前不得备份、恢复或迁移 |
| POST /maintenance/exit | `{expected_revision,explicit_confirm:true}` | 200 NORMAL或仍被阻断；仅无恢复/迁移在途、迁移至head、数据目录可写时退出；恢复后遗留run保持INTERRUPTED |
| POST /maintenance/migrations | `{target_revision:"head",backup_id,explicit_migrate:true}` | 202维护操作；需已验证且兼容的备份、维护且静默；禁止降级，失败保留可恢复状态 |
| GET /maintenance/backups | cursor?,limit? | 备份清单；含状态、过期时间、schema/app版本、验证结果及是否可恢复 |
| POST /maintenance/backups | `{reason?,explicit_backup:true}` | 202 `{operation_id,backup_id?,status,status_url}`；仅维护且静默时创建；验证前usable=false |
| GET /maintenance/backups/{id} | 无 | BackupManifest；db revision、blob hash清单摘要、删除墓碑hash、验证报告、expires_at；过期或已删除410 |
| POST /maintenance/backups/{id}/verify | `{manifest_hash}` | 200 VERIFIED或FAILED；服务端重新核验hash及临时恢复，不接受客户端声明 |
| POST /maintenance/restores | `{backup_id,manifest_hash,explicit_restore:true,acknowledge_interruption:true,acknowledge_lifecycle:true}` | 202 `{restore_id,status,status_url}`；仅维护且静默、已验证、未过期且版本兼容；失败不改动现有数据 |
| GET /maintenance/restores/{id} | 无 | RestoreStatus；阶段、错误及post_restore（中断run数、墓碑应用数、auto_resumed:false） |

所有确认都要求UI有明确用户动作证据。聊天“知道了”、查看摘要和接受单条建议不得自动调用start/continue授权。二期matching/exploration/preferences端点不在一期实现。

## 草稿额度追加（独立账本）

草稿澄清使用只挂 `draft_id` 的独立 `BudgetLedger`，与根研究历史账本、`AssistanceRequest` 的运行求助不是同一机制。追加草稿额度不要求也不得创建 run，不得伪造成 `GRANT_AND_CONTINUE`，不得从根研究账本转入额度；根研究预算也不能用本端点绕过。额度数值只来自政策与已提交的 `BudgetEntry`/`ModelRequest`，聊天文本、模型建议、客户端声明和“知道了”均不构成授权或消耗证据。

`GET /requirements/{id}/budget` 返回 `DraftBudgetSnapshot`：

```json
{
  "draft_id": "uuid",
  "ledger_id": "uuid",
  "ledger_scope": "DRAFT",
  "revision": 4,
  "limits": {"model_requests": 20, "tokens": 30000},
  "consumed": {"model_requests": 20, "tokens": 28750},
  "reserved": {"model_requests": 0, "tokens": 0},
  "remaining": {"model_requests": 0, "tokens": 1250},
  "active_ms": 0,
  "exhausted": true,
  "exhausted_dimensions": ["model_requests"],
  "assistance": {
    "kind": "BUDGET_LIMIT",
    "status": "OPEN",
    "observed_problem": "草稿模型请求已达上限；尚未创建研究run。",
    "unknown_model_request": false,
    "available_actions": [
      {"action": "GRANT_DRAFT_BUDGET", "enabled": true, "disabled_reason": null}
    ]
  },
  "updated_at": "2026-10-19T12:00:00Z"
}
```

`POST /requirements/{id}/budget-grants` 请求：

```json
{
  "draft_revision": 4,
  "approved_increment": {"model_requests": 5, "tokens": 10000},
  "explicit_grant": true,
  "acknowledge_cost_and_ledger": true,
  "note": "继续补充研究要求"
}
```

201 响应：

```json
{
  "grant_id": "uuid",
  "ledger_id": "uuid",
  "ledger_scope": "DRAFT",
  "draft_id": "uuid",
  "draft_revision": 5,
  "granted_increment": {"model_requests": 5, "tokens": 10000},
  "budget": { "…DraftBudgetSnapshot…": "追加后的完整快照" },
  "research_started": false,
  "confirmation_id": null,
  "run_id": null,
  "allowed_actions": [
    {"action": "RETRY_ANALYSIS", "enabled": true, "disabled_reason": null}
  ]
}
```

授权与状态规则：

1. 必须使用本机会话并校验Origin/CSRF；必须携带 `Idempotency-Key` 与 `If-Match: <draft revision>`，且 header 与 body 的 `draft_revision` 一致，缺失428、不一致或过期412。无账号/角色；不得以“本机请求”为由跳过这些校验。
2. 仅 `COLLECTING`/`READY` 草稿可追加；`CONFIRMED`/`STALE` 返回409 `DRAFT_STATE_INVALID`。草稿缺少 `budget assistance` 或所有维度仍有余量时返回409 `DRAFT_BUDGET_NOT_EXHAUSTED`，不做预防性加额。
3. `approved_increment` 至少一个维度为正整数，未知维度、零、负数、非整数或超过政策单次上限均返回422 `DRAFT_BUDGET_GRANT_INVALID`；`explicit_grant` 与 `acknowledge_cost_and_ledger` 必须同时为 `true`，不得有服务端默认值。
4. 事务内对 `BudgetLedger.revision` 做CAS，追加一条 `BudgetEntry(kind=GRANT, operation_id=Idempotency-Key)`，按维度累加；`consumed`、`reserved`、`active_ms` 不变，草稿与根研究都不清零，重试上限不重置。冲突返回409 `DRAFT_BUDGET_CONFLICT` 并要求重新读取，不覆盖已提交事实。
5. 相同幂等键与相同规范请求返回同一 `grant_id` 和原响应，不重复加额；同键异请求409 `IDEMPOTENCY_CONFLICT`。同一次用户动作不得因浏览器重试产生两条GRANT。
6. 本端点只解除额度阻塞，不创建run、Confirmation、ConversationMessage，不改变选定版本、字段、缺失项或摘要hash，不自动调用模型或提交分析。`research_started` 必须为 `false`、`confirmation_id` 与 `run_id` 必须为 `null`；待分析消息仍按既有排队规则处理，用户需显式重试或继续聊天。
7. UNKNOWN模型请求不会被追加额度解除：响应仍返回 `assistance.unknown_model_request=true`，不得提供可点击的继续/重试动作，直到获得可信完成证据。追加也不得跨账本转移或为后续run预支。
8. 响应、日志和错误不得包含密钥、模型内部推理或未脱敏的聊天内容；`grant_id` 即审计用 `BudgetEntry.id`，保留至关联草稿删除。

错误：404 `DRAFT_NOT_FOUND`；409 `DRAFT_STATE_INVALID`、`DRAFT_BUDGET_NOT_EXHAUSTED`、`IDEMPOTENCY_CONFLICT`、`DRAFT_BUDGET_CONFLICT`（并发修改账本，重新读取后可用同键重试）；412 `DRAFT_REVISION_STALE`；422 `DRAFT_BUDGET_GRANT_INVALID`；428缺少If-Match；503持久化不可用。上述错误均不得创建部分额度。

## 首次配置

首次配置是本机单用户工作区的显式初始化，不创建账号、角色、租户或用户切换。完成判据固定为：数据库迁移已到最新、数据目录可写、`/health.execution_available` 为真（Mock 或已验证的真实模式）、保留/上限/预算政策已展示并由用户显式确认保存。`completed` 只能由服务端依据实时检查与 `SetupState` 计算，客户端不得通过提交 `completed:true`、仅勾选界面或“知道了”绕过。

`GET /setup/status` 返回 `SetupStatus`：

```json
{
  "required": true,
  "completed": false,
  "mode": "MOCK",
  "app_version": "0.1.0",
  "schema_version": 1,
  "migration": {"current": "0011_lifecycle", "head": "0011_lifecycle", "up_to_date": true},
  "data_dir": {"display_path": "%LOCALAPPDATA%/Personal-Agent", "writable": true},
  "execution_available": true,
  "runtime": {"mode": "MOCK", "compatible": true, "reason_code": null},
  "policies": {
    "version": "1",
    "hash": "sha256:…",
    "presented_at": "2026-10-19T11:59:00Z",
    "acknowledged": false,
    "acknowledged_at": null
  },
  "credentials": [
    {"provider": "deepseek", "configured": true, "source": "ENV", "last_checked_at": "2026-10-19T12:00:00Z", "error_code": null},
    {"provider": "brave_search", "configured": false, "source": "OS_CREDENTIAL_STORE", "last_checked_at": null, "error_code": "CREDENTIAL_UNAVAILABLE"}
  ],
  "blockers": [
    {"code": "POLICY_ACKNOWLEDGEMENT_REQUIRED", "message": "请先阅读并确认保留与上限政策", "action": "ACKNOWLEDGE_POLICIES"}
  ],
  "revision": 3,
  "allowed_actions": [
    {"action": "ACKNOWLEDGE_POLICIES", "enabled": true, "disabled_reason": null},
    {"action": "SAVE_CREDENTIAL", "enabled": true, "disabled_reason": null}
  ]
}
```

`POST /setup/acknowledgements` 请求：

```json
{
  "policy_version": "1",
  "policy_hash": "sha256:…",
  "data_policy_acknowledged": true,
  "budget_policy_acknowledged": true,
  "explicit_confirm": true
}
```

200 响应为更新后的 `SetupStatus`，并带 `policies.acknowledged_at`。规则：

1. 使用本机会话并校验Origin/CSRF；携带 `Idempotency-Key` 与 `If-Match: <SetupState revision>`，缺失/不匹配按428/412处理。
2. `policy_version`/`policy_hash` 必须与当前 `GET /policies` 一致；政策已变化返回409 `POLICY_CHANGED`，必须重新展示并由用户再次确认，不得自动沿用旧确认。
3. `data_policy_acknowledged`、`budget_policy_acknowledged`、`explicit_confirm` 必须同时为 `true`，否则422 `POLICY_ACKNOWLEDGEMENT_REQUIRED`；不提供默认确认。
4. 服务端在首次向本机会话返回 `SetupStatus` 时记录 `policies.presented_at`；确认必须晚于该展示事实。该端点只记录用户已阅读并确认政策的不可变事实，不自行把 `completed` 置真、不启用真实模式、不创建流程或run；重复相同确认幂等返回，不新增确认版本。`completed` 同时要求 `presented_at` 与 `acknowledged_at` 非空，不能只凭确认请求补出展示事实。
5. `blockers` 与 `allowed_actions` 由服务端实时计算；迁移未到head、数据目录不可写、`execution_available=false` 任一存在时即使已确认政策也保持 `completed=false`，并返回对应阻断码。
6. 已完成后若数据目录变为不可写、迁移版本不兼容或运行时失效，`required` 重新为真；写操作返回409 `SETUP_INCOMPLETE`，只读查看仍可用。

凭据接口：

- `GET /settings/credentials` → `{items:[{provider,configured,source,last_checked_at,error_code?}],checked_at}`。只返回健康状态，不返回密钥、密钥长度、环境变量值或可用于重放的指纹原文。
- `PUT /settings/credentials/{provider}` 请求 `{source:"ENV"或"OS_CREDENTIAL_STORE",secret?,explicit_save:true}`，200返回 `{provider,configured,source,fingerprint,last_checked_at,error_code?}`。`source=ENV` 时 `secret` 必须缺省/为null，服务端只检查环境变量是否可用，不写环境变量；`source=OS_CREDENTIAL_STORE` 时 `secret` 为只写字段，写入系统凭据存储，响应只返回不可逆/带密钥的指纹前缀。未知provider 422；来源与字段不匹配422 `CREDENTIAL_INVALID`；系统凭据存储不可用503 `CREDENTIAL_UNAVAILABLE`。
- `DELETE /settings/credentials/{provider}` 请求 `{source:"OS_CREDENTIAL_STORE",explicit_remove:true}`，200返回 `{provider,configured:false,source}`；只能删除系统凭据存储项，不得删除或改写用户环境变量。
- `configured=true` 只表示凭据存在且最近一次检查可用，不证明真实runtime已通过接入门禁，也不得据此把 `execution_available` 置真。密钥不得进入数据库、日志、错误、SSE、导出或备份清单。

错误：403会话/Origin/CSRF校验失败；404无此provider；409 `SETUP_REQUIRED`、`SETUP_INCOMPLETE`、`POLICY_CHANGED`；412 `SETUP_STATE_STALE`；422 `POLICY_ACKNOWLEDGEMENT_REQUIRED`、`CREDENTIAL_INVALID`；428缺少If-Match；503 `CREDENTIAL_UNAVAILABLE`或持久化不可用。错误响应只含脱敏字段，不得回显secret或环境变量值。

## 维护、备份与恢复

维护模式是应用级门禁，不是run状态，也不把“请求停止”显示成“已停止”。进入维护后停止接受新研究、新分支、run继续、草稿额度追加和业务写入动作，等待在途动作收敛；不终止无法立即停止的动作，不以关闭进程冒充远端取消。备份、恢复和迁移都只能在维护模式且静默（quiescent）时执行。所有维护写操作均使用本机会话、校验Origin/CSRF并携带 `Idempotency-Key`；不得以“本机来源”或UI已确认替代服务端校验。

`GET /maintenance/status` 返回 `MaintenanceStatus`：

```json
{
  "mode": "MAINTENANCE",
  "revision": 3,
  "reason": "升级前备份",
  "requested_at": "2026-10-19T12:00:00Z",
  "entered_at": "2026-10-19T12:00:15Z",
  "draining": false,
  "in_flight_actions": [],
  "pending_writes": [],
  "unknown_model_request": null,
  "quiescent": true,
  "migration_version": "0011_lifecycle",
  "backup_in_progress": false,
  "restore_in_progress": false,
  "allowed_actions": [
    {"action": "CREATE_BACKUP", "enabled": true, "disabled_reason": null},
    {"action": "RESTORE", "enabled": true, "disabled_reason": null},
    {"action": "EXIT_MAINTENANCE", "enabled": true, "disabled_reason": null}
  ]
}
```

`quiescent` 的判定：无在途动作、无draining写入、无待处置的 `INTENT/PARTIAL/UNKNOWN/CONFLICT` `SideEffectOperation`（`APPLIED`、`NOT_APPLIED` 是已核验终态，不阻塞）、无 `RESERVED/IN_FLIGHT/UNKNOWN` 的 `ModelRequest`。UNKNOWN请求属于未静默，不得用备份、恢复、迁移或重启绕过；只有可信完成证据或显式核验才能解除。

进入/退出：

- `POST /maintenance/enter` 请求 `{reason,explicit_confirm:true}`，携带 `If-Match: <maintenance revision>` 与 `Idempotency-Key`，202返回 `{mode:"ENTERING"或"MAINTENANCE",revision,status_url}`。停止新增行动；15秒内未收敛则保持 `ENTERING`、`draining=true` 并显示原因，不得报告已停止，也不得开始备份/恢复/迁移。已有run状态不被伪改；仅维护门禁阻止新动作。
- `POST /maintenance/exit` 请求 `{expected_revision,explicit_confirm:true}`，并携带与 `expected_revision` 一致的 `If-Match: <maintenance revision>`，200返回 `MaintenanceStatus`。只有 `MAINTENANCE` 可退出，且无恢复/迁移在途、迁移已至head、数据目录可写、无未核验写入；UNKNOWN模型请求不阻止退出，但 `execution_available` 保持false并列出阻断原因。恢复或迁移后的遗留执行保持 `INTERRUPTED`，退出维护不自动续跑，必须由用户显式继续。
- `POST /maintenance/migrations` 请求 `{target_revision:"head",backup_id,explicit_migrate:true}`，202返回 `{operation_id,status,app_restart_required:true,status_url}`。仅维护且静默、备份已VERIFIED且兼容时可请求；禁止downgrade或绕过备份；失败时从已验证备份恢复或保持维护并说明原因，不破坏性回退。

维护错误：409 `MAINTENANCE_REQUIRED`（未进入维护）、`MAINTENANCE_BUSY`（在途/未静默/已有维护操作）、`MAINTENANCE_TRANSITION_BLOCKED`（未收敛或条件不满足）；412 `MAINTENANCE_REVISION_STALE`；422 `BACKUP_INCOMPATIBLE`、`RESTORE_BLOCKED`；428缺少If-Match。进入/退出/迁移失败不得改变模式事实、不得伪造“已停止”或“已完成”，并返回具体阻断原因与允许动作。

备份：

`POST /maintenance/backups` 请求 `{reason?,explicit_backup:true}`，202返回 `{operation_id,backup_id?,status:"REQUESTED",status_url}`。规则：

1. 必须处于 `MAINTENANCE` 且 `quiescent=true`；否则409 `MAINTENANCE_REQUIRED` 或 `MAINTENANCE_BUSY`，后者details列出在途动作、pending写入或UNKNOWN请求。
2. 使用SQLite backup API生成一致DB副本，另存blob清单、逐文件hash、schema/迁移版本、app版本、policy版本、删除墓碑hash和 `expires_at`；不得直接复制活跃主DB或遗漏WAL。备份只写本机受控目录，不上传、不自动执行。
3. 备份包含敏感业务数据但不包含可用凭据、密钥、模型内部推理；清单和日志脱敏。默认保留7天，`expires_at` 必须展示。
4. 创建完成不等于可用：必须先恢复到临时目录并通过hash/外键/schema/删除政策核验，状态才为 `VERIFIED`、`usable=true`、`restore_allowed=true`；此前 `usable=false`。客户端不得声明已验证。
5. 同一 `Idempotency-Key` 不重复创建备份；并发维护操作只允许一个非终结操作，第二个409 `MAINTENANCE_BUSY`。

`GET /maintenance/backups` 返回分页清单；`GET /maintenance/backups/{id}` 返回 `BackupManifest`：

```json
{
  "id": "uuid",
  "created_at": "2026-10-19T12:00:00Z",
  "expires_at": "2026-10-26T12:00:00Z",
  "status": "VERIFIED",
  "usable": true,
  "restore_allowed": true,
  "format_version": 1,
  "schema_version": "0011_lifecycle",
  "app_version": "0.1.0",
  "policy_version": "1",
  "db_revision": "0011_lifecycle",
  "manifest_hash": "sha256:…",
  "blob_count": 42,
  "total_bytes": 123456,
  "blob_hashes_digest": "sha256:…",
  "deletion_tombstone_hash": "sha256:…",
  "unknown_model_request": false,
  "verification": {
    "status": "VERIFIED",
    "verified_at": "2026-10-19T12:01:00Z",
    "temp_restore_ok": true,
    "schema_ok": true,
    "foreign_keys_ok": true,
    "hashes_ok": true
  },
  "secret_free": true
}
```

`POST /maintenance/backups/{id}/verify` 请求 `{manifest_hash}`，200返回 `{status:"VERIFIED"或"FAILED",verification,usable,restore_allowed}`。服务端必须重新读取并校验文件hash、DB完整性、外键、schema和删除清单；`manifest_hash` 不匹配返回412 `MANIFEST_HASH_MISMATCH`，不能只凭客户端声明改为VERIFIED。过期、已删除或清理中的备份返回410 `BACKUP_EXPIRED`/`BACKUP_DELETED`；版本不兼容返回422 `BACKUP_INCOMPATIBLE`；核验失败返回422 `BACKUP_VERIFICATION_FAILED` 并保持 `usable=false`。

恢复：

`POST /maintenance/restores` 请求：

```json
{
  "backup_id": "uuid",
  "manifest_hash": "sha256:…",
  "explicit_restore": true,
  "acknowledge_interruption": true,
  "acknowledge_lifecycle": true
}
```

202 返回 `{restore_id,status:"VALIDATING",status_url,backup_id}`。规则：

1. 必须维护且静默、无其他维护操作、备份状态 `VERIFIED`、未过期/未删除、格式/schema/app兼容且 `manifest_hash` 匹配；不满足返回409/410/412/422，且不改动现有数据。`policy_version` 不同本身不构成拒绝，但必须按当前政策重新检查并应用保留/到期规则，不沿用备份时的旧保留期；若政策语义已不兼容则返回422 `BACKUP_INCOMPATIBLE`。
2. 三个显式字段必须为 `true`，不得有默认值；客户端不能声明“已验证/已应用”替代服务端核验。恢复前再次在临时目录验证备份，并核对schema、外键、blob hash和删除墓碑。
3. 恢复前合并当前工作区删除墓碑、备份内墓碑及到期政策：备份之后删除或到期的内容不得因恢复旧备份而复活；仍有合法引用的共享blob不得误删，被删除资料的引用不得绕过墓碑。
4. 发布顺序为：验证临时副本 → 应用墓碑/到期 → 替换DB与blob引用 → 校验hash/外键/schema → 写恢复事实。任一步失败保留当前数据并保持维护模式，状态 `FAILED`，不得部分替换或破坏性downgrade。
5. 成功后把遗留 `RUNNING/PAUSING/CANCELLING` 标为 `INTERRUPTED`（reason=`RESTORED`），`auto_resumed:false`；不自动续跑、不自动创建分支、不把旧成功证据当新完成。`execution_available` 只在全部检查通过后为真。
6. 恢复完成后仍保持 `MAINTENANCE`，由用户显式退出；退出前不得接受新的研究启动。恢复期间不得并发第二个恢复或启动新研究；超时/断线只更新操作状态，不声称数据已替换。

`GET /maintenance/restores/{id}` 返回 `RestoreStatus`：

```json
{
  "id": "uuid",
  "backup_id": "uuid",
  "status": "COMPLETED",
  "manifest_hash": "sha256:…",
  "started_at": "2026-10-19T12:02:00Z",
  "finished_at": "2026-10-19T12:04:00Z",
  "steps": [
    {"name": "VALIDATE_BACKUP", "status": "SUCCEEDED"},
    {"name": "APPLY_TOMBSTONES", "status": "SUCCEEDED"},
    {"name": "SWAP_DATA", "status": "SUCCEEDED"},
    {"name": "VERIFY_DATA", "status": "SUCCEEDED"}
  ],
  "error": null,
  "post_restore": {
    "schema_version": "0011_lifecycle",
    "interrupted_runs": 2,
    "tombstones_applied": 3,
    "execution_available": true,
    "auto_resumed": false
  }
}
```

状态只允许 `VALIDATING/APPLYING_TOMBSTONES/RESTORING/VERIFYING/COMPLETED/FAILED/ROLLED_BACK`；`ROLLED_BACK` 必须附原因和仍可用的当前数据版本。错误：404 `BACKUP_NOT_FOUND`/`RESTORE_NOT_FOUND`；410 `BACKUP_EXPIRED`；409 `MAINTENANCE_REQUIRED`、`MAINTENANCE_BUSY`、`RESTORE_CONFLICT`；412 `MANIFEST_HASH_MISMATCH`；422 `BACKUP_INCOMPATIBLE`、`BACKUP_VERIFICATION_FAILED`、`RESTORE_BLOCKED`；503恢复依赖不可用。

## 核心DTO

`RunSnapshot = {id,root_run_id,parent_run_id?,fork_step_id?,workflow_version_id,status,reason_code?,revision,epoch,current_step_id?,confirmed_input_hash,last_checkpoint_hash?,budget:{limits,consumed,reserved},draining,in_flight_actions:[{id,type,status}],assistance_ids,completeness?,artifact_ids,last_event_sequence,allowed_actions:[{action,enabled,disabled_reason?}]}`。

`RequirementSnapshot = {id,revision,workflow_version_id,status,fields:[{path,value,source,message_id?,suggestion_id?,accepted}],missing_fields,conflicts,preference_order,output_spec,summary_markdown,summary_hash,suggestions,confirmation_id?}`。summary必须含H1、H2、H3和条目；不能为凑标题编造要求。Markdown渲染禁用原始HTML，过滤危险链接。

`Assistance = {id,kind,status,triggered_limits,consumed,completed_steps,remaining_steps,observed_problem,uncertainties,attempted_remedies,available_decisions,decisions}`。UNKNOWN不能被描述成确定故障诊断。

`DraftBudgetSnapshot = {draft_id,ledger_id,ledger_scope:"DRAFT",revision,limits,consumed,reserved,remaining,active_ms,exhausted,exhausted_dimensions,assistance?,updated_at}`。assistance只描述草稿额度阻塞，不是run的AssistanceRequest；`grants`历史由BudgetEntry提供，不在此快照展开。

`SetupStatus = {required,completed,mode,app_version,schema_version,migration,data_dir,execution_available,runtime,policies,credentials,blockers,revision,allowed_actions}`。不含秘密；completed由服务端核验，不接受客户端声明。

`MaintenanceStatus = {mode,revision,reason?,requested_at?,entered_at?,draining,in_flight_actions,pending_writes,unknown_model_request?,quiescent,migration_version,backup_in_progress,restore_in_progress,allowed_actions}`。mode=NORMAL/ENTERING/MAINTENANCE/EXITING。

`BackupManifest = {id,created_at,expires_at,status,usable,restore_allowed,format_version,schema_version,app_version,policy_version,db_revision,manifest_hash,blob_count,total_bytes,blob_hashes_digest,deletion_tombstone_hash,unknown_model_request,verification,secret_free}`。status=CREATING/VERIFYING/VERIFIED/FAILED/EXPIRED/DELETED；只有VERIFIED且未过期才可恢复。

## SSE

`GET /runs/{id}/events`，`Accept: text/event-stream`；认证同本机会话。`Last-Event-ID` 为run内序号，首次可带 `after` 参数。先读快照的last_event_sequence，再从此序号之后订阅，避免获取快照和连接间丢事件。

每条 `id: <sequence>`、`event: <kind>`、`data: <JSON>`；JSON包含 `{schema_version:1,event_id,run_id,step_run_id?,attempt_id?,workflow_version_id,sequence,occurred_at,status?,reason_code?,payload}`。kind至少为 run.state_changed、step.state_changed、action.started、action.finished、artifact.available、assistance.required、budget.changed、recovery.checked、branch.created、late_result.quarantined。不得包含逐字推理、密钥或未经筛选工具原始内容。

15秒心跳注释不占sequence；只发送已提交事件，重连可重复，客户端以(run_id,sequence)去重。历史游标被清理则发送 `stream.reset`（控制帧不冒充业务事件，含snapshot_url），客户端重新获取快照。资源删除返回410。慢客户端最多缓冲1000事件，超出断开重连从DB补；不阻塞引擎、不建立长读事务。GET /runs/{id}/event-history?after=&limit= 提供同格式分页补读，默认100最大1000。

## UI合同

首次配置页在完成前先展示迁移/数据目录/运行时健康、保留与上限政策、外部传输范围和凭据健康；用户必须阅读政策后显式确认，凭据只写不读，完成后才进入固定流程列表。配置页不要求注册、用户切换或角色，也不把 `configured` 显示成“真实模式可用”。

首页固定流程列表；详情含适用范围和版本；聊天页只通过聊天修改要求、展示完整分层列表及显式确认按钮；草稿额度面板显示独立账本的消耗、待分析/模型忙/UNKNOWN原因及显式追加按钮，追加后只刷新额度与允许动作，不自动启动研究或确认；执行页含步骤、来源摘要、上限、暂停/取消分别按钮、求助和当前在途动作；历史页含分支与源成果；版本页可修改定义并保存新版本；文档页可读/下载并标完整程度；数据页说明保留和删除。

维护/数据页显示当前维护模式、在途动作、UNKNOWN请求、备份有效期、验证状态、可恢复性和恢复后中断影响；备份只由显式按钮创建，恢复必须再次确认中断与生命周期影响，过期/未验证/不兼容的备份按钮禁用并显示原因，不能把“已请求”显示成“已备份”或“已恢复”。恢复完成页说明遗留run为INTERRUPTED且不会自动续跑。

空库、版本变化、确认过期、并发忙、未知结果、资料删除、来源失败、写入冲突、额度触顶、策略变化、备份过期/验证失败、维护未静默均有具体说明及允许动作，按钮禁用原因来自服务端allowed_actions。新版保存冲突时保留本地待保存定义，不覆盖服务端。前端不能凭按钮状态替代后端权限/状态验证。
