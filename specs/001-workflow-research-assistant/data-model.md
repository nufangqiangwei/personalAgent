# Phase 1 Data Model

范围：一期实体为实施目标；末节二期实体不要求一期建表。所有业务ID使用UUID，时间UTC RFC3339，UI以本地时区显示。JSON列在写入和读取时按版本校验；所有可变聚合带 `revision`，更新采用CAS。业务历史只追加；删除个人内容时保留无敏感信息墓碑，而非借审计名义永久保存内容。

T001 的四组契约只使用一期实体：草稿额度账本、首次配置单行状态、维护/备份恢复记录；不引入匹配、探索、沉淀或长期偏好实体，也不因维护或备份新增多用户、租户或角色字段。

## 实体与约束

| 实体 | 关键字段 | 关系、约束与索引 |
|---|---|---|
| Workflow | id, latest_executable_version_id, disabled_at, revision | 1:N WorkflowVersion；latest必须属于自身且validated/confirmed，不从最大版本号盲推 |
| WorkflowVersion | id, workflow_id, version_no, definition_json, definition_hash, schema_version, saved_at, change_summary, executable, validation_report | UNIQUE(workflow_id,version_no)；内容不可变；每次保存内容变化新版本；草稿可保存但不成为最新可执行；停用为另记状态事实 |
| RequirementDraft | id, selected_version_id, revision, status, missing_fields, conflicts, field_values, preference_order, output_spec, summary_markdown, summary_hash, source_draft_id | status=COLLECTING/READY/CONFIRMED/STALE；字段含 value、source=user/default/suggestion、message_id、accepted_at；无特别偏好是显式值，非缺失；每个draft至多一个草稿BudgetLedger；额度追加不创建run/Confirmation、不改变字段或摘要hash，仅解除草稿额度阻塞；CONFIRMED/STALE不可追加 |
| ConversationMessage | id, draft_id, sequence, role, content_ref, created_at | UNIQUE(draft_id,sequence)；用户原话与模型摘要区分；仅聊天修改要求 |
| Suggestion | id, draft_id, field_path, value, rationale, status, accepted_message_id | PENDING/ACCEPTED/REJECTED；接受不产生执行确认；旧revision建议不可直接应用 |
| Confirmation | id, draft_id, draft_revision, workflow_version_id, summary_hash, input_hash, budget_policy_hash, data_policy_version, confirmed_at, action | action=START/BRANCH；每次确认不可变；绑定准确快照，不复用过期确认 |
| WorkflowRun | id, root_run_id, parent_run_id, fork_step_id, workflow_version_id, confirmation_id, input_snapshot_ref/hash, config_fingerprint, runtime_fingerprint, status, reason_code, current_step_id, epoch, revision, created_at, active_elapsed_ms, completeness | 根parent为空，root=self；分支root与父一致；phase1 mode固定FIXED。版本、输入不可变；completeness=COMPLETE/PARTIAL/NO_QUALIFIED_RESULT，不能用SUCCEEDED掩盖缺口 |
| BranchProposal | id, parent_run_id, selected_step_id, effective_step_id, draft_id, changed_paths, affected_steps, invalidated_refs, verification_report, status | DRAFT/BLOCKED/READY/CONFIRMED；确认新输入及最终起点后才创建run；影响不明标BLOCKED，不自动前移 |
| ReusedResult | run_id, step_id, source_run_id, source_attempt_id, artifact_id, verification_hash, verified_at | UNIQUE(run_id,step_id,artifact_id)；只允许起点前有效依赖成果；不是新执行的成功attempt；来源可追溯至父链 |
| StepRun | id, run_id, step_id, status, current_attempt_no, revision | UNIQUE(run_id,step_id)；节点配置来自固定版本；未选条件分支可SKIPPED并记录规则依据，不能随意跳必需节点 |
| StepAttempt | id, step_run_id, attempt_no, epoch, status, input_refs/hash, output_ref/hash, started_at, ended_at, error_category, retry_reason, session_link_id | UNIQUE(step_run_id,attempt_no)；每次重试/中断重做追加；旧attempt不被新attempt覆盖 |
| SessionLink | id, run_id?, attempt_id?, draft_id?, adapter_session_id, provider_session_id?, provider, sdk_version, runtime_version, profile_hash, capability_hash, store_ref?, status | attempt或draft二选一；provider ID允许创建未完成时为空；先存本地关联再提交任务，重复创建依operation去重 |
| ExecutionEvent | id, run_id?, draft_id?, sequence, step_run_id?, attempt_id?, workflow_version_id?, occurred_at, kind, status?, reason_code?, payload_ref, schema_version, provider_event_key? | UNIQUE(owner,sequence)，provider key有值时去重；重要变更与状态同事务；无模型内部推理 |
| ModelRequest | id, owner_type, owner_id, attempt_id?, runtime_id, status, reserved_tokens, actual_tokens?, started_at, ended_at?, completion_evidence_ref?, error | RESERVED/IN_FLIGHT/COMPLETED/CANCELLED_CONFIRMED/UNKNOWN；全应用只有一个非终结请求；UNKNOWN不自动过期解锁 |
| ExecutionSlot | singleton_id=1, run_id?, epoch, process_instance_id, draining, revision | 唯一行+OS实例锁；只有收敛且无未知写入时释放；崩溃不直接把槽当空闲 |
| BudgetLedger | id, root_run_id?, draft_id?, limits_json, consumed_json, reserved_json, active_ms, revision | root_run_id与draft_id恰一非空；UNIQUE(root_run_id)、UNIQUE(draft_id)；根历史共享，草稿独立；预留、结算、额度追加用BudgetEntry记录；各维度非负，consumed单调不减，不以重试/分支/追加清零；exhausted由limits−consumed−reserved派生，不用独立布尔量掩盖 |
| BudgetEntry | id, ledger_id, operation_id, kind, amount, created_at, decision_id? | RESERVE/SETTLE/RELEASE/GRANT；同ledger+operation+kind去重；GRANT仅由显式追加端点写入，amount为各维度正整数，operation_id为幂等键；草稿GRANT不得引用run的UserDecision，run GRANT不得写草稿账本；UNKNOWN保留预留，不当零消耗；追加不重置consumed或尝试上限 |
| AssistanceRequest | id, run_id, kind, limit_snapshot, consumed_snapshot, completed_steps, remaining_steps, observed_problem, unknowns, attempted_remedies, decision_schema?, status | kind=BUDGET_LIMIT/WRITE_RECONCILIATION/HUMAN_REVIEW/RUNTIME_UNCERTAIN；OPEN/RESOLVED/CANCELLED；必须提供实质求助信息；HUMAN_REVIEW 保存该步骤声明的结果契约，重启后仍据此校验 |
| UserDecision | id, assistance_id, decision, approved_increment, continue_authorized, review_result?, evidence_refs, created_at | 聊天讨论可记录但不解除阻塞；追加额度和继续为不同显式字段；不可只凭用户声称成功跳过写入核验；HUMAN_REVIEW 的结果必须满足声明的结果契约 |
| SideEffectOperation | id, root_run_id, run_id, step_attempt_id, logical_effect_key, target_ref, expected_before_hash?, intended_after_hash, intent_ref, status, observed_hash?, verified_at?, verification_ref | INTENT/NOT_APPLIED/APPLIED/PARTIAL/UNKNOWN/CONFLICT；同一逻辑效果跨分支查询；尝试ID不作唯一幂等依据；目标不接受任意客户端绝对路径 |
| ResearchAction | id, run_id, attempt_id, direction_id, purpose, tool_name, arguments_ref, started_at, result_ref, scope_hash | 每个定义方向都有动作结果或缺口；参数先脱敏，外部内容标untrusted |
| Source | id, run_id, normalized_url, title, fetched_at, published_at?, updated_at?, content_hash, blob_id?, availability, deleted_at? | 获取时间必填；无法确认的发布时间为空，不从获取时间推断；内容hash用于去重，不抹去不同获取时间 |
| Evidence | id, run_id, claim, source_ids, excerpt_refs, classification, confidence_reason, conflicts, validity | classification=FACT/OPINION/INFERENCE；validity=VERIFIED/UNVERIFIED/OUTDATED/CONFLICTED；重要外部事实须来源或未证实标记 |
| Artifact | id, run_id, attempt_id?, workflow_version_id, operation_id, blob_id, kind, format, completeness, manifest_hash, created_at, access_state | format=markdown；状态AVAILABLE/EXPIRED/DELETED/INTEGRITY_ERROR；内容组织report/checklist/comparison；下载失败不覆盖已生成成果 |
| Blob | id, managed_relative_path, sha256, size_bytes, media_type, created_at, expires_at, deleted_at | 不存任意绝对路径；immutable、同目录rename；访问必须经元数据权限与生命周期检查 |
| SetupState | id=1, schema_version, app_version, migration_version, policy_version, policy_hash, policies_presented_at?, policies_acknowledged_at?, completed_at?, revision | 单行工作区状态；completed只能由服务端按迁移=最新、数据目录可写、execution_available、政策已展示并确认计算；客户端声明、仅勾选或“知道了”无效；不存账号/角色/密钥；政策hash变化使旧展示/确认失效 |
| MaintenanceState | id=1, mode, reason?, requested_at?, entered_at?, revision | 单行；mode=NORMAL/ENTERING/MAINTENANCE/EXITING；只由显式操作转换；ENTERING收敛前不得备份/恢复/迁移；崩溃后保留维护/进入中事实，不自动开放写入 |
| MaintenanceOperation | id, kind, status, backup_id?, target_revision?, requested_at, started_at?, finished_at?, error_code?, verification_ref? | kind=BACKUP/RESTORE/MIGRATION；status=REQUESTED/RUNNING/SUCCEEDED/FAILED/ROLLED_BACK；同应用同时最多一个非终结维护操作；恢复/迁移必须引用已验证备份；状态只追加，失败不得篡改业务数据事实 |
| DeletionRecord | id, affected_ids, requested_at, completed_at, tombstone_hash, backup_ids | 删除后普通查询不得读取内容；恢复先应用当前工作区与备份内的删除墓碑；共享blob在仍有合法引用时不能误删，但被删除资料不能经别的引用绕过 |
| BackupManifest | id, created_at, expires_at, status, format_version, schema_revision, app_version, policy_version, db_revision, manifest_hash, blob_count, total_bytes, blob_hashes_digest, deletion_tombstone_hash, unknown_model_request, verified_at?, verification_report, restored_at?, revision | status=CREATING/VERIFYING/VERIFIED/FAILED/EXPIRED/DELETED；备份内容不可变，状态与核验事实追加；VERIFIED必须来自临时目录恢复验证；只有VERIFIED且未过期可恢复；EXPIRED/DELETED返回410；清单不含凭据；恢复后不复活已删内容；不直接复制活跃主DB或遗漏WAL |

