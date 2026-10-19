# Phase 1 Data Model

范围：一期实体为实施目标；末节二期实体不要求一期建表。所有业务ID使用UUID，时间UTC RFC3339，UI以本地时区显示。JSON列在写入和读取时按版本校验；所有可变聚合带 `revision`，更新采用CAS。业务历史只追加；删除个人内容时保留无敏感信息墓碑，而非借审计名义永久保存内容。

## 实体与约束

| 实体 | 关键字段 | 关系、约束与索引 |
|---|---|---|
| Workflow | id, latest_executable_version_id, disabled_at, revision | 1:N WorkflowVersion；latest必须属于自身且validated/confirmed，不从最大版本号盲推 |
| WorkflowVersion | id, workflow_id, version_no, definition_json, definition_hash, schema_version, saved_at, change_summary, executable, validation_report | UNIQUE(workflow_id,version_no)；内容不可变；每次保存内容变化新版本；草稿可保存但不成为最新可执行；停用为另记状态事实 |
| RequirementDraft | id, selected_version_id, revision, status, missing_fields, conflicts, field_values, preference_order, output_spec, summary_markdown, summary_hash, source_draft_id | status=COLLECTING/READY/CONFIRMED/STALE；字段含 value、source=user/default/suggestion、message_id、accepted_at；无特别偏好是显式值，非缺失 |
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
| BudgetLedger | id, root_run_id?或draft_id?, limits_json, consumed_json, reserved_json, active_ms, revision | 根历史共享；预留、结算、额度追加用BudgetEntry记录；各维度非负，不以重试/分支清零 |
| BudgetEntry | id, ledger_id, operation_id, kind, amount, created_at, decision_id? | RESERVE/SETTLE/RELEASE/GRANT；同operation同kind去重；UNKNOWN保留预留，不当零消耗 |
| AssistanceRequest | id, run_id, kind, limit_snapshot, consumed_snapshot, completed_steps, remaining_steps, observed_problem, unknowns, attempted_remedies, status | kind=BUDGET_LIMIT/WRITE_RECONCILIATION/HUMAN_REVIEW/RUNTIME_UNCERTAIN；OPEN/RESOLVED/CANCELLED；必须提供实质求助信息 |
| UserDecision | id, assistance_id, decision, approved_increment, continue_authorized, evidence_refs, created_at | 聊天讨论可记录但不解除阻塞；追加额度和继续为不同显式字段；不可只凭用户声称成功跳过写入核验 |
| SideEffectOperation | id, root_run_id, run_id, step_attempt_id, logical_effect_key, target_ref, expected_before_hash?, intended_after_hash, intent_ref, status, observed_hash?, verified_at?, verification_ref | INTENT/NOT_APPLIED/APPLIED/PARTIAL/UNKNOWN/CONFLICT；同一逻辑效果跨分支查询；尝试ID不作唯一幂等依据；目标不接受任意客户端绝对路径 |
| ResearchAction | id, run_id, attempt_id, direction_id, purpose, tool_name, arguments_ref, started_at, result_ref, scope_hash | 每个定义方向都有动作结果或缺口；参数先脱敏，外部内容标untrusted |
| Source | id, run_id, normalized_url, title, fetched_at, published_at?, updated_at?, content_hash, blob_id?, availability, deleted_at? | 获取时间必填；无法确认的发布时间为空，不从获取时间推断；内容hash用于去重，不抹去不同获取时间 |
| Evidence | id, run_id, claim, source_ids, excerpt_refs, classification, confidence_reason, conflicts, validity | classification=FACT/OPINION/INFERENCE；validity=VERIFIED/UNVERIFIED/OUTDATED/CONFLICTED；重要外部事实须来源或未证实标记 |
| Artifact | id, run_id, attempt_id?, workflow_version_id, operation_id, blob_id, kind, format, completeness, manifest_hash, created_at, access_state | format=markdown；状态AVAILABLE/EXPIRED/DELETED/INTEGRITY_ERROR；内容组织report/checklist/comparison；下载失败不覆盖已生成成果 |
| Blob | id, managed_relative_path, sha256, size_bytes, media_type, created_at, expires_at, deleted_at | 不存任意绝对路径；immutable、同目录rename；访问必须经元数据权限与生命周期检查 |
| DeletionRecord / BackupManifest | id, affected_ids, requested_at, completed_at, backup_ids；backup含db revision、blob hash清单、expires_at | 删除后普通查询不得读取内容；恢复先应用删除记录；共享blob在仍有合法引用时不能误删，但被删除资料不能经别的引用绕过 |

