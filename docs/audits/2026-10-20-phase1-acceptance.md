# 一期独立代码验收报告

日期：2026-10-20（Asia/Shanghai）。审计对象：C:/Code/spiderAgent 当前实现。**结论：一期验收不通过，不能按已完成一期交付或真实研究能力发布。**

本报告重新核对当前需求、任务、实际源代码与测试，运行现有离线检查及独立反例；不以任务[x]、旧交接文档、上次收敛结论或Agent完成声明作为证据。本轮不修改源码、spec.md、plan.md、tasks.md，不自动修复，不调用真实供应商，不升级或重锁依赖。

## 1. 范围和依据

依据：[AGENTS.md](/C:/Code/spiderAgent/AGENTS.md)、[.specify/memory/constitution.md](/C:/Code/spiderAgent/.specify/memory/constitution.md)、[specs/001-workflow-research-assistant/spec.md](/C:/Code/spiderAgent/specs/001-workflow-research-assistant/spec.md)、[specs/001-workflow-research-assistant/plan.md](/C:/Code/spiderAgent/specs/001-workflow-research-assistant/plan.md)、[specs/001-workflow-research-assistant/tasks.md](/C:/Code/spiderAgent/specs/001-workflow-research-assistant/tasks.md)，并读取data-model及REST/工作流/runtime三个契约。

历史原始需求：[docs/需求.md](/C:/Code/spiderAgent/docs/需求.md)、[docs/PRD-一期.md](/C:/Code/spiderAgent/docs/PRD-一期.md)。当前spec明示替代旧分期；旧PRD的“仅旅游、长期偏好属一期、不开放版本编辑和恢复”等内容不作为本轮一期标准。没有据旧文档把后续功能恢复为一期缺陷。

一期七个P1故事：US1选流后聊天澄清与完整确认；US3固定工作流多方向研究；US4文档交付；US7不可变版本管理；US9状态、暂停取消、人工恢复、节点分支、上限求助、全局串行；US10实际可用完整UI；US11无需先描述需求的手动选流。另包含首次配置、公开只读工具授权、资料生命周期、备份迁移恢复、可复现启动/打包及安全约束。

明确排除：二期US2/US5/US6/US8；FR-003、FR-005、FR-013–019、FR-023；SC-002、SC-004、SC-010、SC-011。不把模型匹配、协同探索、流程沉淀、长期偏好管理、旅游专有规则、账号/租户/角色算作一期缺陷。

核对范围：23项FR、13项SC、47个故事验收场景、A1–A5，共88项需求/验收条目；原始T001–T103中92个已勾选任务逐项核对。原始11个未勾选任务及Phase11的35个补缺任务仍按未完成处理，不重复当作虚假完成。232个源文件、规格和契约的前后SHA-256一致。

判定：通过＝该子范围有本轮代码与验证证据；部分实现＝存在实现但仍有未满足义务；失败＝直接反例或占位使核心义务不能成立；无法验证＝缺环境/授权/目标用户或指定证据。通过不等于真实模式通过。CRITICAL为基线不可用或关键全局一致性/可信判定直接破坏；HIGH为核心功能、恢复、授权或数据保护问题；MEDIUM为重要测试、边界或可复现性问题。

## 2. 总体结果与验收矩阵入口

| 范围 | 结论 |
|---|---|
| 一期功能FR（23项） | 3通过、12部分实现、8失败 |
| 已勾选任务（92项） | 21通过、57部分实现、14失败；不能将92个勾选视为92项验收通过 |
| 缺陷/工程不符合项 | 31项：CRITICAL 4、HIGH 23、MEDIUM 4。代码、测试、环境和交付条件的性质逐项说明 |
| 默认自动化 | 后端251、前端43、策略16通过，共310项；独立反例仍否定若干核心保证 |
| 质量门禁 | ruff失败4处；mypy、前端/策略类型检查通过；前端生产构建通过 |
| 正式E2E | r1248缺失，浏览器启动前阻塞；1项启动错误、23项未执行，不计应用业务断言失败 |
| 补充E2E | 本机旧版r1234：19通过、5失败；不能替代锁定版本验收 |
| live | 10项默认跳过；实际采用和真实两流程交付未运行 |

完整矩阵已分别输出，均含需求ID、任务ID、代码文件/函数、测试证据、结论与缺陷严重度：

- [一期88项需求/验收场景矩阵](/C:/Code/spiderAgent/docs/audits/2026-10-20-phase1-requirement-matrix.md)。
- [92项已完成任务逐项审计矩阵](/C:/Code/spiderAgent/docs/audits/2026-10-20-phase1-task-matrix.md)。

## 3. 需求分解、任务偏差和验收标准质量

在FR粒度，23项一期需求均能映射到任务，未发现完全没有任务的FR。问题主要在实施和测试没有兑现任务全文；“有任务”和新增补缺任务不是完成证据。阶段检查点/单项任务多数有明确可验收描述，但下列验收方法或范围没有落实：

| 审查点 | 结论 |
|---|---|
| 原始需求/当前spec | 当前分期明确替代历史；不能把长期偏好、仅旅游、探索等算缺陷。计划仍称“全部未开始”，与实际不一致，但本轮不修改。 |
| 配置/维护统一写入阻断 | REST通用约定明确要求；实现没有统一门禁。T093/T095与T135未列清全入口反例，P14/P15直接暴露，建议新增N01。 |
| T035 / SC-007 | 任务要求两个流程各改三次；实际01-workflow-versions只对技术选型一个流程循环三次。标准存在，验收不足。 |
| T050 / SC-001/006/013 | 要求三输入、四喜好、六确认；实际02-requirements只有普通聊天、无偏好和空消息三例。 |
| T070 / SC-006 | 要求三结构/两喜好及无合格/部分成果；当前三项E2E集中读取、失败重取、删除，不能证明目标矩阵。 |
| T073 / SC-017 | 标准明确要求实际下游模型≤1；测试仅计runtime.submit，未计同期澄清网关，空请求区间循环也可通过。验收方法偏离需求。 |
| T021 | 多项测试无实际断言，且先要求已有采用证据再验证；运行无失败也不能形成可信采用结论。 |
| T086–T100等未勾选任务 | 即使存在部分文件/测试，仍按未完成；本轮不自动改勾选，也不把未完成项说成虚假完成。 |
| T101 | 仓库内构建/新数据目录与源代码外干净安装不同；独立分发资源缺口见D28，本轮未运行发布包安装。 |
| T023“跨root去重”措辞 | spec要求跨关联执行核验，独立根研究的去重范围待明确。建议澄清键和授权范围；不据此推断独立研究必须共用结果，不单独算代码缺陷。 |

## 4. 测试执行记录