常用索引：runs(root_run_id,created_at)、runs(parent_run_id)、events(run_id,sequence)、attempts(step_run_id,attempt_no)、operations(root_run_id,logical_effect_key)、artifacts(run_id)、blobs(expires_at)、budget_ledgers(root_run_id)唯一、budget_ledgers(draft_id)唯一、budget_entries(ledger_id,operation_id,kind)唯一、backup_manifests(expires_at)、maintenance_operations(kind,status)。`SetupState`与`MaintenanceState`用 `id=1` 唯一行+CHECK约束。runs.confirmation_id唯一，proposal对应创建的run唯一，防不同幂等键复用同一确认启动两次。模型请求非终结占用由单例槽行事务控制，不能仅依赖进程内锁。外键默认RESTRICT，删除服务协调墓碑与内容清除，禁止级联悄悄破坏执行引用。

## 运行状态与转换

| 当前 | 允许到达 | 条件 |
|---|---|---|
| PENDING | RUNNING / CANCELLED | 输入/版本/政策确认有效，CAS取得执行槽且模型槽无未知请求；不自动排队启动 |
| RUNNING | WAITING_USER / PAUSING / CANCELLING / SUCCEEDED / FAILED / INTERRUPTED | 上限或HumanReview先封闭新行动；完成需所有必需路径、输出与交付检查通过 |
| PAUSING | PAUSED / INTERRUPTED / CANCELLING | 当前动作确认收敛才PAUSED；无法判定进入INTERRUPTED，保留draining和未知原因 |
| CANCELLING | CANCELLED / INTERRUPTED | 终止已确认才CANCELLED；结果未知则INTERRUPTED并保留cancel_requested，核验后只能完成取消，不偷偷续跑 |
| PAUSED / INTERRUPTED | RUNNING / CANCELLING | 用户点击继续、原输入/版本/hash/兼容性通过、结果核验完成、资源足够且无cancel_requested；否则等待并给原因 |
| WAITING_USER | RUNNING / PAUSING / CANCELLING / FAILED | 普通HumanReview满足结果契约；BUDGET_LIMIT需明确追加和继续；UNKNOWN需真实结束证据；不得因沉默或超时自动恢复 |
| FAILED / CANCELLED / SUCCEEDED | 无直接续跑转换 | 终态保留。重跑走新分支；下载重试只新增访问事实，不重跑研究 |