常用索引：runs(root_run_id,created_at)、runs(parent_run_id)、events(run_id,sequence)、attempts(step_run_id,attempt_no)、operations(root_run_id,logical_effect_key)、artifacts(run_id)、blobs(expires_at)。runs.confirmation_id唯一，proposal对应创建的run唯一，防不同幂等键复用同一确认启动两次。模型请求非终结占用由单例槽行事务控制，不能仅依赖进程内锁。外键默认RESTRICT，删除服务协调墓碑与内容清除，禁止级联悄悄破坏执行引用。

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

节点状态：PENDING → RUNNING → SUCCEEDED/FAILED/WAITING_USER/INTERRUPTED/CANCELLED；Condition 未选择路径记SKIPPED。只有引擎可改StepRun；attempt终结后不可再次运行，恢复创建新attempt。HumanReview等待不记成功；已成功节点不会因resume自动重跑。

## 确认与分支算法契约

1. draft修改后revision+1，旧Confirmation无效；拒绝将待接受建议放进已确认输入。保存全部分层摘要，确认请求必须匹配hash与revision。
2. 分支请求记录选定节点；校验它在原版本内、实际可达且前置成果存在。选择未满足前置条件的节点只返回阻塞，不自动从别处开始。
3. changed_paths结合每节点input_reads与依赖传递闭包求最早受影响节点；未声明完整依赖、模型影响判断不确定或时效不明时不复用，要求确认可验证的更早起点，最保守为首节点。
4. 用户确认完整新要求和实际起点后，事务创建新run、parent/root关系和ReusedResult。所选节点及之后旧成果仅历史可见，不能标新分支成功。原版本不变，想改流程结构须新版本新研究。
5. 核验跨整条root历史的SideEffectOperation；新run_id不构成重复写入授权。预算与已经发生用量沿根账本累计。

## 检查点与恢复协议

执行动作前短事务创建attempt/intent/资源预留并写事件；事务外执行。产物写同目录临时文件并flush/fsync，以operation稳定目标发布；验证最终hash后短事务提交APPLIED、Artifact、节点成功、run游标、资源结算和事件。此协议不保证数据库与文件跨资源原子性，保证成功记录不会早于有效产物。

启动自检持有单实例锁，扫描遗留RUNNING/PAUSING/CANCELLING为INTERRUPTED并写事件。只诊断、不运行新研究。检查runtime指纹、状态schema、流程定义hash、确认输入hash、来源时效和blob完整性；版本不兼容、资料删除/过期则阻断，不以会话恢复成功覆盖这些检查。

操作结果核验：目标符合intended_after_hash则APPLIED并复用；确定未生效则NOT_APPLIED并在政策允许时重试；部分内容/证据缺失则PARTIAL或UNKNOWN；目标被后来更改则CONFLICT。后三者等待人工处置和可验证结果，不可点击“从头重跑”绕过。文件缺失不一定证明从未写入，必须结合操作记录和目标历史。

SSE/规范化事件与业务状态同事务，DB提交后通知；通知失败可重放。runtime事件重复以稳定key去重，无provider key时本地接收序列只用于显示，不能重复结算资源或提交副作用。epoch在每次新attempt/恢复更新，旧epoch结果不能推进状态，但仍可补记实际副作用事实。

## 二期概念模型（不建一期表）

MatchingResult（requirement、候选真实version、理由/缺口、用户选择）；ExplorationSession（目标、方向集、执行记录）；UserInstruction（修订、范围、确认、生效边界）；WorkflowDraft（探索来源、提炼路径、被舍弃路径、验证报告）；Preference（值、适用范围、显式保存确认、更新时间/删除）。与一期run/artifact/versions关联，通过新迁移添加，不提前引入多用户owner或租户字段。