工具链实测：CPython3.12.9（全部Python经uv）、Node24.21.0、npm11.19.0。默认命令加--offline/--no-sync，沿用现有依赖。测试使用隔离临时库；补充E2E使用审计目录新库，迁移至0014并导入两流程，Mock服务绑定127.0.0.1:18123，结束已关闭。没有删除或使用用户正式数据目录。

### 4.1 命令与结果

| 检查 | 命令/入口 | 结果与证据 |
|---|---|---|
| 后端静态 | `uv --no-cache run --offline --no-sync --project code/backend --python 3.12 ruff check --no-cache code/backend` | **失败4处**；[.cache/audits/2026-10-20-phase1/backend_ruff.log](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/backend_ruff.log) |
| 后端类型 | 同uv前缀，`mypy --cache-dir .cache/audits/2026-10-20-phase1/mypy code/backend/src` | 通过，102source files；[.cache/audits/2026-10-20-phase1/backend_mypy.log](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/backend_mypy.log) |
| 后端单元/契约/集成 | 同uv前缀，`python -B -m pytest code/backend/tests/unit code/backend/tests/contract code/backend/tests/integration -q -p no:cacheprovider --basetemp <审计临时目录> --junitxml <审计XML>` | **251 passed，70.32s**；unit76/contract85/integration90；[.cache/audits/2026-10-20-phase1/backend-junit.xml](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/backend-junit.xml) |
| 前端类型 | `npm run typecheck --prefix code/frontend` | 通过；[.cache/audits/2026-10-20-phase1/frontend_typecheck.log](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/frontend_typecheck.log) |
| 前端单测 | `npm run test --prefix code/frontend -- --run --reporter=default --reporter=json --outputFile=<审计JSON>` | **43 passed / 8文件**；[.cache/audits/2026-10-20-phase1/frontend-vitest.json](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/frontend-vitest.json) |
| 策略类型/单测 | `npm run typecheck --prefix code/runtime-policy`；`npm run test --prefix code/runtime-policy -- --run …` | 类型通过，**16 passed**；[.cache/audits/2026-10-20-phase1/policy-vitest.json](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/policy-vitest.json) |
| 前端生产构建 | `npm run build --prefix code/frontend` | 通过，88modules；[.cache/audits/2026-10-20-phase1/frontend-build.log](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/frontend-build.log) |
| 新库迁移/种子 | 同uv前缀，`alembic -c code/backend/alembic.ini upgrade head`；`python -B -m personal_agent.bootstrap.seed` | 临时库0014、两流程；另2个现有迁移用例通过 |
| 锁定浏览器E2E | `PERSONAL_AGENT_E2E_NO_SERVER=1`，端口18123；`npm run test:e2e --prefix code/frontend -- --reporter=list,json --max-failures=1` | **环境阻塞**：r1248 headless shell不存在，23项未执行；[.cache/audits/2026-10-20-phase1/e2e-pinned.json](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/e2e-pinned.json) |
| 补充浏览器E2E | 显式指定本机r1234，既有配置不变；`--reporter=list,json --global-timeout=600000` | **19 passed / 5 failed，109.49s**；[.cache/audits/2026-10-20-phase1/e2e-supplementary.json](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/e2e-supplementary.json) |
| live默认禁用 | 同uv前缀，`python -B -m pytest code/backend/tests/live -q -p no:cacheprovider` | **10 skipped**（采用7、交付3）；[.cache/audits/2026-10-20-phase1/live-disabled-junit.xml](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/live-disabled-junit.xml) |
| 独立审计反例 | stdin内存代码、固定响应、临时DB，无新增项目测试源文件 | **P01–P18**；[.cache/audits/2026-10-20-phase1/probes.json](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/probes.json)、[.cache/audits/2026-10-20-phase1/boundary-probes.json](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/boundary-probes.json) |

最初沙箱内出现Python启动0xc0000142与Vitest spawn EPERM；沙箱外重跑后已完成。这些是环境启动障碍，不算业务测试失败。未安装浏览器、未绕过live授权。前端单测有RouterLink stub警告，但断言通过；没有把警告当失败。

未执行：真正的Harness采用、真实来源/付费模型研究、目标用户独立三次操作、规定12次稳定性完整矩阵、发布包源代码外干净安装、r1248正式E2E。没有运行覆盖率仪表，故不报告行/分支覆盖率百分比；矩阵评估的是需求场景覆盖。

### 4.2 补充E2E五项失败归因

| 用例 | 失败证据 | 审计归因 |
|---|---|---|
| [complete-journey:14](/C:/Code/spiderAgent/code/frontend/tests/e2e/complete-journey.spec.ts:14) | heading名“文档”同时匹配h1文档和h2文档正文，strict violation | 测试定位器不精确；不据此否认页面已打开。 |
| [complete-journey:44](/C:/Code/spiderAgent/code/frontend/tests/e2e/complete-journey.spec.ts:44) | 查找“必要信息”失败；实际组件显示“仍缺少：topic” | 文案断言与实现不一致，不能证明缺失阻断坏。 |
| [recovery-branches:40](/C:/Code/spiderAgent/code/frontend/tests/e2e/recovery-branches.spec.ts:40) | 要求终态run取消按钮enabled，实际disabled | 测试与服务端allowed_actions相矛盾；当前禁用是正确行为。 |
| [stop-budget-serial:13](/C:/Code/spiderAgent/code/frontend/tests/e2e/stop-budget-serial.spec.ts:13) | 同样要求已完成run取消enabled | 测试错误，不代表运行中停止已经验收。 |
| [stop-budget-serial:35](/C:/Code/spiderAgent/code/frontend/tests/e2e/stop-budget-serial.spec.ts:35) | 无待处理求助时查找未渲染的“不会自动继续” | 错误场景/文案断言；尚未验收开放求助处置。 |

全部失败截图、错误上下文和trace保留于 [补充E2E证据目录](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/e2e-supplementary-assets)。五项失败仍使测试门禁不通过，但没有被冒充成五项应用功能缺陷。

补充测量：已提交事件展示24样本，p50=111ms、p95=246ms；停止反馈=29ms。二者在旧版r1234+本机Mock条件下通过，只覆盖事件列表/按钮反馈口径，不证明完整状态快照、真实供应商或锁定浏览器性能。

## 5. 缺陷明细（全部有实际代码证据）

### D01 · CRITICAL · 真实研究路径仍不可执行

来源：FR-028；US10/AC6；T020/T038/T055/T056/T095。

代码：[runtime.py · create_session()](/C:/Code/spiderAgent/code/backend/src/personal_agent/adapters/harness/runtime.py:120)；[lifespan.py · build_context()](/C:/Code/spiderAgent/code/backend/src/personal_agent/bootstrap/lifespan.py:157)；[public-topic-research.json](/C:/Code/spiderAgent/code/backend/workflows/public-topic-research.json:1)。