`WAITING_USER` 时可能仍有收敛中的动作，API必须返回 `draining=true` 和 `in_flight_actions`，不能仅看状态放开第二执行。停止后有可信副作用结果也须追加事实，不允许改变CANCELLED为SUCCEEDED。

维护门禁不新增run状态，也不改写上表：进入维护只阻止新动作，run保留原状态和原因；恢复或迁移完成后遗留 `RUNNING/PAUSING/CANCELLING` 一律转 `INTERRUPTED`（reason=`RESTORED`），不得自动回到RUNNING、不得自动续跑，只能由用户显式继续或另建分支。备份快照不能把中断事实覆盖成成功。

节点状态：PENDING → RUNNING → SUCCEEDED/FAILED/WAITING_USER/INTERRUPTED/CANCELLED；Condition 未选择路径记SKIPPED。只有引擎可改StepRun；attempt终结后不可再次运行，恢复创建新attempt。HumanReview等待不记成功；已成功节点不会因resume自动重跑。

## 确认与分支算法契约

1. draft修改后revision+1，旧Confirmation无效；拒绝将待接受建议放进已确认输入。保存全部分层摘要，确认请求必须匹配hash与revision。
2. 分支请求记录选定节点；校验它在原版本内、实际可达且前置成果存在。选择未满足前置条件的节点只返回阻塞，不自动从别处开始。
3. changed_paths结合每节点input_reads与依赖传递闭包求最早受影响节点；未声明完整依赖、模型影响判断不确定或时效不明时不复用，要求确认可验证的更早起点，最保守为首节点。
4. 用户确认完整新要求和实际起点后，事务创建新run、parent/root关系和ReusedResult。所选节点及之后旧成果仅历史可见，不能标新分支成功。原版本不变，想改流程结构须新版本新研究。
5. 核验跨整条root历史的SideEffectOperation；新run_id不构成重复写入授权。预算与已经发生用量沿根账本累计。

