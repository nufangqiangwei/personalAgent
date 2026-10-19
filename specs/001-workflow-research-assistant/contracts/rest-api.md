# REST / SSE / UI Contract v1

这是实施契约，非已运行API。基础路径 `/api/v1`，JSON UTF-8，UUID标识，UTC时间，未知字段默认拒绝。下列字段用 `?` 表示可空/可省略，其余必填。枚举/实体约束见 ../data-model.md。生产同源本机会话，写请求校验Origin和CSRF；不建设账号、租户或角色。

## 通用约定

- 写请求使用 `Idempotency-Key`；键按工作区+路由+规范请求hash去重，重复同请求返回原资源，同键异请求409。执行启动/分支/额度追加的去重记录保留至关联业务记录删除；不能因浏览器重试产生新run或重复追加额度。
- 可变资源返回ETag=revision；更新/确认/恢复需 `If-Match`，缺失428、不匹配412。不可变版本使用definition_hash。
- 列表 `{items,next_cursor}`，limit默认20最大100；快照包含 `last_event_sequence`。分页稳定排序created_at+id。
- 错误 `{error:{code,message,retryable,details,request_id}}`；details仅含可公开字段。400格式、403范围拒绝、404不存在、409状态/忙/保存冲突、410删除或过期、412旧revision、422语义失败、503依赖不可用。所有错误均脱敏。
- 重要错误code：INPUT_INCOMPLETE、UNACCEPTED_SUGGESTION、CONFIRMATION_STALE、VERSION_CHANGED、RUN_BUSY、MODEL_BUSY、MODEL_OUTCOME_UNKNOWN、PREREQUISITE_INVALID、START_POINT_REQUIRES_CONFIRMATION、BUDGET_ASSISTANCE_REQUIRED、SIDE_EFFECT_UNRESOLVED、RUNTIME_INCOMPATIBLE、ARTIFACT_UNAVAILABLE、DEFINITION_INVALID。

## 接口目录

| 方法/路径 | 请求 | 成功响应和规则 |
|---|---|---|
| GET /health | 无 | 200 `{status,mode,dependency_checks,execution_available}`；不包含凭据。进程存活与真实runtime可用分开 |
| GET /policies | 无 | 200 `{version,retention,budget_defaults,allowed_actions,storage_scope}`，首次保存/执行前UI展示 |
| GET /workflows | cursor?,limit? | 最新可执行版本列表，含用途/边界/必要输入/主要步骤/产出；空库items=[]，不推荐虚构工作流 |
| GET /workflows/{id} | 无 | `{id,latest_version,disabled,revision}` |
| GET /workflows/{id}/versions | cursor?,limit? | 全历史及草稿、时间、变更摘要 |
| GET /workflows/{id}/versions/{version_id} | 无 | 完整定义、hash、验证报告、可执行标识 |
| POST /workflows/{id}/versions | `{base_version_id,definition,change_summary,confirm_executable}` | 201新版本；检查base与当前编辑基线冲突409；不覆盖旧版本；无内容变化返回原版本不虚增 |
| POST /workflows/{id}/disable | `{reason}` | 200停用；保留已有运行和历史 |
| POST /requirements | `{workflow_version_id}` | 201 draft COLLECTING；仅选流不启动研究 |
| GET /requirements/{id} | 无 | draft DTO，含messages、来源、建议、缺失、冲突及完整摘要 |
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

所有确认都要求UI有明确用户动作证据。聊天“知道了”、查看摘要和接受单条建议不得自动调用start/continue授权。二期matching/exploration/preferences端点不在一期实现。

## 核心DTO

`RunSnapshot = {id,root_run_id,parent_run_id?,fork_step_id?,workflow_version_id,status,reason_code?,revision,epoch,current_step_id?,confirmed_input_hash,last_checkpoint_hash?,budget:{limits,consumed,reserved},draining,in_flight_actions:[{id,type,status}],assistance_ids,completeness?,artifact_ids,last_event_sequence,allowed_actions:[{action,enabled,disabled_reason?}]}`。

`RequirementSnapshot = {id,revision,workflow_version_id,status,fields:[{path,value,source,message_id?,suggestion_id?,accepted}],missing_fields,conflicts,preference_order,output_spec,summary_markdown,summary_hash,suggestions,confirmation_id?}`。summary必须含H1、H2、H3和条目；不能为凑标题编造要求。Markdown渲染禁用原始HTML，过滤危险链接。

`Assistance = {id,kind,status,triggered_limits,consumed,completed_steps,remaining_steps,observed_problem,uncertainties,attempted_remedies,available_decisions,decisions}`。UNKNOWN不能被描述成确定故障诊断。

## SSE

`GET /runs/{id}/events`，`Accept: text/event-stream`；认证同本机会话。`Last-Event-ID` 为run内序号，首次可带 `after` 参数。先读快照的last_event_sequence，再从此序号之后订阅，避免获取快照和连接间丢事件。

每条 `id: <sequence>`、`event: <kind>`、`data: <JSON>`；JSON包含 `{schema_version:1,event_id,run_id,step_run_id?,attempt_id?,workflow_version_id,sequence,occurred_at,status?,reason_code?,payload}`。kind至少为 run.state_changed、step.state_changed、action.started、action.finished、artifact.available、assistance.required、budget.changed、recovery.checked、branch.created、late_result.quarantined。不得包含逐字推理、密钥或未经筛选工具原始内容。

15秒心跳注释不占sequence；只发送已提交事件，重连可重复，客户端以(run_id,sequence)去重。历史游标被清理则发送 `stream.reset`（控制帧不冒充业务事件，含snapshot_url），客户端重新获取快照。资源删除返回410。慢客户端最多缓冲1000事件，超出断开重连从DB补；不阻塞引擎、不建立长读事务。GET /runs/{id}/event-history?after=&limit= 提供同格式分页补读，默认100最大1000。

## UI合同

首页固定流程列表；详情含适用范围和版本；聊天页只通过聊天修改要求、展示完整分层列表及显式确认按钮；执行页含步骤、来源摘要、上限、暂停/取消分别按钮、求助和当前在途动作；历史页含分支与源成果；版本页可修改定义并保存新版本；文档页可读/下载并标完整程度；数据页说明保留和删除。

空库、版本变化、确认过期、并发忙、未知结果、资料删除、来源失败、写入冲突均有具体说明及允许动作，按钮禁用原因来自服务端allowed_actions。新版保存冲突时保留本地待保存定义，不覆盖服务端。前端不能凭按钮状态替代后端权限/状态验证。