LiveRuntime.create_session 在通过门禁后仍无条件抛 RUNTIME_ADAPTER_INCOMPLETE，submit/model_transport 也未实现；live 装配使用 UnavailableToolService。两种子流程的域名仍为 example.org/example.net/vendor.example。Mock 正常不构成真实研究能力。

验证：源码直接证明占位；10 项 live 测试本轮跳过。没有访问真实供应商，未推断真实来源质量。

建议：完成真实 SDK/模型网关/MCP/工具装配并替换演示来源范围；经独立采用门禁后用两个流程实际交付。 已有任务：T121。

### D02 · CRITICAL · 模型调用绕过统一网关且并发占槽可突破

来源：FR-033；T014/T016/T073。

代码：[engine.py · _run_agent()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/engine.py:280)；[model_gateway.py · reserve()](/C:/Code/spiderAgent/code/backend/src/personal_agent/adapters/harness/model_gateway.py:94)；[budget_models.py · ModelRequest()](/C:/Code/spiderAgent/code/backend/src/personal_agent/adapters/persistence/budget_models.py:69)。

AgentTask 直接调用 RuntimePort，未通过 ModelGateway；reserve 采用先查后插，数据库无非终结请求唯一约束。

验证：P08：完成研究后模型消耗=2，但 model_requests 表=0；P18：同步两个查询后的并发 reserve 均成功，留下 2 条非终结请求。只控制调度时间，未替换业务返回值。

建议：统一所有实际模型调用，原子持久占槽；添加研究与澄清重叠、同刻 reserve、UNKNOWN 重启反例。 已有任务：T104。

### D03 · HIGH · 重试与工具预算会漏记，恢复会重新获得尝试次数

来源：FR-024/033；T015/T059。

代码：[engine.py · _execute_with_retries()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/engine.py:188)；[engine.py · _run_tool()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/engine.py:388)；[budget_service.py · reserve()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/budget_service.py:180)。

每次重试仍使用同一 run:step:model/tool 操作键，预算服务会幂等复用已有条目；工具 actual 只结算 tool_calls。attempt offset 仅调整编号，while 仍重新运行完整 max_attempts，未采用节点更小的 max_attempts。

验证：现有 budget 算术与重试样例通过，但没有验证失败后每次真实调用均计费、resume 后总尝试数不增加；源码路径可直接核对。

建议：按真实调用独立记账，结算全部维度，限制累计尝试数并保留 UNKNOWN 预留。 已有任务：T105。

### D04 · HIGH · 时间和 deadline 未贯通，上限触发未创建求助

来源：FR-024；US9/AC9–10；T011/T015/T078。

代码：[engine.py · _run_agent()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/engine.py:280)；[engine.py · _fail()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/engine.py:957)；[config.py · BudgetLimits()](/C:/Code/spiderAgent/code/backend/src/personal_agent/bootstrap/config.py:40)。

RuntimeTask/ToolRequest deadline 传当前 now，未在节点等待外层执行时限；active_ms 没有生产累计路径。预算预留异常流入 _fail，缺 BUDGET_LIMIT 求助事务。

验证：P08 的 active_ms=0 仅作辅助，静态搜索确认没有累计调用；P12：模型额度置零后状态 FAILED、原因 BUDGET_ASSISTANCE_REQUIRED、求助数=0。

建议：接入可控计时与正确 deadline，触顶进入 WAITING_USER 并保存完整求助，仅明确追加和继续后恢复。 已有任务：T106。

### D05 · HIGH · 必填与节点输入、绑定契约未完整校验

来源：FR-002/006/007；T044/T058/T059。

代码：[requirements.py · missing_fields()](/C:/Code/spiderAgent/code/backend/src/personal_agent/domain/requirements.py:94)；[requirement_confirmation.py · _recompute()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/requirement_confirmation.py:631)；[engine.py · _dispatch()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/engine.py:236)；[engine.py · _run_tool()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/engine.py:388)。

user 来源的 null 不计缺失；确认前只检查顶层 required 存在和已有 conflicts，不校验值类型/范围/嵌套 schema。引擎没有统一的依赖与输入 schema 验证，工具 arguments_bindings 被直接转换成字典而非求值。

验证：P01：required topic=None 返回 missing=[]；现有用例只覆盖 default 来源 null。表达式绑定单测通过不等于调度器已使用绑定。

建议：在确认及节点边界执行结构/语义校验，解析所有绑定并拒绝缺前置与未知输入。 已有任务：T107。

### D06 · HIGH · 需求分析丢失流程、历史上下文和助手追问

来源：FR-001/002/010；T044/T048。

代码：[requirement_analysis.py · analyze()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/requirement_analysis.py:79)；[requirement_analysis.py · _apply()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/requirement_analysis.py:220)；[RequirementsPage.vue · send()](/C:/Code/spiderAgent/code/frontend/src/pages/RequirementsPage.vue:107)。

定义的 ANALYSIS_SYSTEM_PROMPT 未发送，请求只有本轮 user 文本；所选流程/旧字段/历史问答缺失。_apply 不保存 summary 和 clarifying_questions，也不产生助手消息。

验证：P09：第二轮请求只含 Second input，一共仅保存两条 user 消息，给定的追问未持久化。

建议：发送最小必要流程和历史上下文，严格验证输出并保存/展示追问、依据与取舍回答。 已有任务：T122。

### D07 · HIGH · 确认后无法聊天纠正，完整摘要未覆盖所有确认信息

来源：FR-004/031；US1/AC4/6；US11/AC5；T043/T045/T058。

代码：[requirements.py · assert_editable()](/C:/Code/spiderAgent/code/backend/src/personal_agent/domain/requirements.py:74)；[requirement_confirmation.py · append_message()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/requirement_confirmation.py:220)；[requirement_confirmation.py · _recompute()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/requirement_confirmation.py:631)；[coordinator.py · start()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/coordinator.py:67)。

CONFIRMED 不在可编辑状态，UI 也禁用输入；旧确认失效函数并不能被完整服务链调用。摘要渲染未接 output_spec，start 未完整比较当前 draft revision/确认内容及工作流最新变化。

验证：P10：确认后 append_message 抛 DRAFT_STATE_INVALID；test_editing_a_confirmed_draft_invalidates_the_confirmation 只测纯函数，未覆盖服务拒绝。

建议：允许未消费确认通过聊天作废后重确认，摘要绑定全部要求；启动原子验证 revision/hash/版本/政策。 已有任务：T108/T123。

### D08 · HIGH · A→B→A 保存错误复用旧版本

来源：FR-020/021；T030/T032。

代码：[workflow_registry.py · save_version()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/workflow_registry.py:166)。

按该流程所有历史 definition_hash 查重，相同历史内容直接返回旧记录；一次当前内容发生变化的保存没有形成新版本，也不会默认展示此次 A。