## 草稿额度账本契约

1. 每个 `RequirementDraft` 至多一条 `BudgetLedger`（`draft_id` 非空、`root_run_id` 为空）；每个根研究历史恰一条 `BudgetLedger`（`root_run_id` 非空、`draft_id` 为空）。两账本不共享、不互转、不因分支创建新账本；分支继续使用共同根的账本。草稿额度不是运行求助，不得写入 `AssistanceRequest`/`UserDecision`，也不得借用 `GRANT_AND_CONTINUE`。
2. 消耗只由已提交的 `ModelRequest` 与 `BudgetEntry(RESERVE/SETTLE/RELEASE)` 产生；聊天文本、模型建议、UI显示和客户端“已批准”声明都不是消耗或授权证据。`consumed` 单调不减，`reserved` 仅由未终结请求持有。
3. 触顶判定为任一维度 `remaining = limits − consumed − reserved ≤ 0`。触顶后草稿进入派生的 `BUDGET_LIMIT` 求助状态，只允许显式 `GRANT`；未触顶不创建预防性加额，也不自动扩大上限。
4. `GRANT` 只能在 `RequirementDraft.status=COLLECTING/READY` 时写入；必须来自显式用户动作，携带草稿 `revision` 的If-Match与幂等键，并在事务内对账本 `revision` 做CAS。追加 `BudgetEntry(kind=GRANT, operation_id=幂等键)`，将各维度正整数增量累加到 `limits_json`；`consumed`、`reserved`、`active_ms` 不变，账本 `revision` 递增。不得只加派生字段而漏记GRANT，也不得用直接改 `limits_json` 代替账本条目。
5. GRANT 不创建run、Confirmation或ConversationMessage，不改变字段、来源、缺失项、摘要hash或选定版本，不自动提交分析或研究启动；`research_started` 恒为 `false`。用户仍需显式重试分析或继续聊天。
6. UNKNOWN 的 `ModelRequest` 保留在草稿账本中并不因GRANT解除；`assistance.unknown_model_request=true` 时不得提供继续/重试动作，直到取得可信完成证据。GRANT 也不重置节点/草稿尝试上限。
7. 同 `ledger_id + operation_id + kind` 去重，浏览器重试不得重复加额；并发分析、结算与GRANT以账本 `revision` CAS 串行化，失败方返回冲突并重新读取，不产生部分额度。GRANT 的 `operation_id` 同时作为请求幂等审计键。
8. 确认并启动研究后，草稿账本保留用于审计与展示，但不得转入根账本、不得抵扣run预算、不得为后续run预支；根研究使用自己的额度与独立累计，不复用草稿余额。删除草稿时按生命周期保留无内容墓碑与账本事实，不复用其额度。
9. `DraftBudgetSnapshot` 的 `limits/consumed/reserved/remaining` 必须由账本与请求事实实时计算；exhausted、耗尽维度和可用动作不得由客户端写入或缓存覆盖。

