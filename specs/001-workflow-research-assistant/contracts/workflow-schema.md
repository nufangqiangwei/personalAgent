# Workflow Definition Contract v1

持久格式：规范JSON；schema_version=`1`。Pydantic v2模型在实现阶段生成JSON Schema 2020-12，严格拒绝未知字段。本文为完整语义契约，不能只通过JSON形状验证就标可执行。YAML导入不列一期必需能力，避免多套解析行为。

## 顶层字段

| 字段 | 类型 / 约束 |
|---|---|
| schema_version | string，固定1 |
| workflow_id / version | UUID / 正整数；保存时由Registry确认，客户端不能覆盖已有版本 |
| name / description / applicability | 非空string；分别最多200/4000/4000字符；适用边界可在选流前展示 |
| input_schema / output_schema | JSON Schema对象，draft2020-12；禁止远程$ref，只允许本定义内引用；限制嵌套深度20 |
| entry_step | 唯一步骤ID |
| directions | 非空数组，元素{id,title,scope}；研究种子流程至少两个方向 |
| steps | 1—100个Step，ID唯一 |
| transition_rules | Transition数组，决定业务流向，禁止prompt隐含调度 |
| execution_policy | {max_active_seconds,max_model_requests,max_tool_calls,max_search_calls,max_page_reads,max_tokens,max_attempts_per_step,model_timeout_seconds,tool_timeout_seconds}，正整数；defaults见plan，确认时可收紧，放宽需用户明确授权 |
| completion_policy | {required_output_step_ids,required_direction_ids,allow_partial_document}；必需步骤未完成不得标完整成功 |

## Step

公共字段：`id`（字母数字下划线，1—64）、`type`、`name`、`input_schema`、`output_schema`、`bindings`、`input_reads`（输入JSON Pointer集合）、`depends_on`（步骤ID集合）、`policy`、`config`。

bindings是命名字段到 `{source:"input"|"step",step_id?:ID,pointer:JSONPointer}` 的映射。不能引用未执行/不可达步骤，禁止模板字符串执行代码。input_reads必须覆盖读取原始要求的路径；间接依赖由引擎计算传递闭包。缺失依赖声明导致定义不可发布；影响无法判定时禁止复用前置成果。

policy包含 `{allowed_tools:[string],allowed_domains:[string],max_attempts,timeout_seconds,side_effect_mode:"none"|"managed_artifact"}`，只能收紧顶层/全局政策，工具列表必须在注册表存在。域名规范化、子域名规则明确配置，空列表默认拒绝网络而非允许所有。

| type | config | 执行规则 |
|---|---|---|
| AgentTask | {goal_template,context_bindings,direction_ids} | 只接当前节点目标和最小上下文；模型输出不包含授权next_step指令；Agent Loop由Harness执行，结果由引擎结构及语义校验 |
| ToolTask | {tool_name,arguments_bindings,direction_id?} | 工具名称须在allowed_tools中，运行时仍校验参数和scope；不能以工具调用管理API |
| Condition | {expression} | 无LLM，受限表达式返回boolean；必须有true和false两条互斥出边 |
| HumanReview | {prompt,decision_schema,reason_code} | 生成求助/审核对象，等待显式决定；缺失或不合schema不推进 |
| Transform | {transform_id,parameters} | 仅代码注册表中的纯函数，如去重/排序/合并；不得eval、网络访问、执行任意用户代码 |
| Output | {renderer:"markdown",structure:"report"|"checklist"|"comparison",content_bindings} | 通过Artifact Service写受管产物，记录intent；必须包括来源、缺口、硬约束与完整程度 |

可嵌入的目标模板只替换绑定值，不能改变工具/流程定义。AgentTask需要产生写入时只输出结构化内容，实际文档写入由Output执行。

## Transition与表达式

Transition字段 `{from,to,on}`，on为success/true/false/exhausted。普通节点最多一个success出边；Condition恰好true/false；Output终端无出边。exhausted为可选的显式耗尽替代路径，只能指向HumanReview或表达失败/部分成果的Output，不能绕过仍依赖失败成果的必需节点。

表达式仅支持AST：`{op:"eq"|"ne"|"lt"|"lte"|"gt"|"gte"|"contains",left:Operand,right:Operand}` 或 `{op:"and"|"or",args:[Expression...]}` 或 `{op:"not",arg:Expression}`。Operand为`{literal:JSONScalar}`或`{ref:{source,step_id?,pointer}}`；类型不匹配/缺失值产生CONDITION_INVALID，不将unknown当true。深度最多10、子表达式最多100，禁止脚本、正则执行和动态函数名。

结构校验必须检查：entry存在、所有引用存在、可达、无环、每条正常路径能到Output/显式失败、依赖均满足、条件分支不会读未执行路径输出、工具注册和权限子集、完成策略一致、输入输出Schema有效。未选路径标SKIPPED并保留规则证据。

## 证据与结果语义

研究节点输出包含`directions:[{id,status:"covered"|"missing",findings,source_ids,gaps}]`；每个要求方向都出现。重要事实有source_ids或unverified标记。候选硬约束结果为SATISFIED/VIOLATED/UNKNOWN，只有全部SATISFIED才进入合格推荐；UNKNOWN单列待确认。偏好排序必须引用最终确认的priority，不得私自生成数值权重。

`Output`成果可标COMPLETE/PARTIAL/NO_QUALIFIED_RESULT；只有完成策略允许且剩余必需条件已满足时，部分文档才能作为该流程成功交付，但UI必须显示PARTIAL而非“完整成功”。未满足完成策略的已保存部分成果可下载，run仍FAILED或WAITING_USER。

## 版本与兼容

definition_hash按稳定JSON序列化计算。保存新版本不改变运行中的schema/版本引用。schema_version未知拒绝发布/恢复；迁移须显式生成新版本，旧记录保留。运行恢复需相同定义hash、兼容状态schema、原输入hash与产物hash。更改定义不能通过同run恢复生效。

## 种子流程设计

- 技术选型对比：澄清技术场景/硬约束/优先顺序 → 官方能力方向 → 运维/限制方向 → 去重证据 → 条件判定是否缺关键条件 → 必要时HumanReview → 生成对比表。候选工具或软件按当前来源核查，不用模型记忆替代事实。
- 公开主题资料研究：澄清主题/范围/时效/输出结构 → 基础背景方向 → 最新进展方向 → 冲突与缺口归类 → 条件判定 → 报告或清单。

两个流程均由真实版本定义载入，至少两个查询方向，有界重试、人工介入及Output；具体实例将在实施时提供并通过本契约校验。本次不生成可冒充实现的流程执行代码。