验证：P07：依次保存 A/B/A，第三次 returned_version=1、created=false、总历史=2。

建议：仅当前内容无变化时复用；返回历史内容的修改也新增版本与时间摘要。 已有任务：T124。

### D09 · HIGH · 确认的偏好排序在真实整理链失效

来源：FR-010；SC-006；T057/T064。

代码：[research_evidence.py · evaluate_candidates()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/research_evidence.py:87)；[research_evidence.py · order_by_priority()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/research_evidence.py:115)。

evaluate_candidates 构造新候选时丢弃 attributes，而 order_by_priority 只读取 attributes，所有候选落入相同排序键。

验证：P03：价格优先和速度优先都返回 cheap,fast，attributes 均未保留。现有排序单测直接输入未经过归一化的候选，未覆盖组合链。

建议：保留经验证的属性并按已确认顺序比较，使用同材料相反顺序验证排序或理由变化。 已有任务：T125。

### D10 · CRITICAL · 合格推荐不核对实际硬约束全集，来源引用未解析

来源：FR-009/010/012；SC-005；T057/T065。

代码：[research_evidence.py · evaluate_candidates()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/research_evidence.py:87)；[research_evidence.py · record()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/research_evidence.py:140)；[document_content.py · validate()](/C:/Code/spiderAgent/code/backend/src/personal_agent/domain/document_content.py:46)。

只要模型返回的那几项 constraints 全为 SATISFIED 就判合格，函数未接实际确认约束集，遗漏条件无法判 UNKNOWN；事实 source_id 非空即可，不核实是否对应已存可用来源。

验证：P04：仅自报 budget=SATISFIED 即进入 qualified，接口无实际约束参数。源码显示 source_rows 只用于汇总，不用于验证 finding.source_ids。未声称已发生真实错误推荐。

建议：把全部确认约束与来源索引传入判定，缺项/无依据/虚构引用均未知或未证实。 已有任务：T120。

### D11 · HIGH · 本次文档语言详略被忽略，格式和完整程度判定不足

来源：FR-011/012；SC-005/006；T065–T067。

代码：[artifact_service.py · deliver()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/artifact_service.py:62)；[document_content.py · decide_completeness()](/C:/Code/spiderAgent/code/backend/src/personal_agent/domain/document_content.py:185)；[markdown.py · _render_checklist()](/C:/Code/spiderAgent/code/backend/src/personal_agent/adapters/rendering/markdown.py:159)；[markdown.py · _render_comparison()](/C:/Code/spiderAgent/code/backend/src/personal_agent/adapters/rendering/markdown.py:199)。

交付仍用服务默认 zh-CN/standard。清单/对比表不包含完整目标和约束，brief 会省略缺口；decide_completeness 不核对全部必需栏目/硬约束，deliver 未检查全部 required_output_step_ids。

验证：P11：确认 en/brief，产物 rendered 仍为 zh-CN/standard。现有渲染测试证明局部函数能变换语言，不证明交付服务采用用户值。

建议：执行前协商格式，交付传入本次值；三结构和简版保留必需栏目、理由、来源及未知，按完整策略判终态。 已有任务：T126。

### D12 · HIGH · 检查点与恢复无法重建完整研究证据

来源：FR-025；T017/T023/T077。

代码：[engine.py · _start_attempt()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/engine.py:551)；[engine.py · _finish_attempt()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/engine.py:618)；[engine.py · _resume_point()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/engine.py:721)；[recovery.py · _checks()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/recovery.py:186)。

attempt input_json={}、session_link_id=None，研究未持久 SessionLink；完成 attempt、StepRun/转换分别提交。resume 清空 evidence/source 后只恢复 outputs，未回填交付依赖的证据；恢复仅校验 artifact 文件，不逐项校验保存的 output_hash。

验证：17 项恢复测试通过的是各自夹具路径；没有完成研究节点后只剩 Output 的内容等价性断言。源码可直接定位三个分离提交和未重建字段。

建议：建立原子检查点、真实会话/输入关联，重建来源证据并核对全部依赖 hash/schema/时效。 已有任务：T109。

### D13 · CRITICAL · 未核验写入仍可宣布暂停并释放执行槽

来源：FR-024/025/033；T076/T081。

代码：[stop_control.py · converge()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/stop_control.py:126)；[stop_control.py · _in_flight_count()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/stop_control.py:191)；[engine.py · _finish_attempt()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/engine.py:618)。

converge 仅统计 RUNNING StepAttempt，未读模型 UNKNOWN 或整条 root 未决 SideEffectOperation；无 attempt 即 PAUSED/CANCELLED 并释放槽。结果提交也未全面匹配当前 epoch。

验证：P13：存在持久 INTENT 时 request_pause 返回 PAUSED，slot_released=true。后续恢复检查阻止该 run 不弥补已释放槽的切换风险。

建议：真实在途和写入收敛后才释放槽，15 秒未确认如实阻塞，终态/epoch 栅栏隔离迟到提交。 已有任务：T110。

### D14 · HIGH · 节点分支拒绝无变更重跑，启动又忽略确认的起点

来源：FR-032；SC-014/015；T075/T079/T080。

代码：[branches.py · compute_impact()](/C:/Code/spiderAgent/code/backend/src/personal_agent/domain/branches.py:85)；[branch_service.py · confirm()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/branch_service.py:207)；[engine.py · run()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/engine.py:114)；[branch_analysis.py · _artifact_usable()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/branch_analysis.py:217)。

无 changed_paths 强制 BLOCKED；confirm 新分支提交 resume=False，引擎始终从 entry_step 开始。复用判断不核验 blob 过期或实际 hash，差异只算 fields，忽略偏好/输出要求。

验证：P02：无修改首节点重跑被 BLOCKED。启动参数与 entry_step 路径构成静态确定证据；现有 E2E 只断言 blocked，没有成功分叉正例。

建议：允许无修改重跑，实际从确认 fork 点启动；限定已执行较早节点和完整有效前缀，覆盖再次分叉及输出/偏好变化。 已有任务：T111。

### D15 · HIGH · 工具授权令牌可跨执行身份使用

来源：FR-026；T054。

代码：[scope.py · validate()](/C:/Code/spiderAgent/code/backend/src/personal_agent/adapters/tools/scope.py:79)；[mcp_service.py · execute()](/C:/Code/spiderAgent/code/backend/src/personal_agent/adapters/tools/mcp_service.py:51)。

grant 含 run_id/attempt_id，但 validate 只收 token/tool/epoch/now；execute 不比较 request 所属身份或数据库当前 epoch。

验证：P16：令牌发给 run-A/attempt-A，run-B/attempt-B 传相同 token/epoch 调用 content.extract 返回 ok。

建议：校验完整 owner 与持久当前状态，停止/换尝试撤销旧 token，并严格参数 schema。 已有任务：T113。

