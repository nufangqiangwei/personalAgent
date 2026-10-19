# Agent Runtime / Tool / Model Gateway Contract v1

下述签名是Personal-Agent端口，不是官方SDK原样API。领域层只依赖这些DTO。官方接口映射与证据见 ../research.md；SDK固定版本实际能力经契约测试后启用，不能编造session.cancel等不存在接口。

## Runtime端口

| 操作 | 输入 | 输出与错误 |
|---|---|---|
| capabilities() | 无 | {adapter_version,sdk_version,runtime_version,platform,profile_hash,supports_resume,supports_cooperative_stop,remote_cancel_verifiable,tool_policy_enforced,model_gateway_enforced,persistence_mode}；unsupported能力明确false |
| create_session(context) | {operation_id,owner:{run_id?,attempt_id?,draft_id?},epoch,model_id,profile_hash,capability_token,policy_hash} | {adapter_session_id,provider_session_id?,runtime_id}；重复operation返回同一关联；不得选用默认全权限profile |
| submit(task) | {operation_id,session_id,epoch,goal,context_refs,validated_input,output_schema,deadline,tool_scope,budget_reservation_id} | {submission_id,status:"accepted"}；accepted只是入队，不能代表业务完成 |
| events(session_id,cursor?) | 游标 | AsyncIterator<RuntimeEvent>；可重复，映射层去重；断流不代表任务完成 |
| wait_result(submission_id) | 提交ID | {status:"completed"|"failed"|"unknown",structured_output?,usage,tool_action_refs,completion_evidence_ref?,error?}；只有引擎验证后才能形成业务成功 |
| request_stop(session_id,reason) | pause/cancel/budget/shutdown | {status:"requested"|"drained"|"unknown",in_flight_request_ids}；先撤销新工具/模型权限；不映射不存在的SDK cancel |
| reconcile(session_id) | 已有关联 | {local_runtime_status,model_requests,tool_operations,completion_evidence}；证据不足保持unknown |
| restore_session(link,checkpoint) | 已记录版本/profile/原session及业务检查点 | 恢复原session或RUNTIME_INCOMPATIBLE/SESSION_MISSING；禁止默默新建并称原恢复 |
| dispose(runtime_id) | 受管runtime标识 | 清理本地资源，记录退出结果；不承诺远端模型已结束 |

节点恢复默认可创建新session并传入业务检查点，此时必须追加新的SessionLink，明确标为重新执行未完成attempt而非原session恢复。该选择不重复实现Harness Loop。

## 工具与模型边界

ToolRequest为`{operation_id,run_id,attempt_id,epoch,tool_name,args,scope_token,deadline}`。ToolResult为`{status,result_ref?,source_refs,side_effect_operation_id?,usage,error?}`。ToolService验证作用域token、当前epoch、允许工具、参数Schema、网络/文件目标、预算与停止标志，任一不满足拒绝。token仅对当前节点有效，不能访问API管理操作。Artifact写入使用独立受管服务，不给Agent任意文件写权限。

首批工具：`search.web(query,language,domain_filters)`、`http.fetch(url)`、`browser.read(url,extraction_spec)`、`content.extract(source_ref,schema)`；extract使用纯解析，若需要模型必须显式转入ModelGateway计量，不能暗中调用。无shell、任意JS/Python执行、网页表单提交、支付、发送工具。MCP仅loopback/受管stdio，握手令牌、请求上限和取消标志不可由外部页面控制。

受限profile不自动发现用户插件；TS策略插件复用官方工具注册、restrict和guard。restrict不是独立安全边界，guard和Python ToolService必须再次拒绝scope内新增越权工具。所有任务、模型压缩、重试经唯一ModelGateway；本机代理只允许已配置模型路径/供应商并转发协议，不允许任意URL代理。真实provider凭据仅网关持有，runtime获得仅本机有效令牌，防止直连旁路。

ModelGateway.request接受`{request_id,owner,epoch,model_id,messages,output_limit,deadline,reservation}`；发送前事务占槽，IN_FLIGHT时拒绝第二请求。成功读取结束证据后结算并释放；调用方取消、超时、连接断开记UNKNOWN，保留槽和预留。重启仍需核验，无法核验就阻止新请求。禁止SDK自身retry绕开新请求的预算/序号。模型错误若明确完成才可按有界策略重新申请。

## RuntimeEvent

`{schema_version:1,event_id,provider_event_key?,session_id,submission_id?,owner,attempt_id?,epoch,sequence,occurred_at,kind,payload}`。kind仅保留session.started、action.proposed、tool.started、tool.finished、usage.updated、result.available、runtime.failed、runtime.stopped等可审查事实。归一化事件与产品业务节点分离，session完成不等于run完成。

不输出或保存逐字内部推理，action.proposed仅说明查询目的/方向。供应商原始事件先白名单转换和脱敏；敏感原始payload不落盘。协议续接需要的推理字段仅内存使用。若官方持久化无法过滤推理，则不用其持久日志，改用无持久化profile并由结构化业务检查点恢复；这仍复用官方Loop。

## 取消、迟到与错误

错误统一`{category,code,message,retryable,details}`；category=VALIDATION/PERMISSION/BUDGET/TIMEOUT/DEPENDENCY/INTERRUPTED/INTEGRITY/UNKNOWN_OUTCOME。permission、integrity、unknown_outcome默认不可自动重试。单次成功返回不免除上层语义检查。

stop先封闭新动作，等待当前请求/工具原生超时或结束；Playwright不依赖Task.cancel，使用受管task与原生timeout，先关context再关browser。超过收敛窗口仍显示draining，强制关闭仅记录本地退出。旧epoch结果隔离并追加事实；真实写入结果可补记核验，不能使已取消运行继续。

会话/运行时升级必须跑：Windows启动、受限工具拒绝、模型门无旁路、事件脱敏、无推理持久化、缺失session恢复失败、超时UNKNOWN保持、实际完成解锁、迟到不覆盖、正常结构化结果等契约测试。Mock实现同一端口，默认测试不需要真实凭据。
