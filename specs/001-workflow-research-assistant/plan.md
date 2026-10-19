# Implementation Plan: Personal-Agent 工作流驱动的信息研究助理

**Branch**: `001-workflow-research-assistant`（setup-plan 返回的功能标识；当前目录没有 Git 仓库，未创建 Git 分支）  
**Date**: 2026-10-19 | **Spec**: [spec.md](spec.md)  
**Input**: 当前分期规格、项目宪章 1.0.0、用户提供的《Personal-Agent 技术规划说明》。历史技术方案仅供背景参考。

## Summary

采用 Python 模块化单体、Vue 本地 Web UI、SQLite WAL 与本地不可变产物。Workflow Engine 是唯一业务推进者；官方 DeepSeek Harness Python SDK 承担节点内 Agent Loop，适配器处理权限、事件和生命周期，不重写模型循环。

当前一期交付：列表选流、聊天补齐并确认要求、固定流程执行、多方向采集、Markdown 文档、版本管理、暂停/取消/中断恢复、节点重跑分支、上限求助及完整 UI。二期匹配、探索、沉淀、长期偏好仅保留扩展边界，不建设其表/页面，不作为一期发布前置。设计与任务拆分已完成：任务清单见 [tasks.md](tasks.md)（T001–T103，全部未开始）；本计划不含业务实现代码，实现进度与验证证据以 tasks.md 和 docs/release-evidence.md 为准。

## Technical Context

| 项目 | 决策 |
|---|---|
| Language/Version | Python 3.12.x；TypeScript 5.x；Vue 3.x；Windows x64 |
| Primary Dependencies | FastAPI >=0.115,<1；Pydantic >=2.10,<3；SQLAlchemy >=2.0,<2.1；Alembic >=1.14,<2；aiosqlite >=0.20,<1；httpx >=0.28,<1；Playwright >=1.50,<2；jsonschema >=4.23,<5 |
| Agent Runtime | 候选 deepseek-harness-sdk==0.2.1rc2 + deepseek-harness-runtime-bin==0.2.1rc2；专用受限 profile、官方 Loop，MCP Python 工具边界及最小 TS guard 插件 |
| Frontend | Vite >=7,<8，Vue Router 4.x，Node.js 24.x；REST 管理，SSE 进度；生产同源 |
| Dependency Management | uv >=0.8,<1、npm 11.x；接入通过后提交 uv.lock、package-lock.json，精确固定全部依赖、插件与浏览器 revision，禁止浮动 latest/master 交付 |
| Storage | 本机 SQLite WAL、foreign_keys=ON、synchronous=FULL、busy_timeout=5000ms；Repository/UoW；大日志和文档在文件系统 |
| Testing | pytest 8.x、pytest-asyncio >=0.24,<2、Mock Runtime/Tool/Clock；Vitest 3.x、Vue Test Utils 2.x、Playwright UI E2E；ruff、mypy、vue-tsc |
| Target Platform / Type | Windows x64 单用户本地 Web 应用，FastAPI 单 worker + lifespan 管理的内部执行器；不使用请求级 BackgroundTasks 作为持久作业系统 |
| Performance Goals | 本机10,000条执行记录下，列表/详情（不含模型）p95 <500ms；已提交事件到UI p95 <1s；停止请求接收反馈 <1s。是待测目标，不表示动作立即终止 |
| Constraints | 最多一个运行分支、一个模型请求在途；单进程实例锁；未知模型结果阻塞后续调用；不自动续跑；无 Celery/Redis/Kubernetes |
| Scale/Scope | 一用户一工作区、两份种子流程、一期完整 UI；每流程最多100节点、定义1MiB、事件正文64KiB、单产物20MiB，超限明确失败 |

命名约定：产品对外名统一为 **Personal-Agent**，Python 包名 `personal_agent`、本机数据目录 `%LOCALAPPDATA%/Personal-Agent` 与此一致；本地仓库目录名 `spiderAgent` 仅为磁盘路径，不作为产品名或交付名称。

上述范围是依赖候选要求，未宣称组合已验证。2026-10-19 实施前实测与处置：Node 24.21.0 满足要求；uv 0.5.30、系统 Python 3.11.3 不满足，已升级 uv 至 0.13.1 并用 `uv python install 3.12` 安装 CPython 3.12.9，实施统一使用该 3.12 解释器。Harness SDK/runtime 0.2.1rc2 仍未安装、未验证。真实 wheel 的权限、Windows、事件、模型门与日志行为必须通过 [research.md](research.md) 的接入门禁后才采用，不把官方文档核验等同运行测试。

## Constitution Check

PASS 指设计符合条款，不代表未实现的软件通过测试。研究前检查强制设计义务，Phase 1 后以本次契约复核。