### D16 · HIGH · 持久 JSON 缺版本校验，部分业务引用和 CAS 未落实

来源：T010/T012/T013/T014；宪章技术约束。

代码：[base.py · JsonObject()](/C:/Code/spiderAgent/code/backend/src/personal_agent/adapters/persistence/base.py:49)；[execution_models.py · WorkflowRun()](/C:/Code/spiderAgent/code/backend/src/personal_agent/adapters/persistence/execution_models.py:35)；[recovery.py · resume()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/recovery.py:94)。

JsonObject 仅验证 dict，JsonValue 不校验；root/parent、部分 slot/session/event/ledger 业务引用无 FK。多处可变聚合先检查 revision 再普通 ORM 写入，未使用已有 cas_update。

验证：P06：schema_version=999 和未知字段写入类型装饰器被接受。迁移测试只验证表存在/升级，不验证这些业务约束。

建议：补版本化读写校验、引用约束与实际 CAS，新增兼容迁移及并发/损坏状态反例。 已有任务：T112。

### D17 · HIGH · 同键并发请求会重复产生业务对象

来源：T024；宪章 II。

代码：[middleware.py · __call__()](/C:/Code/spiderAgent/code/backend/src/personal_agent/api/middleware.py:71)；[middleware.py · _store()](/C:/Code/spiderAgent/code/backend/src/personal_agent/api/middleware.py:269)。

IdempotencyMiddleware 执行业务并发送响应后才插 RequestRecord，两个相同请求可都先读到不存在再执行，唯一约束只吞第二条记录冲突。

验证：P17：在两个 _load 完成处设置调度屏障，两请求均 201，生成不同的两个 draft ID。没有伪造响应。

建议：执行前持久原子占用 key/hash，关联业务提交和响应恢复，覆盖同键并发/响应丢失/进程中断。 已有任务：T119。

### D18 · HIGH · 文本日志与模型结果不能保证脱敏或不保存推理

来源：FR-026；A3；T020/T025/T097。

代码：[logging.py · format()](/C:/Code/spiderAgent/code/backend/src/personal_agent/bootstrap/logging.py:77)；[events.py · redact()](/C:/Code/spiderAgent/code/backend/src/personal_agent/adapters/harness/events.py:49)；[engine.py · _finish_attempt()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/engine.py:618)。

日志 formatter 对字符串原样返回；按敏感键过滤的 redact 无法处理正文中的秘密。_finish_attempt 仅去除 __ 前缀，含 reasoning 的宽松模型输出可保存。

验证：P05：合成非真实 secret 出现在日志 message。只记录此合成泄露复现，不声称项目含真实泄露凭据；现有测试大多使用无秘密正文或敏感字段名。

建议：在落盘和出口使用字段白名单及受控文本处理，丢弃推理字段，验证真实输入→日志/SSE/文档/备份全链。 已有任务：T115。

### D19 · HIGH · 删除与保留未覆盖所有内容副本和读取

来源：FR-027；A4；T089/T091。

代码：[data_lifecycle.py · request_deletion()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/data_lifecycle.py:107)；[data_lifecycle.py · apply_retention()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/data_lifecycle.py:232)；[artifact_service.py · get()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/artifact_service.py:179)；[run_snapshots.py · serialize_steps()](/C:/Code/spiderAgent/code/backend/src/personal_agent/application/run_snapshots.py:147)。

删除只改变 artifact 状态和部分 blob；document_json、对话、来源、嵌入结果及幂等响应仍保留。retention 主要启动/维护执行，普通读取不统一校验 expires_at，业务 90 天没有覆盖全部实体，失效备份文件只变状态。

验证：4 项生命周期样例通过；它们没有覆盖所有读取副本和长时间运行不重启时的到期。源码中无相关表清理/统一读取屏障。

建议：按资料而非单行覆盖访问、复用和物理删除；持久清理任务、共享引用和备份都应遵守保留策略。 已有任务：T117。

### D20 · HIGH · 恢复安全副本、删除墓碑与校验清单不满足可靠回退

来源：FR-025/027；T092/T096。

代码：[restore.py · restore()](/C:/Code/spiderAgent/code/backend/src/personal_agent/bootstrap/restore.py:49)；[restore.py · _apply_tombstones()](/C:/Code/spiderAgent/code/backend/src/personal_agent/bootstrap/restore.py:241)；[backup.py · _verify_files()](/C:/Code/spiderAgent/code/backend/src/personal_agent/bootstrap/backup.py:279)。

safety_db 用 shutil.copy2 活跃主库，未包含 WAL；当前墓碑只应用到 staged artifacts/reuse 行，未把完整 DeletionRecord 带回恢复库；verify 主要 PRAGMA 与 blob hash，未绑定 DB 内容摘要。异常回退吞错后仍说保留当前数据。

验证：4 项备份恢复测试通过，但未验证有 WAL 未检查点写入的失败回退、两次恢复后墓碑仍在及数据库内容被改但 PRAGMA 仍正常。此处报告协议缺口，不声称已发生用户数据损失。

建议：用 SQLite backup API 生成安全副本，合并持久墓碑，绑定 DB/schema/blob 清单并验证连续恢复及失败回退。 已有任务：T118。

### D21 · HIGH · 分支 UI 成功链路不可达

来源：FR-032；T085。

代码：[BranchProposalPanel.vue · confirm()](/C:/Code/spiderAgent/code/frontend/src/components/BranchProposalPanel.vue:117)；[BranchProposalPanel.vue · validate()](/C:/Code/spiderAgent/code/frontend/src/components/BranchProposalPanel.vue:95)；[RequirementsPage.vue · confirm()](/C:/Code/spiderAgent/code/frontend/src/pages/RequirementsPage.vue:154)。

分支确认请求不存在的 /api/v1/policies；派生草稿页固定 action=START；提案只在组件 ref，跳转要求页再返回丢失，validate 仍传旧 effective_step_id。

验证：E2E recovery-branches 只有 blocked 提案正例，本轮该例通过不证明能建立分支。路由表只有 /policies，可直接核对。

建议：保留提案路由状态，使用正确政策地址和 BRANCH 确认，展示完整要求并执行用户选择的前移起点。 已有任务：T128。

### D22 · MEDIUM · 事件补读游标和实时业务状态刷新不完整

来源：FR-024；T082/T083/T086。

代码：[run.ts · applyEvents()](/C:/Code/spiderAgent/code/frontend/src/stores/run.ts:135)；[run.ts · backfill()](/C:/Code/spiderAgent/code/frontend/src/stores/run.ts:153)；[events.py · event_history()](/C:/Code/spiderAgent/code/backend/src/personal_agent/api/events.py:191)；[RunProgressPage.vue](/C:/Code/spiderAgent/code/frontend/src/pages/RunProgressPage.vue:1)。