## 首次配置契约

1. `SetupState` 为单行（`id=1`）工作区状态，保存 `schema_version`、`app_version`、`migration_version`、`policy_version`、`policy_hash`、`policies_presented_at`、`policies_acknowledged_at`、`completed_at` 与 `revision`。不保存账号、角色、租户、用户资料或任何密钥。
2. `completed` 只能由服务端实时判定：迁移已到head、数据目录可写、`execution_available` 为真（Mock或已通过接入门禁的真实模式）、当前 `policy_hash` 已展示并被显式确认。任一不满足即 `completed=false`；客户端提交 `completed:true`、只勾选界面或仅回复“知道了”均无效。
3. 政策展示与确认只记录不可变事实：`policies_presented_at` 必须早于确认；确认必须匹配当前 `policy_version` 与 `policy_hash`，并同时确认数据/保留政策与预算/上限政策；政策内容或hash变化使旧展示/确认失效，必须重新展示和确认。确认不启用真实模式、不创建流程或run、不替代迁移与运行时检查。
4. 凭据健康只保存 provider、来源类型、是否可用、最近检查时间和不可逆/带密钥的指纹前缀；密钥只允许来自环境变量或系统凭据存储，不进入数据库、日志、错误、SSE、导出或备份清单。`configured=true` 只表示凭据存在，不表示真实runtime通过接入门禁。
5. 首次配置未完成时，只读接口与 `/setup/*`、`/settings/credentials` 可用；研究、分支、run继续、流程保存、删除等写操作返回 `SETUP_REQUIRED`。已完成后若迁移、数据目录或运行时检查退化，`required` 重新为真并以 `SETUP_INCOMPLETE` 阻断写操作，保留只读查看。
6. 首次配置不引入账号、登录、用户切换或租户；本机会话、Origin/CSRF与实例锁仍适用。配置完成不自动创建研究、分支或后台任务。

## 维护、备份与恢复契约

1. `MaintenanceState` 为单行状态机：`NORMAL → ENTERING → MAINTENANCE → EXITING → NORMAL`。只允许显式用户操作触发；进入时停止新增研究、分支、run继续、草稿额度追加与业务写入，并在动作边界等待在途收敛。15秒未收敛保持 `ENTERING` 与 `draining=true`，不得显示“已停止”，也不得开始备份/恢复/迁移。
2. `quiescent` 必须同时满足：无在途动作、无draining写入、无待处置的 `INTENT/PARTIAL/UNKNOWN/CONFLICT` `SideEffectOperation`、无 `RESERVED/IN_FLIGHT/UNKNOWN` 的 `ModelRequest`。`APPLIED`/`NOT_APPLIED` 是已核验终态，不阻塞。UNKNOWN 不得通过备份、恢复、迁移、关闭进程或重启绕过。
3. 维护模式是应用级门禁，不伪造run状态；不把请求停止写成终态，也不自动把暂停/中断run改为成功。退出维护只在无恢复/迁移在途、迁移至head、数据目录可写、无未核验写入时允许；UNKNOWN 可以保留但 `execution_available=false`。恢复或迁移后的遗留执行保持 `INTERRUPTED`，退出维护不自动续跑。
4. 备份使用SQLite backup API生成一致DB副本，并保存blob逐文件hash、schema/迁移版本、app版本、policy版本、删除墓碑hash、`expires_at` 与 `manifest_hash`；不得直接复制活跃主DB或遗漏WAL。备份只写本机受控目录，不含可用凭据、密钥或模型内部推理，默认7天过期。
5. 备份创建完成不等于可用：必须恢复到临时目录并通过hash、外键、schema与删除政策核验后才置 `VERIFIED/usable=true/restore_allowed=true`；此前 `usable=false`。客户端声明“已验证”不改变状态；`manifest_hash` 不匹配、版本不兼容或核验失败分别返回412/422并保持不可恢复。
6. 恢复必须引用 `VERIFIED`、未过期/未删除、格式/schema/app兼容的 `BackupManifest`，并同时显式确认恢复、执行中断和生命周期影响。`policy_version` 不同本身不构成拒绝，但必须按当前政策重检并应用保留/到期规则，不沿用备份时的旧保留期；政策语义不兼容时返回 `BACKUP_INCOMPATIBLE`。恢复前再次验证临时副本；恢复顺序为验证 → 合并并应用当前工作区墓碑、备份内墓碑与到期政策 → 替换DB/blob引用 → 校验hash/外键/schema → 写入恢复事实。备份之后删除或到期的内容不得因恢复旧备份而复活；共享blob仍有合法引用时不得误删，被删资料不得经其他引用绕过。
7. 恢复失败必须保留恢复前的数据与可用备份，保持维护模式并记录 `FAILED/ROLLED_BACK` 与原因；禁止部分替换、破坏性downgrade或以客户端声明掩盖失败。恢复成功后把遗留 `RUNNING/PAUSING/CANCELLING` 标为 `INTERRUPTED`（reason=`RESTORED`），`auto_resumed=false`；只有全部检查通过才 `execution_available=true`，且仍须用户显式退出维护。
8. `MaintenanceOperation` 同应用同时最多一个非终结操作；备份、恢复、迁移均需 `Idempotency-Key`，重复同请求返回原操作，同键异请求冲突。备份过期/删除返回410，未验证/不兼容返回422，未维护/未静默返回409。
9. 用户只能发起备份/恢复/迁移请求，不能把备份标为VERIFIED、把恢复标为COMPLETED、把副作用标为APPLIED或把遗留run标为成功；这些状态只由服务端核验事实写入。
10. `SetupState`、`MaintenanceState`、`MaintenanceOperation` 与 `BackupManifest` 随一期生命周期迁移建立，不新增二期表；不得用文件、进程内变量或客户端声明代替持久状态。备份清单与恢复事实的保留不低于备份政策期限。

