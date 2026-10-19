# Quickstart / 一期验证指南

**当前状态**：仓库目前仅有规格、设计与任务文档（[tasks.md](tasks.md) 已生成，T001–T103 均未开始），代码尚未创建；依赖锁文件将在 T006 生成。以下安装、启动和测试命令是实施阶段必须提供的目标入口，现阶段不能运行出产品，也未报告任何业务测试通过。数据结构见 data-model.md，接口见 contracts/。

## 环境与初始配置

Windows x64、Python3.12、Node24/npm11、uv（版本要求见plan）。SQLite数据库放本机磁盘，不用网络共享。2026-10-19 实测：uv 已升级至 0.13.1，CPython 3.12.9 已通过 `uv python install 3.12` 安装；系统 PATH 上的 python 仍为 3.11.3，实施时须用 `uv run --python 3.12` 或 `uv python pin 3.12`，不得直接使用系统 python。生产一个应用实例、一个worker，不开reload。浏览器使用与Playwright锁定版本匹配的Chromium。

真实模式需要用户自行配置模型与Brave Search凭据和获授权范围；密钥经环境/系统凭据存储，不写进源文件或测试夹具。固定资料Mock模式不需要网络或付费模型。依赖采用前先在隔离验证目录验证SDK0.2.1rc2和匹配runtime的Windows启动、工具拒绝、事件、模型门、无内部推理落盘；任何失败阻止真实模式。

实施后首次开发环境准备（在仓库根PowerShell，锁文件应已提交）：

```powershell
uv sync --project backend --python 3.12 --frozen --all-extras
uv run --project backend python -m playwright install chromium
npm ci --prefix frontend
npm ci --prefix runtime-policy
npm run build --prefix runtime-policy
uv run --project backend alembic -c backend/alembic.ini upgrade head
uv run --project backend python -m personal_agent.bootstrap.seed
```

种子加载幂等，提供“技术选型对比”和“公开主题资料研究”两个固定流程；空库验收通过单独临时工作区，不删除日常数据。首次设置界面显示数据目录、保留政策、外部传输范围、模型/搜索配置健康状态及上限；用户无需注册。初始配置完成的判据（同 spec.md 假设 14）：迁移已至 head、数据目录可写、`/health` 的 `execution_available` 为真、保留与上限政策已展示并保存。

目标启动入口：

```powershell
uv run --project backend python -m personal_agent.bootstrap.run --mode mock
npm run dev --prefix frontend -- --host 127.0.0.1
```

后端默认127.0.0.1:8000，前端开发127.0.0.1:5173且代理/api到后端；生产构建 `npm run build --prefix frontend` 后由后端同源提供静态资源，`bootstrap.run --mode live` 启用已验证runtime。入口必须设置并自检Windows Proactor、单实例锁、迁移版本、受限profile与模型网关。运行中禁止reload；第二启动应明确拒绝。

## 自动化验证入口（实施时建立）

```powershell
uv run --project backend ruff check backend
uv run --project backend mypy backend/src
uv run --project backend pytest backend/tests/unit backend/tests/contract backend/tests/integration
npm run typecheck --prefix frontend
npm run test --prefix frontend -- --run
npm run test:e2e --prefix frontend
npm run test --prefix runtime-policy -- --run
```

默认测试使用固定clock/random/HTTP/model fixtures和临时数据目录，禁止访问真实供应商。真实接入测试另运行 `uv run --project backend pytest backend/tests/live -m live`，只在用户明确启用live、已配置凭据且确认费用范围时执行；其结果不能从Mock推断。所有模型调用仍受同一单请求门。

## UI端到端场景与预期