applyEvents 不推进 lastSequence 或更新 run 状态；backfill 只读一页，定时 load 可能跳过未补完历史。请求 limit=1000 的后端多取一行被 MAX_BUFFER 截断，next_cursor 可能错误。

验证：现有事件单测和六项后端契约通过，但未覆盖 1001 条/多页竞态；补充浏览器事件列表 p95=246ms、停止反馈29ms通过，仅证明该两个测量口径，不证明业务状态亦及时。

建议：独立推进游标、补完全部页并刷新业务快照，修复最大分页边界和 reset/重连处理。 已有任务：T127。

### D23 · HIGH · 首次配置与维护状态未阻止业务写入

来源：contracts/rest-api: 通用约定；T093/T095。

代码：[deps.py · context()](/C:/Code/spiderAgent/code/backend/src/personal_agent/api/deps.py:17)；[requirements.py · create_requirement()](/C:/Code/spiderAgent/code/backend/src/personal_agent/api/requirements.py:90)；[settings.py · setup_status()](/C:/Code/spiderAgent/code/backend/src/personal_agent/api/settings.py:57)；[app.py · create_app()](/C:/Code/spiderAgent/code/backend/src/personal_agent/api/app.py:34)。

API 公共依赖/中间件未执行 SETUP_REQUIRED 或维护门禁；setup completed 仅看确认行存在，不要求两个政策同意字段均真。

验证：P14：setup_completed=false 仍可创建 draft，返回201；P15：MAINTENANCE 下仍返回201。检索执行入口不存在相关阻断。

建议：为模型/工具和全部业务写入口加统一配置/维护门禁；核验实际政策同意，保留允许的配置与只读操作。 已有任务：T135；另建议新增 N01 统一写入门禁。

### D24 · HIGH · 真实运行时采用测试含无断言占位

来源：T021；技术采用门禁。

代码：[test_runtime_adoption.py · test_gateway_has_no_provider_bypass()](/C:/Code/spiderAgent/code/backend/tests/live/test_runtime_adoption.py:52)；[test_runtime_adoption.py · test_tool_scope_is_denied_even_when_registered_later()](/C:/Code/spiderAgent/code/backend/tests/live/test_runtime_adoption.py:46)。

多个测试仅 importorskip 并在无 adoption_evidence 时跳过；已有证据时函数直接返回，没有实际验证网关旁路、越权、推理文件、UNKNOWN 或会话恢复。

验证：本轮10项 live 均 skipped；独立检查函数体确认空测试，因此不能把未来零失败等同采用通过。

建议：写可独立建立证据的实测断言，成功后生成版本/profile 绑定记录，失败/跳过保持禁用。 已有任务：T129。

### D25 · HIGH · 必需稳定性与发布证据缺失

来源：SC-001/012–017；T086–T088/T096/T098/T102/T103。

代码：[test_fixed_research.py · prepare_confirmation()](/C:/Code/spiderAgent/code/backend/tests/integration/test_fixed_research.py:49)；[complete-journey.spec.ts](/C:/Code/spiderAgent/code/frontend/tests/e2e/complete-journey.spec.ts:1)；[tasks.md](/C:/Code/spiderAgent/specs/001-workflow-research-assistant/tasks.md:1)。

没有 test_phase1_stability.py、acceptance-matrix.md、release-evidence.md；三输入完整闭环、12次重复研究、成功分支及上限求助等全矩阵没有证据，部分实现测试直接改内部数据库准备确认。

验证：251+43+16 通过不能替代缺失矩阵；24项补充E2E仅19通过且多数未覆盖要求的组合。T098/T102/T103 本来仍未勾选，按未完成验收报告，不冒充虚假完成。

建议：补齐全矩阵并保留逐例需求—输入—版本—事件—文档证据；真实验证授权不足明确列未运行。 已有任务：T130/T131/T133。

### D26 · MEDIUM · 已有测试会漏检关键并发，还包含错误端到端断言

来源：T035/T050/T070/T073/T086/T087/T088。

代码：[test_serial_execution.py · test_retry_after_timeout_never_overlaps_model_requests()](/C:/Code/spiderAgent/code/backend/tests/integration/test_serial_execution.py:348)；[complete-journey.spec.ts](/C:/Code/spiderAgent/code/frontend/tests/e2e/complete-journey.spec.ts:1)；[recovery-branches.spec.ts](/C:/Code/spiderAgent/code/frontend/tests/e2e/recovery-branches.spec.ts:1)；[stop-budget-serial.spec.ts](/C:/Code/spiderAgent/code/frontend/tests/e2e/stop-budget-serial.spec.ts:1)。

串行测试统计 runtime.submit，不统计澄清网关；ModelRequest spans 为空时循环断言也能通过。E2E 文档标题模糊匹配两个标题，缺失文案与页面不一致，两个样例要求终态取消按钮 enabled，另一个在无求助场景寻找未渲染警告。

验证：补充浏览器运行5失败：strict locator1、缺文案2、错误取消断言2。均已由源码/错误上下文归因于测试问题，未据此断言应用对应功能坏；截图和trace保留。

建议：修正断言并补实际下游请求计数、非空样本、成功恢复/分支/求助和端到端负例。 已有任务：T104/T131；另建议新增 N02 现有失败断言修订。

### D27 · MEDIUM · 锁定浏览器缺失，声明与发布可复现要求未完全落实

来源：AGENTS.md §2–3；T006/T101。

代码：[playwright.config.ts](/C:/Code/spiderAgent/code/frontend/playwright.config.ts:1)；[pyproject.toml](/C:/Code/spiderAgent/code/backend/pyproject.toml:1)；[package.json](/C:/Code/spiderAgent/code/frontend/package.json:1)。

Playwright=1.64.1 要求 Chromium r1248，本机只安装 r1208/r1234；直接依赖仍为 >=、^、~ 范围，锁文件提供了实际解析版本，但清单未按计划精确固定。

验证：默认E2E在启动前因 r1248 缺失阻塞，23项未执行；r1234运行只记补充结果。没有下载浏览器或升级依赖。

建议：使用与锁文件一致的浏览器重跑全部E2E，并精确固定直接依赖、重生成锁文件与指纹。 已有任务：T136。

### D28 · HIGH · 交付包未收齐运行资源，独立安装验收无证据

来源：T101；plan: 可复现交付。

代码：[config.py · load_config()](/C:/Code/spiderAgent/code/backend/src/personal_agent/bootstrap/config.py:198)；[lifespan.py · _observations()](/C:/Code/spiderAgent/code/backend/src/personal_agent/bootstrap/lifespan.py:79)；[package.ps1](/C:/Code/spiderAgent/code/scripts/package.ps1:1)；[pyproject.toml](/C:/Code/spiderAgent/code/backend/pyproject.toml:1)。

