# Personal-Agent 技术规划说明

**项目名称：** Personal-Agent  
**文档用途：** Spec Kit `/speckit.plan` 技术规划输入  
**技术规划阶段：** 项目初始化、整体架构设计

## 1. 技术目标

根据当前 `spec.md` 中定义的功能需求，为 Personal-Agent 设计一套模块化、可扩展、可追踪、支持断点恢复的 Agent 系统。

核心设计原则：

1. Workflow Engine 负责执行流程的控制，Agent Harness 负责工作流节点内需要模型自主决策的任务。
2. 固定工作流严格遵守工作流定义，不允许 Agent 绕过执行约束。
3. 工作流探索模式允许用户参与决策，并能将探索过程转化为可复用的工作流。
4. 所有任务执行过程都需要有可追踪的执行记录。
5. 系统必须支持暂停、恢复、失败重试以及工作流版本管理。
6. 优先实现单用户、本地运行的 MVP，避免不必要的分布式架构。

## 2. 技术栈

### 2.1 后端

- 主要开发语言：Python 3.12
- Web API：FastAPI
- 数据验证：Pydantic v2
- ORM：SQLAlchemy 2.x
- 数据库迁移：Alembic
- 异步任务：优先使用应用内部任务执行器，不在初期引入 Celery。
- 日志：Python logging，采用结构化日志格式。

### 2.2 前端

- Vue 3 + TypeScript
- Vite 构建
- REST API 负责管理和交互操作
- SSE 推送执行进度及事件
- 提供工作流选择、任务执行状态、历史记录、人工介入、结果展示和工作流探索界面

### 2.3 Agent 执行内核

优先使用 DeepSeek Harness，通过官方 Python SDK 集成。

技术要求：

- 将 DeepSeek Harness 封装在独立的 Agent Runtime Adapter 内。
- 业务模块不直接依赖 Harness 内部数据结构。
- 支持创建 Session、提交任务、获取结果及事件记录。
- 研究并复用 Harness 已有的工具调用、会话管理、插件及持久化能力。
- 对 Harness 无法提供的能力，通过适配层或者外围业务模块实现。
- 保留未来替换 Agent Harness 的可能性。

禁止重新实现 Harness 已经提供且满足需求的 Agent Loop。

### 2.4 数据存储

MVP 使用 SQLite，启用 WAL 模式。

数据库主要保存：

- 工作流定义和版本
- 工作流执行实例
- 工作流节点执行状态
- Agent Session 关联信息
- 工作流探索历史
- 用户偏好
- 执行事件索引
- 文档元数据

大型日志与文档产物保存到本地文件系统。

数据库访问通过 Repository 或明确的数据访问层隔离，为将来迁移 PostgreSQL 保留空间。

### 2.5 浏览器与信息采集

- 浏览器自动化优先采用 Playwright。
- HTTP 请求优先采用 httpx。
- 为浏览器操作、网页搜索、信息提取建立统一 Tool Adapter。
- 浏览器会话应支持合理复用。
- 用户可以查看采集方向、任务进度、来源及重要中间结果。
- 所有外部网页内容均视为不可信输入，不得直接获得系统指令权限。

### 2.6 测试技术

- pytest
- pytest-asyncio
- 使用 Mock Agent Runtime 测试工作流，不要求所有测试调用真实模型。
- 针对状态恢复、重试、版本隔离、权限边界建立集成测试。
- 前端测试优先采用 Vitest。

## 3. 系统架构

建议采用模块化单体架构，明确划分以下模块。

### 3.1 API Layer

负责：

- 接收用户请求
- 工作流选择与管理
- 创建、暂停和恢复任务
- 提供任务状态与执行事件
- 用户决策交互
- 输出文档访问

### 3.2 Workflow Registry

负责：

- 存储工作流定义
- 管理工作流版本
- 校验工作流结构
- 查询与匹配工作流
- 工作流发布和停用

### 3.3 Workflow Engine

负责：

- 创建工作流执行实例
- 解析工作流定义
- 管理节点状态与执行顺序
- 校验节点输入输出
- 调度当前节点执行
- 执行条件分支
- 管理暂停、恢复、重试及失败
- 保存执行状态

Workflow Engine 是整个系统唯一的工作流推进决策者。

### 3.4 Agent Runtime Adapter

负责：

- 封装 DeepSeek Harness Python SDK
- 管理 Agent Session 的创建和生命周期
- 向 Agent 提交当前节点任务
- 根据节点权限限制可执行能力
- 接收并转换执行结果
- 收集 Harness 执行事件

必须区分 Agent 内部推理步骤和 Workflow Engine 的业务步骤。

### 3.5 Exploration Manager

负责：

- 管理探索任务
- 展示 Agent 当前的研究方向
- 接受用户调整意见
- 记录探索过程及人工决策
- 收集成功执行路径
- 生成候选工作流定义

探索过程生成的是工作流草稿，必须经过校验与用户确认后才能发布。

### 3.6 Artifact Service

负责：

- 汇总工具采集结果
- 记录资料来源
- 按用户指定格式组织内容
- 根据用户偏好生成文档
- 保存交付产物

