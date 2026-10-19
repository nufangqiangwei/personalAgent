# Phase 0 Research — Personal-Agent

日期：2026-10-19。依据当前 spec.md、宪章 1.0.0 和用户技术规划输入。这里区分官方资料证据、项目设计决策与尚未执行的接入测试；后者不冒充已验证能力。设计问题均有明确选择及失败处置，不遗留需要用户决定的技术选型。

## R1 官方 Python SDK 与 Windows

- **Decision**：候选锁定 `deepseek-harness-sdk==0.2.1rc2`，配套 `deepseek-harness-runtime-bin==0.2.1rc2`，Python 3.12、Windows x64；不采用旧方案的 TypeScript SDK 作为主业务入口。
- **Evidence**：PyPI 固定版本页标注 Python >=3.10；runtime README 列出 Windows x64 wheel，SDK 以 stdio JSON-RPC 启动 bundled runtime，普通运行无需另装 Node。Node 24 用于前端和薄插件构建。本机仅发现 Python311 路径，未安装或运行目标 SDK。[SDK 发行页](https://pypi.org/project/deepseek-harness-sdk/0.2.1rc2/)、[runtime 发行页](https://pypi.org/project/deepseek-harness-runtime-bin/0.2.1rc2/)、[runtime 文档](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk-runtime/README.md)。
- **Rationale**：满足用户 Python 优先且复用供应商 Loop。发行版与 runtime 必须匹配，RC 的兼容风险通过独立契约测试控制。
- **Alternatives considered**：TS SDK sidecar 增加通信层；自写 Agent Loop 违反输入要求；WSL 不作为 Windows MVP 的强制依赖。
- **Verification boundary**：master 文档是可变参考，不是固定 commit；未证明 master 所有接口均存在于 0.2.1rc2 wheel。实际 wheel 烟测失败则禁用真实 runtime，保留 Mock 供开发，不以 Mock 验收替代真实交付。

## R2 Session、事件与业务关联

- **Decision**：每个节点 attempt 建独立 session，业务保存 adapter_session_id、provider_session_id、runtime 指纹、run/step/attempt 对应关系。默认恢复从已校验业务检查点重建最少上下文；只有持久化满足安全约束且版本兼容时才使用官方 resume。
- **Evidence**：官方 SDK 支持 `resume_session`；恢复缺失/不兼容/占用会话会失败，不创建替代。`session/prompt` 返回的 messageId 只是入队标识；事件含 session.event/status，wait 涉及根会话及托管后代。不能恢复外部浏览器进程。[Python SDK](https://github.com/deepseek-ai/deepseek-harness/blob/master/python/sdk/README.md)、[服务端](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sdk/server/README.md)、[协议](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sdk/protocol/README.md)。
- **Rationale**：引擎只接受验证后的结果及工具事实，Harness 恢复不决定 run 成功或下一业务节点。保存规范化事件及游标可关联追踪，不读取供应商私有库。
- **Alternatives considered**：一个永久 session 容易污染节点权限/上下文；历史对话不能充当工作流检查点。
- **Verification boundary**：测试事件顺序、重复事件、恢复失败与持久化内容。原始推理不进入产品存储；若不能关闭或过滤供应商持久日志中的内部推理，则使用无持久化 profile + 自有结构化事件/结果；若仍无法满足则停止接入。

## R3 节点工具权限和扩展

- **Decision**：专用受控 profile，只加载官方 Loop、SDK、必要模型能力及受限工具；Python Tool Service 经官方 MCP client 接入。薄 TS Cordis 插件承担 restrict/guard 和模型门接入，不能包含业务编排或另写 Agent Loop。
- **Evidence**：工具服务有 `ctx.tools.restrict({allow:[...]})`，但仅过滤继承工具；自身 scope 注册有豁免，故必须再由 `ctx.tools.guard` 拒绝越权。未证实 Python SDK 有直接注册 callback 的等价接口。官方 sdk-minimal 自带危险权限 shell 等能力，不能原样上线。[工具服务](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/tools/README.md)、[minimal profile](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/sdk-minimal/README.md)。
- **Rationale**：profile 去除不需要能力、guard 强制检查、Python Tool Service 再查 scope/epoch/额度三层控制，工具名字白名单不等同于网络或文件范围授权。
- **Alternatives considered**：仅 prompt 禁止、仅隐藏工具名、沿用默认全权限 profile 均无法满足权限门禁。
- **Verification boundary**：按发行 wheel 验证 MCP/guard/profile 支持及拒绝路径；禁止自动扫描用户工作区插件、shell、子 Agent、自修改。SDK 不支持需要的挂接时，先修订适配设计，不绕过权限。

## R4 全应用串行、超时与取消

- **Decision**：单应用实例、一个运行槽、一个模型网关；所有实际模型请求（包括 compaction、澄清、文档、重试）经过同一持久请求账本与 semaphore(1)。Harness 模型 transport 通过本机受限 ModelGateway 代理到已配置供应商；代理只转协议与计量，不实现 Loop。禁用无关 compaction/子 Agent/隐式 SDK 重试；必要压缩也必须经过网关。
- **Evidence**：官方协议当前无 session/cancel 或 session/close，关闭 runtime 不证明远端请求已终止。模型在 initialize 确定。默认 profile 可能产生压缩调用，不能只在 Python submit 外加锁就声称每次模型请求受控。[协议](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/sdk/protocol/README.md)、[minimal profile](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/bundle/sdk-minimal/README.md)。
- **Rationale**：持有模型槽直到结束/已证实取消；本地超时、断连接、进程死亡都可能进入 UNKNOWN。UNKNOWN 跨重启保留，禁止第二请求；恢复 UI 说明缺失证据，只有可验证结束记录才解锁，用户点击重试本身不是证明。这是明确的保守可用性限制。
- **Alternatives considered**：超时自动释放锁会违反 FR-033；模型并发后丢弃结果不能修复已发生的并发。关闭 worker 可隔离本地动作，不能充当远端取消确认。
- **Verification boundary**：验证所有请求都路由网关且无旁路；若候选版本不支持受控 provider/transport 配置则真实模式门禁失败。暂停先禁止下一动作，允许当前有界动作收敛；终态由引擎确认，不直接映射 SDK status。

## R5 SQLite 事务、文件检查点与备份

- **Decision**：SQLAlchemy AsyncEngine + aiosqlite，每事务独立 AsyncSession；WAL、FULL、外键与 5s busy_timeout；短事务统一写入入口，网络调用期间无 DB 事务。状态、事件、用量与引用同事务提交，revision CAS 防并发覆盖。
- **Evidence**：AsyncSession 不宜在并发任务间共享；WAL 仍是单 writer，需本机文件系统，FULL 强于 NORMAL 的断电持久性。[SQLAlchemy asyncio](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html)、[SQLite WAL](https://www.sqlite.org/wal.html)。
- **Rationale**：适合单用户单执行；SQLite WAL checkpoint 和业务 checkpoint 分开命名。文件先按 operation_id 写不可变路径并验证，再提交 DB 引用；允许待清理孤儿，禁止未保存文件被标成功。此跨资源协议是项目设计，不宣称 DB/文件具有统一原子事务。[原子提交背景](https://www.sqlite.org/atomiccommit.html)。
- **Alternatives considered**：长事务包住 LLM/抓取会锁库；PostgreSQL/Redis 队列为 MVP 引入无必要部署成本。
- **Verification boundary**：故障注入覆盖写入前、文件完成后、DB commit 前后；备份使用 SQLite backup API + blob 清单，维护模式中产生一致副本，迁移失败恢复已验证副本，禁止直接复制活跃 DB 主文件遗漏 WAL。

## R6 浏览器、HTTP、搜索与外部内容

- **Decision**：httpx 默认读取，Playwright Chromium 处理需要渲染的公开页面；同 run context 复用，无跨任务 cookies 自动复用。Brave Search Web API 为首个可替换搜索实现，需用户配置凭据；默认测试固定 fixture，不自动退回浏览器抓搜索引擎。
- **Evidence**：Playwright Python 要求 Windows Proactor；取消正在 Playwright 调用中的 asyncio Task 不受支持。采用原生超时、协作取消与受管关闭，必要时 shield 保留动作直至收敛。[Playwright library](https://playwright.dev/python/docs/library)、[关闭语义](https://playwright.dev/python/docs/api/class-browser)、[asyncio shield](https://docs.python.org/3.12/library/asyncio-task.html#shielding-from-cancellation)。Brave 提供 Web Search API，需要 subscription token。[官方接口](https://api-dashboard.search.brave.com/api-reference/web/search/get)。
- **Rationale**：采集只读，不授予对外修改能力；取消浏览器不是恢复网页状态，重新获取页面须新记录时间/hash，旧证据不冒充新查询。URL、DNS、重定向和子资源范围校验由工具代码执行。
- **Alternatives considered**：直接复用用户默认浏览器泄漏会话范围；用 Task.cancel 代表停止没有可靠保证；把网页指令当工具策略违反宪章。
- **Verification boundary**：真实网络可达性与中文来源覆盖尚未测试；失败按 SOURCE_UNAVAILABLE 报告，不宣称有结果。检测 timeout、断网、私网跳转、恶意网页、关闭未完成及文件副作用未知。

## R7 版本、输入确认、分支及预算

- **Decision**：规范 JSON 定义、不可变版本、DAG 顺序/条件执行；运行输入按字段记录 user/default/suggestion 来源，接受建议与确认执行不同；分支采用 parent/root ID 及不可变成果引用。输入依赖字段声明支持影响分析，未知影响保守要求用户前移到可验证起点。
- **Rationale**：直接落实 spec FR-001/004/020/025/032；不需要 Git 来存执行历史。根预算账本跨分支累计，追加需用户明确处置，防新 ID 重置额度。
- **Alternatives considered**：覆盖旧 run 或复制成果为新成功记录会丢历史；工作流使用 prompt 控制、无限循环及任意 Python 条件执行均不可接受。

## R8 UI、文档、生命周期与版本固定

- **Decision**：Vue 3 + TS、Vite、REST+SSE；Markdown 为唯一一期交付格式，内容支持报告/清单/对比表。默认保留缓存7天、业务内容90天，删除即时阻断普通访问和复用；备份7天并纳入删除清单。资源数值见 plan。
- **Rationale**：两端明确 DTO/事件，摘要确定性渲染 H1/H2/H3，UI 覆盖真实执行和所有失败状态；不把二期探索放进一期依赖。JSON Schema 2020-12 配合 Pydantic 运行时校验。[Pydantic schema](https://pydantic.dev/docs/validation/latest/concepts/json_schema/)、[Vite 环境要求](https://vite.dev/guide/)、[FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/)。
- **Alternatives considered**：WebSocket 双向流增加恢复复杂性；专有文档格式和长期偏好本期不需要。数据永不过期不满足本期生命周期约束。
- **Verification boundary**：依赖兼容范围见 plan，真正采用前用 Python3.12/Node24 解算并提交锁文件；本次未创建锁文件或声称安装成功。SDK 接入、全部内部模型请求纳管、日志不落推理是明确的真实模式前置检查。

## 研究结论

Python/Windows 接入路径有官方依据，采用候选版本及受控 profile；业务恢复、权限、串行与安全持久化由外围边界负责，不重写 Harness Loop。没有未决产品澄清项。集成测试未执行的事实保留为实施门禁：Windows启动、wheel接口一致性、工具拒绝、网关无旁路、事件过滤及安全持久化任一失败都阻止真实模式发布。