wheel 仅 packages=src/personal_agent；代码从包外 repo 路径读 Alembic/种子/fixtures/profile；package.ps1 对策略插件仅复制 dist，未复制 profile.json。

验证：本轮只构建前端并从仓库启动，未生成发布包或在干净安装目录验证；源码明确缺少资源复制/包资源定位，安装结果标无法验证，不声称实际打包已失败。

建议：收齐资源和固定依赖，消除源码 tests/fixtures 运行依赖，干净目录验证迁移、种子、UI与离线研究。 已有任务：T134。

### D29 · MEDIUM · 强制静态检查当前失败

来源：T007/T102；宪章 IV。

代码：[test_backup_restore.py · test_failed_migration_restores_the_verified_backup()](/C:/Code/spiderAgent/code/backend/tests/integration/test_backup_restore.py:324)；[test_data_lifecycle.py](/C:/Code/spiderAgent/code/backend/tests/integration/test_data_lifecycle.py:1)。

ruff 报 test_backup_restore.py:327 未使用 context；test_data_lifecycle.py:9/45 导入顺序、:215 多余括号，共4项。

验证：本轮 ruff exit=1，结果写 backend_ruff.log；没有运行 --fix，其他类型/自动化通过也不抵消这道门禁。

建议：修正4处静态检查问题并重跑相应检查，发布证据明确记录实际结果。 已有任务：T133。

### D30 · HIGH · 网络预检查未约束实际解析连接，响应上限只在下载后截断

来源：FR-026；T052/T054/T055/T097。

代码：[scope.py · validate_url()](/C:/Code/spiderAgent/code/backend/src/personal_agent/adapters/tools/scope.py:128)；[http_fetch.py · fetch()](/C:/Code/spiderAgent/code/backend/src/personal_agent/adapters/tools/http_fetch.py:35)；[browser_read.py · read()](/C:/Code/spiderAgent/code/backend/src/personal_agent/adapters/tools/browser_read.py:23)。

validate_url 单独解析公共IP，然后 httpx/浏览器按原域名独立连接，未把已检查解析绑定到连接；client.get 完整缓冲响应后才取 max_body_bytes。浏览器路由只验证目标，未设置受控 DNS/连接层。

验证：现有 rebinding 测试模拟第二跳返回私网，未覆盖第一次校验后连接重新解析。仅报告代码层保护缺口，未连接任何真实私网目标。

建议：连接层绑定已验证地址并保持TLS/SNI，覆盖子资源/重定向；有界流式读与可协作停止。 已有任务：T114。

### D31 · HIGH · 数据和备份目录缺少约定的 ACL 设置及自检

来源：T025；宪章 V；plan: 本机文件保护。

代码：[config.py · ensure_directories()](/C:/Code/spiderAgent/code/backend/src/personal_agent/bootstrap/config.py:185)；[settings.py · _data_dir_writable()](/C:/Code/spiderAgent/code/backend/src/personal_agent/api/settings.py:46)。

ensure_directories 仅 mkdir，首次配置仅写探针；代码未检查或收紧 Windows ACL，不能证明数据库、日志、会话及备份满足授权边界。

验证：源码搜索 ACL/icacls/chmod 无实现。本轮没有改变系统权限，也未把本机当前 ACL 推断成产品已自动保护。

建议：设置并核验单用户目录权限，继承异常/恢复后权限不安全时给出诊断并拒绝敏感操作。 已有任务：T116。

## 6. 宪章及核心设计决策核查

| 原则/决策 | 证据及结论 |
|---|---|
| 宪章I确定性 | 状态/表达式/固定配置存在；P01/P04/P08/P12/P18与D02–D05/D10否定关键输入、预算、模型槽、可信合格性保证，**失败**。 |
| 宪章II可恢复性 | 17恢复、8写入、4备份例通过；D12–D14/D17/D20及P13/P17反例显示检查点、收敛、分支、幂等回退不完整，**部分实现，关键路径失败**。 |
| 宪章III解耦 | domain/application/ports/adapters/UI分层明确，领域未依赖Vue/FastAPI对象；真实适配与工具结果契约未完成，**部分实现**。未发现需单列缺陷的无依据通用平台扩张。 |
| 宪章IV自动化 | 310项默认测试通过，但ruff失败、正式E2E阻塞且补充5失败；历史“先失败后实现”无法只从当前文件证明，**未达到合并/发布门禁**。 |
| 宪章V数据安全 | 28项安全局部用例通过；P05/P16及D18–D20/D30/D31显示授权、脱敏、生命周期、备份与本机保护不足，**失败**。未读取/展示真实凭据。 |
| plan核心1 JSON/DAG/版本 | 发布校验22例通过，D05/D08说明执行输入与版本保存仍不符，部分实现。 |
| plan核心2 完整确认绑定 | 7需求契约例通过，D07表明修改失效/完整摘要/最新版本未完整落地，部分实现。 |
| plan核心3 停止/取消/求助 | P12/P13直接反例，失败。 |
| plan核心4 原ID恢复/新分支有效复用 | D12/D14/D21，失败。 |
| plan核心5 根预算累计与最坏预留 | 算术11例通过；D02/D03/D04破坏实际全局保证，部分实现。 |
| plan核心6 intent→文件→hash→同事务检查点 | 受管写入8例通过；Output前置成功/分离提交D12，部分实现。 |
| plan核心7 提交后SSE/迟到/UNKNOWN | 基本重放6例通过，D02/D13/D22仍在，部分实现。 |

## 7. 建议修复任务（仅建议，不改tasks）

大部分问题已有Phase11任务。以下建议以缺陷ID对齐，优先复用已有T104–T138，避免机械新增重复编号。每项先补可复现失败用例、再改实现、最后复验；本轮没有执行这些修复。