| 门禁 | 研究前 | 设计后 | 依据 |
|---|---|---|---|
| 确定性 | PASS | PASS | 版本化 DAG、确认快照、受限条件表达式、输出结构/语义校验；记录配置/运行时及外部观测，SC-003/013 |
| 可恢复性 | PASS | PASS | run/attempt 独立状态、写入intent、文件校验、人工续跑、源成果引用、分支与根预算；SC-012/014—016 |
| 解耦 | PASS | PASS | domain 无框架对象；Runtime/Tool/Repository/Renderer 显式契约；不另写Loop |
| 自动化测试 | PASS | PASS | 离线Mock、状态机/契约/故障注入/UI矩阵；本次为纯文档检查 |
| 数据安全 | PASS | PASS | 最小上下文、代码工具范围、URL/路径校验、脱敏、删除及备份处理，A1—A5 |
| 技术采用及锁定 | PASS：仅候选 | PASS：有采用门禁 | 真实接入失败则禁用真实runtime，不绕过约束上线；锁文件在实施时生成 |
| 资源与取消 | PASS | PASS | ModelGateway、持久槽位、epoch fence、停止处理中、UNKNOWN隔离 |

无宪章例外。如果实际接入无法落实权限或全局模型门，停止真实接入并修订方案，不以 Mock 通过替代发布验证。

## Architecture and Boundaries

| 模块 | 职责与边界 |
|---|---|
| API / UI | 列表、聊天、完整要求摘要、版本编辑、执行图/分支、求助、文档和删除；管理API不暴露给Agent |
| Requirement Service | 字段来源、建议接受和完整列表执行确认分开；修改递增revision使旧确认失效；确定性校验必填/冲突/适用性，H1/H2/H3由结构化数据渲染 |
| Workflow Registry | 不可变版本、结构/工具/表达式校验、乐观并发、最新可执行指针；历史不会覆盖 |
| Workflow Engine | DAG串行调度、条件、输入输出校验、预算、状态/检查点；只有它推进业务流程 |
| Execution Coordinator / ModelGateway | 单执行槽、单模型槽、实例锁、持久请求账本、停止收敛；包括压缩/澄清/重试的所有LLM请求共用网关，不允许SDK旁路 |
| Runtime Adapter | SDK session与attempt关联，最小节点任务上下文、受限工具及输出契约，规范化事件；不会更新业务状态 |
| Tool Adapters | search/fetch/browser.read/extract；参数、目标、权限、额度再次校验；共享httpx client，按run隔离浏览器context；一期无shell或对外写入工具 |
| Artifact Service | 证据汇总、硬约束三态、报告/清单/对比表、Markdown渲染、操作账本与hash、获取失败后重新下载 |
| Persistence | Repository/UoW短事务；状态快照+追加事实，不引入完整event sourcing平台 |
| Exploration Manager（二期） | 复用Runtime/Tool/Artifact，管理指示及生效边界，成功路径转草稿，经测试/校验/用户确认后发布 |

### 核心执行决策

1. 规范 JSON 为持久定义；六种节点、顺序与无环分支，详见契约。UI提供可读定义与JSON编辑，不要求可视化画布。每次修改保存新版本；开始前版本变化需重新确认。
2. 用户确认绑定 draft revision、完整摘要hash、workflow version、政策与预算。建议接受不等于执行确认，所有修改使旧确认失效；一期不直接编辑要求列表。
3. PAUSING/CANCELLING 表达请求处理，PAUSED/INTERRUPTED 可人工恢复，CANCELLED 不可恢复，WAITING_USER/BUDGET_LIMIT 必须明确处置。具体状态见 data-model。
4. 续跑沿用run_id并追加attempt；节点重跑创建独立run_id，parent指来源、root指共同根；仅复用确认适用的前置成果引用。改输入只能新分支，影响不明须用户确认更早起点。
5. 根预算账本跨分支累计；不通过新ID清零。独立新研究有独立预算，但不能自动建立以绕过求助。所有请求先预留最坏资源，结算失败保持unknown。
6. 写入先持久化intent，再写同目录临时文件、flush/fsync、移到不可变目标并验hash，最后事务提交artifact/step/checkpoint/event。允许孤儿文件，禁止DB引用未完整产物；崩溃核对实际目标，不假设文件与DB统一原子。
7. SSE只发送已提交事件，按序号重放；迟到结果作为隔离事实追加，不能覆盖终态或新attempt。确实发生的写入不得隐瞒。UNKNOWN模型请求跨重启保持阻塞，关闭本地进程不是远端终止证据。

### 默认政策（执行前展示，调整必须明确确认）