| 场景 | 操作与观察 | 验收 |
|---|---|---|
| 主路径三例 | 选流后聊天：信息完整、必要项缺失后补齐、先填后纠正；显示H1/H2/H3及列表，确认后执行并保存/重开Markdown | SC-001/009，FR-001/002/004/028/031 |
| 建议与取舍 | 用户“不知道”“帮我决定”；先显示依据和待接受建议；接受后仍不研究。喜好冲突用具体假设取舍，无特别偏好有效；修改后旧确认失效 | SC-006/013 |
| 范围与格式 | 执行前约定report/checklist/comparison三种结构与两组喜好；不支持的格式先协商；硬约束不合格/未知不混作合格 | SC-005/006 |
| 固定控制 | 至少8例覆盖正常、条件、失败、运行中改版本、模型诱导跳步、网页注入；只能走声明路径，实际版本始终可查 | SC-003 |
| 证据和交付 | 两方向均有结果/缺口；冲突/过期/未证实标记；模拟无合格候选、部分完成和下载失败，保留成果、可再取、不假报完整 | SC-005/008 |
| 版本编辑 | 两个流程各保存修改3次，旧版本可查，新运行默认最新；模拟过时编辑409，保留待保存内容；旧run不变 | SC-007 |
| 异常UI | 空库、不适用、改选、版本变化、来源失败、超时、取消、存储失败、资料删除均显示原因和可用后续操作 | SC-008/009 |
| 稳定性 | 两流程×三组有效输入×各执行两次，共12次；可用固定来源、上限内均生成可获取文档；真实模式另抽查两流程各一次，不能以Mock代发布 | SC-012 |
| 断点 | 在完成节点后、节点中途强制终止测试实例；重开显示中断不自动执行；点击继续核验，原ID不变、成功节点不再执行、未完成attempt追加 | SC-012/014 |
| 文件副作用 | 故障注入：intent前、intent后、文件发布后DB提交前、DB提交后；另构造未生效/部分/未知/目标后来改动；确认已生效只复用，不明阻塞重跑 | SC-012，故事9.5 |
| 分支 | 续跑两次同ID；较早节点/首节点重跑、分支再分支为新ID，父根正确；缺少前置成果、跨分支既有写入不允许跳过核验 | SC-014 |
| 分支输入 | 仅影响后续、影响更早、用户拒绝前移、影响不明四例；完整新列表与实际起点确认前不启动，原分支不改、无效成果不复用 | SC-015 |
| 暂停取消 | 动作中pause显示处理中，收敛后PAUSED，同ID继续；cancel终结，resume拒绝，可另确认分支；迟到输出不改终态 | SC-016 |
| 上限 | 缩小时间/请求/token上限触发完整求助；不回应/“知道了”不继续；明确追加和继续后同ID、累计消耗不清零；新分支共享根账本 | SC-016 |
| 串行 | 双击启动、异任务启动、分支切换、执行中聊天、模型超时重试、停止时模型在途；计数器证明run≤1、实际LLM≤1；未知请求不能释放槽 | SC-017 |
| 数据安全 | 注入索密/改流程文本、恶意URL/重定向/私网/路径穿越、工具scope越权，均拒绝；错误/SSE/文档无密钥/内部推理 | A1—A3，FR-026 |
| 保留删除 | 查看期限、主动删除及模拟到期；文档/缓存/session及备份清理，普通读取410、新任务不能复用；引用显示墓碑；旧备份恢复不复活已删内容 | A4，FR-027 |

故障注入仅在临时工作区测试实例，不终止用户其他进程、不破坏真实文档。验证保存每次控制决策的输入hash、版本、事件序号、attempt和结果hash，以便重放相同观测检查控制结果一致。

## API/SSE复验

1. 同Idempotency-Key重复POST /runs，得到同run_id；同键不同body报409。两标签页旧If-Match确认/保存报412或版本冲突409，不覆盖内容。
2. 读快照取得last_event_sequence，订阅events，断线后带Last-Event-ID重连；确认不丢已提交事件、不重复执行动作；过期游标触发reset并读取新快照。
3. 模拟SDK仅返回accepted后断线，run不能SUCCEEDED。模拟旧epoch result.available，追加隔离事件，不能推进新attempt。
4. 让运行时内部压缩、澄清和Output生成请求同时尝试进入网关，观测实际下游在途数≤1。断开远端连接但不能证明完成，记录UNKNOWN，重启后仍阻止下一请求。

## 迁移、备份与恢复

目标运维入口 `python -m personal_agent.bootstrap.backup` / `restore --manifest <path>` 均由UI维护操作可调用，日常用户不必用命令。维护模式先阻止新增动作并收敛已在途，SQLite backup API生成DB副本并保存blob/hash清单、schema版本与删除清单，验证恢复到临时目录后才能声明备份可用。

验收一次成功升级、一次迁移中失败后恢复、一次版本不兼容恢复拒绝；恢复后检查外键/hash/引用/删除政策，旧RUNNING记录标INTERRUPTED，确认不会自动续跑。SDK/profile升级同时回归权限、单模型门、事件、取消与检查点，不允许换版本后直接信任旧session。

## 发布证据

保留静态检查/默认测试/真实接入测试结果及锁文件指纹；记录未运行项目和限制。一期发布须全部一期标准通过且真实UI连接实际runtime与文档，两期边界不混用。当前仅完成设计文档检查，尚无上述运行验证证据。