MVP 优先支持 Markdown，其他导出格式采用可扩展的 Renderer。

## 4. 工作流定义

采用声明式工作流定义。

优先选择 YAML 或 JSON 作为工作流的持久化表示，并使用 Pydantic Schema 进行验证。

每个工作流应至少包含：

- workflow_id
- version
- name
- description
- input_schema
- output_schema
- steps
- transition_rules
- execution_policy

工作流节点至少支持：

- AgentTask：Agent 自主执行一个受约束的任务
- ToolTask：执行明确指定的工具
- Condition：根据结果进行分支判断
- HumanReview：等待用户审核或提供方向
- Transform：处理与转换结构化数据
- Output：整理并生成最终结果

MVP 优先实现顺序执行和条件分支。

不允许通过自然语言 Prompt 代替工作流状态控制和节点调度逻辑。

## 5. 状态管理与持久化

工作流执行实例和节点执行实例必须分别管理状态。

建议状态集合：

- PENDING
- RUNNING
- WAITING_USER
- SUCCEEDED
- FAILED
- CANCELLED

如果需要额外的 PAUSED 状态，由技术规划阶段确定其语义和状态转换规则。

每次关键状态变化都需要记录：

- run_id
- step_run_id
- workflow_version
- 时间戳
- 状态变化
- 输入和输出引用
- 错误信息
- Agent Session ID

要求：

- 工作流恢复不依赖模型对历史对话的自行理解。
- 已成功完成的节点不应因恢复操作而自动重复执行。
- 对存在外部副作用的操作设计幂等键或人工核验机制。
- 工作流数据库状态与 Agent Session 记录保持关联。
- 对进程异常退出后的恢复制定明确策略。
- 不能将 Harness 的 Session 恢复直接等同于业务工作流的恢复。

## 6. 固定工作流执行约束

固定工作流必须以工作流定义作为执行依据。

Agent 只接收当前节点对应的目标、上下文、允许使用的工具及输出契约。

要求：

- Agent 不可以直接修改工作流定义。
- Agent 不可以自行跳过业务节点。
- 节点完成后由 Workflow Engine 验证输出，再决定是否推进。
- 工具使用权限必须通过代码层控制，不能只依靠 Prompt。
- 用户修改已发布工作流时，创建新版本。
- 已经启动的执行实例继续引用其启动时的工作流版本。

## 7. 工作流探索与沉淀

探索模式允许 Agent 根据任务目标动态制定研究步骤，并接受用户指导。

探索记录应包含：

- 用户原始目标
- Agent 提出的研究方向
- 工具调用与采集结果
- 用户调整与人工决策
- 执行失败及修正过程
- 最终交付结果

工作流沉淀过程：

1. 分析探索执行轨迹。
2. 识别可复用的成功步骤。
3. 提取步骤输入输出和依赖。
4. 生成声明式工作流草稿。
5. 校验工作流 Schema。
6. 用户审核与修改。
7. 通过测试后发布新版本。

不能直接把历史对话当作可执行工作流。

## 8. 非功能性技术约束

- 初期支持 Windows x64 本地运行。
- 面向单用户使用。
- 优先保证数据一致性、可恢复性和执行可观察性。
- 不在 MVP 引入 Kubernetes、Redis 队列、微服务等非必要组件。
- 模型调用和浏览器操作应设置超时、重试和取消机制。
- 任务日志不应记录明文 API Key 或敏感身份凭据。
- 外部网页与文档内容不得绕过 Agent 工具权限控制。
- 允许后续扩展定时任务和主动通知，但初期不需要实现完整分布式调度系统。

## 9. 技术调研要求

执行 `/speckit.plan` 时，优先研究以下问题：

1. DeepSeek Harness Python SDK 在 Windows 环境的支持情况。
2. Harness 的 Session、事件和持久化接口能否满足追踪与恢复需求。
3. 如何为不同工作流节点配置可调用工具的范围。
4. 如何在不修改 Harness 内核的前提下收集执行事件。
5. Harness Session 和业务 WorkflowRun 如何建立稳定关联。
6. 长时间运行的浏览器任务如何取消、恢复和避免重复副作用。
7. SQLite 在异步任务与状态更新场景中的事务设计。

对于不能直接确定的技术能力，需要在 `research.md` 中记录验证结果、备选方案及选择依据，不允许假定第三方 SDK 已实现尚未确认的能力。

## 10. 本次规划期望的输出

请结合当前功能规格和项目宪章完成技术规划，生成：

- plan.md：确定技术栈、系统架构及目录结构。
- research.md：记录技术验证、主要决策与替代方案。
- data-model.md：设计数据实体、关联关系及状态转换。
- contracts/：定义 REST API、工作流 Schema、Agent Runtime Adapter 接口。
- quickstart.md：说明本地运行环境及端到端验证场景。

规划时重点处理模块之间的边界，避免重复实现 DeepSeek Harness 提供的能力。

当前阶段不编写业务实现代码，不提前进行 `/speckit.tasks` 的任务拆分。