| 项目 | 默认值 |
|---|---|
| 每个研究历史 | 有效研究20分钟；30次模型请求、60次工具、20次搜索、40次页面读取、累计100,000 input+output tokens；任一触顶求助 |
| 计时 | 暂停/等待/中断不计有效研究时长；已发起操作用量仍累计，未知消耗不按零处理 |
| 单次模型 | 120s超时，最多8192输出tokens；输入token以实际tokenizer或保守上界预留，无法保证上界则拒绝调用 |
| 重试 | 每节点最多3次尝试，初次+2重试，1s/4s退避；权限/契约/未知副作用错误不自动重试；预算追加不自动重置尝试上限 |
| HTTP/浏览器 | connect10s/read30s；浏览器动作/导航30s；停止收敛窗口15s，逾期显示尚未确认停止并保持槽位 |
| 澄清 | 每draft 20次模型或30,000tokens；触顶须用户追加。草稿消耗与后续研究分别记账、同时可见，不隐藏前置成本 |
| 保留 | 页面缓存和日志7天；会话协议数据最多7天且完成后尽快销毁（不保存内部推理）；业务对话、输入、事件、来源摘录、产物90天；去个人化流程版本保留至删除 |
| 删除 | 到期/用户删除后立即不可访问不可复用，清理blob/cache/session；保留无内容墓碑，跨分支引用随之失效并阻止不安全恢复 |
| 备份 | 用户显式本机备份，7天保留；纳入删除清单，恢复后先执行删除/到期政策再开放。用户自行下载的副本由用户管理 |

不写死模型价格，不把token限制冒充确定金额。额度预留不足时提前求助；只有明确批准增量和继续且阻塞已解决才运行。未知远端完成状态可能持续阻塞，这是安全串行的可用性限制。

### 安全、迁移与运行

- 默认127.0.0.1、单worker、执行期间无reload；OS实例锁拒绝第二进程。生产同源，开发限定本机Origin；Host/Origin校验，一次性本机引导换取HttpOnly SameSite会话，写操作CSRF校验，无账号/角色系统。
- 工具只访问授权公开资料，禁止内网/loopback/link-local/元数据地址、file/javascript协议；DNS、重定向及浏览器子资源逐次验证，防路径穿越与DNS rebinding；TLS验证开启，不绕登录或验证码。
- Harness使用受管空工作区和显式profile，禁用shell/fs、自动插件发现、子Agent、自修改。Python业务工具使用受控本地MCP服务，薄TS guard只是权限机制，不能推进流程。
- 凭据从环境或系统凭据库注入，日志字段白名单并脱敏；模型协议推理只允许内存传递。不能关闭原始推理持久化的profile不能通过真实接入门禁。
- 迁移前维护模式停止动作并核验在途，用SQLite backup API + blob清单备份；失败从验证过的备份恢复，不依赖破坏性downgrade。恢复校验hash/外键/删除墓碑，遗留执行保持INTERRUPTED，用户手动继续。

## Project Structure

### Documentation (this feature)

```text
specs/001-workflow-research-assistant/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
└── contracts/
    ├── rest-api.md
    ├── workflow-schema.md
    └── runtime-adapter.md
```

### Source Code (repository root，拟建目录)

```text
backend/
├── pyproject.toml / uv.lock / alembic.ini
├── migrations/
├── src/personal_agent/
│   ├── api/                     # DTO、路由、SSE、安全
│   ├── domain/                  # 状态、确认、版本、预算、影响分析
│   ├── application/             # requirements、registry、engine、coordinator
│   ├── ports/                   # runtime、tool、repository、renderer、clock
│   ├── adapters/
│   │   ├── harness/             # Python SDK、事件、会话
│   │   ├── tools/               # MCP、Brave、httpx、Playwright
│   │   ├── persistence/         # SQLAlchemy、UoW、blob
│   │   └── rendering/           # Markdown
│   └── bootstrap/               # 配置、实例锁、lifespan、自检
├── workflows/                   # 技术选型对比、公开主题资料研究
└── tests/{unit,contract,integration,live,fixtures}/
runtime-policy/                  # 受限profile、最小TS guard/provider挂接、锁文件
frontend/
├── package.json / package-lock.json
├── src/{api,components,pages,router,stores}/
└── tests/{unit,e2e}/
scripts/                         # 启动、备份恢复与验收入口
```

工作数据位于 `%LOCALAPPDATA%/Personal-Agent/{db,artifacts,cache,runtime,logs,backups}`，不进入版本库，使用本机文件ACL保护。

**Structure Decision**: backend/frontend为开发边界，生产一个本地应用；Harness进程及MCP服务是受管适配器，不是业务微服务。两份种子流程适用范围不同，各至少两个查询方向，不限旅游。

## Phase Outputs and Coverage

Phase 0 见 research.md；Phase 1 见 data-model.md、contracts 和 quickstart.md。FR-001/002/004/010/011 → requirements；006/007/008/009 → engine/tools/evidence；012 → artifact；020/021/022 → registry；024/025/032/033 → coordinator/recovery；026/027 → security/lifecycle；028/029/030/031 → UI。二期 FR-003/005/013—019/023 不作一期门禁。

一期验收覆盖 SC-001/003/005—009/012—017；二期标准保留至二期。真实接入、所有模型请求纳管、日志安全烟测未运行，不能报告通过。

## Complexity Tracking

无宪章违规。Repository、操作账本、模型网关、运行时进程分别服务当前存储隔离、中断一致性、全局串行及供应商生命周期需求；没有引入通用工作流平台、消息队列或分布式调度。