## 检查点与恢复协议

执行动作前短事务创建attempt/intent/资源预留并写事件；事务外执行。产物写同目录临时文件并flush/fsync，以operation稳定目标发布；验证最终hash后短事务提交APPLIED、Artifact、节点成功、run游标、资源结算和事件。此协议不保证数据库与文件跨资源原子性，保证成功记录不会早于有效产物。

启动自检持有单实例锁，扫描遗留RUNNING/PAUSING/CANCELLING为INTERRUPTED并写事件。只诊断、不运行新研究。检查runtime指纹、状态schema、流程定义hash、确认输入hash、来源时效和blob完整性；版本不兼容、资料删除/过期则阻断，不以会话恢复成功覆盖这些检查。

备份/恢复协议：备份只在维护且静默时用SQLite backup API生成，不直接复制活跃主DB；清单保存db revision、schema/app/policy版本、blob hash、删除墓碑与过期时间，验证阶段恢复到临时目录，失败不得标为可用。恢复先验证清单和兼容性，再合并当前与备份内墓碑并应用到期政策，随后替换数据并校验hash/外键/schema；失败保留恢复前数据与可用备份。恢复成功不自动续跑：遗留执行转INTERRUPTED，用户显式继续前不得新增研究；旧备份不得复活已删除内容，也不得用恢复绕过未知模型请求或未核验写入。

操作结果核验：目标符合intended_after_hash则APPLIED并复用；确定未生效则NOT_APPLIED并在政策允许时重试；部分内容/证据缺失则PARTIAL或UNKNOWN；目标被后来更改则CONFLICT。后三者等待人工处置和可验证结果，不可点击“从头重跑”绕过。文件缺失不一定证明从未写入，必须结合操作记录和目标历史。

SSE/规范化事件与业务状态同事务，DB提交后通知；通知失败可重放。runtime事件重复以稳定key去重，无provider key时本地接收序列只用于显示，不能重复结算资源或提交副作用。epoch在每次新attempt/恢复更新，旧epoch结果不能推进状态，但仍可补记实际副作用事实。

## 二期概念模型（不建一期表）

MatchingResult（requirement、候选真实version、理由/缺口、用户选择）；ExplorationSession（目标、方向集、执行记录）；UserInstruction（修订、范围、确认、生效边界）；WorkflowDraft（探索来源、提炼路径、被舍弃路径、验证报告）；Preference（值、适用范围、显式保存确认、更新时间/删除）。与一期run/artifact/versions关联，通过新迁移添加，不提前引入多用户owner或租户字段。

T001 的草稿额度追加、首次配置、维护与备份恢复均已完成一期契约：草稿额度使用独立 `BudgetLedger(draft_id)`，不借用二期 `Preference`；首次配置使用单行 `SetupState`，不引入账号/租户；维护与备份恢复使用 `MaintenanceState`/`MaintenanceOperation`/`BackupManifest`，不引入 `ExplorationSession` 或 `MatchingResult`。