| 建议 | 缺陷/已有任务 | 最低修复验收目标 |
|---|---|---|
| A01 · CRITICAL | D01；T121 | 完成真实 SDK/模型网关/MCP/工具装配并替换演示来源范围；经独立采用门禁后用两个流程实际交付。 |
| A02 · CRITICAL | D02；T104 | 统一所有实际模型调用，原子持久占槽；添加研究与澄清重叠、同刻 reserve、UNKNOWN 重启反例。 |
| A03 · HIGH | D03；T105 | 按真实调用独立记账，结算全部维度，限制累计尝试数并保留 UNKNOWN 预留。 |
| A04 · HIGH | D04；T106 | 接入可控计时与正确 deadline，触顶进入 WAITING_USER 并保存完整求助，仅明确追加和继续后恢复。 |
| A05 · HIGH | D05；T107 | 在确认及节点边界执行结构/语义校验，解析所有绑定并拒绝缺前置与未知输入。 |
| A06 · HIGH | D06；T122 | 发送最小必要流程和历史上下文，严格验证输出并保存/展示追问、依据与取舍回答。 |
| A07 · HIGH | D07；T108/T123 | 允许未消费确认通过聊天作废后重确认，摘要绑定全部要求；启动原子验证 revision/hash/版本/政策。 |
| A08 · HIGH | D08；T124 | 仅当前内容无变化时复用；返回历史内容的修改也新增版本与时间摘要。 |
| A09 · HIGH | D09；T125 | 保留经验证的属性并按已确认顺序比较，使用同材料相反顺序验证排序或理由变化。 |
| A10 · CRITICAL | D10；T120 | 把全部确认约束与来源索引传入判定，缺项/无依据/虚构引用均未知或未证实。 |
| A11 · HIGH | D11；T126 | 执行前协商格式，交付传入本次值；三结构和简版保留必需栏目、理由、来源及未知，按完整策略判终态。 |
| A12 · HIGH | D12；T109 | 建立原子检查点、真实会话/输入关联，重建来源证据并核对全部依赖 hash/schema/时效。 |
| A13 · CRITICAL | D13；T110 | 真实在途和写入收敛后才释放槽，15 秒未确认如实阻塞，终态/epoch 栅栏隔离迟到提交。 |
| A14 · HIGH | D14；T111 | 允许无修改重跑，实际从确认 fork 点启动；限定已执行较早节点和完整有效前缀，覆盖再次分叉及输出/偏好变化。 |
| A15 · HIGH | D15；T113 | 校验完整 owner 与持久当前状态，停止/换尝试撤销旧 token，并严格参数 schema。 |
| A16 · HIGH | D16；T112 | 补版本化读写校验、引用约束与实际 CAS，新增兼容迁移及并发/损坏状态反例。 |
| A17 · HIGH | D17；T119 | 执行前持久原子占用 key/hash，关联业务提交和响应恢复，覆盖同键并发/响应丢失/进程中断。 |
| A18 · HIGH | D18；T115 | 在落盘和出口使用字段白名单及受控文本处理，丢弃推理字段，验证真实输入→日志/SSE/文档/备份全链。 |
| A19 · HIGH | D19；T117 | 按资料而非单行覆盖访问、复用和物理删除；持久清理任务、共享引用和备份都应遵守保留策略。 |
| A20 · HIGH | D20；T118 | 用 SQLite backup API 生成安全副本，合并持久墓碑，绑定 DB/schema/blob 清单并验证连续恢复及失败回退。 |
| A21 · HIGH | D21；T128 | 保留提案路由状态，使用正确政策地址和 BRANCH 确认，展示完整要求并执行用户选择的前移起点。 |
| A22 · MEDIUM | D22；T127 | 独立推进游标、补完全部页并刷新业务快照，修复最大分页边界和 reset/重连处理。 |
| A23 · HIGH | D23；T135；另建议新增 N01 统一写入门禁 | 为模型/工具和全部业务写入口加统一配置/维护门禁；核验实际政策同意，保留允许的配置与只读操作。 |
| A24 · HIGH | D24；T129 | 写可独立建立证据的实测断言，成功后生成版本/profile 绑定记录，失败/跳过保持禁用。 |
| A25 · HIGH | D25；T130/T131/T133 | 补齐全矩阵并保留逐例需求—输入—版本—事件—文档证据；真实验证授权不足明确列未运行。 |
| A26 · MEDIUM | D26；T104/T131；另建议新增 N02 现有失败断言修订 | 修正断言并补实际下游请求计数、非空样本、成功恢复/分支/求助和端到端负例。 |
| A27 · MEDIUM | D27；T136 | 使用与锁文件一致的浏览器重跑全部E2E，并精确固定直接依赖、重生成锁文件与指纹。 |
| A28 · HIGH | D28；T134 | 收齐资源和固定依赖，消除源码 tests/fixtures 运行依赖，干净目录验证迁移、种子、UI与离线研究。 |
| A29 · MEDIUM | D29；T133 | 修正4处静态检查问题并重跑相应检查，发布证据明确记录实际结果。 |
| A30 · HIGH | D30；T114 | 连接层绑定已验证地址并保持TLS/SNI，覆盖子资源/重定向；有界流式读与可协作停止。 |
| A31 · HIGH | D31；T116 | 设置并核验单用户目录权限，继承异常/恢复后权限不安全时给出诊断并拒绝敏感操作。 |

另建议新增三个明确的验收任务：

- **N01 · HIGH：统一配置/维护写入门禁。** 列出setup未完成及NORMAL/ENTERING/MAINTENANCE/EXITING下全部入口允许矩阵；P14/P15转失败回归，并覆盖start/resume/branch/chat/保存/删除与并发状态切换。未完成配置的业务写入返回SETUP_REQUIRED；维护期间禁止新增模型/工具行动，同时保留允许的只读和配置修复入口。
- **N02 · MEDIUM：固定浏览器与现有端到端门禁恢复。** 安装锁定r1248，在隔离可重复环境修订五个错误断言并全24项通过，再补缺失场景。不得删除失败用例或放宽产品约束冒充修复。
- **N03 · MEDIUM：验收追踪和歧义澄清。** 将92项任务结论、88项需求逐一关联失败用例、修复和复验记录；明确T023跨root去重及T101安装包口径。未实施/未验证项保持未完成，历史TDD证据无法验证则直说。

建议复验顺序：输入/确认和全局模型槽 → 预算/停止/检查点 → 分支/生命周期/安全/备份 → 文档与UI闭环 → 完整离线矩阵和锁定E2E → 经显式授权及费用确认的真实采用与两流程交付 → 干净安装和发布证据。全部一期强制标准满足前，保持验收不通过。

## 8. 审计完整性与附件

保护文件基线：[.cache/audits/2026-10-20-phase1/protected-baseline.json](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/protected-baseline.json)；结束哈希核验：[.cache/audits/2026-10-20-phase1/integrity.json](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/integrity.json)，232文件changed=[]。仅新增审计文档、矩阵与隔离测试产物；前端dist属于本轮生成物，不是源文件修订。审计Mock服务已关闭。

本轮原始逐例结果：[.cache/audits/2026-10-20-phase1/backend-junit.xml](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/backend-junit.xml)、[.cache/audits/2026-10-20-phase1/frontend-vitest.json](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/frontend-vitest.json)、[.cache/audits/2026-10-20-phase1/policy-vitest.json](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/policy-vitest.json)、[.cache/audits/2026-10-20-phase1/e2e-pinned.json](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/e2e-pinned.json)、[.cache/audits/2026-10-20-phase1/e2e-supplementary.json](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/e2e-supplementary.json)、[.cache/audits/2026-10-20-phase1/live-disabled-junit.xml](/C:/Code/spiderAgent/.cache/audits/2026-10-20-phase1/live-disabled-junit.xml)。状态原样保留，不自动改任何任务勾选。

