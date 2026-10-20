# Quickstart / 一期验证指南

**当前状态（2026-10-20，T101 记录）**：一期实现代码已位于 `code/`（后端、前端、策略插件、脚本）。本文件中的安装、启动、构建与验证命令已改为**实际存在的脚本入口**，并记录本机验证结果。`code/scripts/start.ps1`、`code/scripts/package.ps1` 为 T101 交付；`code/scripts/check.ps1`、`e2e.ps1`、`e2e-backend.ps1` 保持可用。

仍存在以下限制，不能据此宣称一期发布完成：

- `--mode live` 在 T021 运行时接入门禁通过前会被拒绝；`start.ps1` 在预检阶段直接报错并给出 `docs/runtime-adoption.md` / `docs/live-acceptance.md` 指引。
- 与锁定的 Playwright 1.64.0 匹配的 Chromium revision **1248 尚未安装**。Mock 研究不需要浏览器；`browser.read` 工具与基于浏览器的 E2E 需要先执行 `start.ps1 -InstallBrowsers`（联网下载）或 `uv run --project code/backend python -m playwright install chromium`。
- 本机仅安装了 Windows PowerShell 5.1（`powershell`），`pwsh` 7 不在 PATH；脚本按两者兼容编写，**实测使用 `powershell -NoProfile -ExecutionPolicy Bypass -File`**。
- T086–T103 中除已勾选任务外仍有验收项未完成（如 E2E 收口、安全/稳定性/性能、真实接入与发布证据）。T101 只负责启动与打包入口，T102/T103 才能给出发布结论。
- 备份/恢复入口由 T090–T096 覆盖，其中 T096 的备份/恢复演练仍未完成；T101 未运行该链路，本文件仅保留一期目标描述，不表示已验证。

## 环境与初始配置

Windows x64、CPython 3.12.9（经 uv）、Node.js 24.x / npm 11.x、uv `>=0.8,<1`。2026-10-20 T101 复验：uv 0.13.1、CPython 3.12.9、Node.js 24.21.0、npm 11.19.0。系统 PATH 上的 `python` 仍可能是 3.11.3，**禁止**直接使用；一切经 `uv run --project code/backend --python 3.12` 执行。SQLite 数据库放本机磁盘，不用网络共享；生产一个应用实例、一个 worker，不开 reload。浏览器使用与 Playwright 锁定版本匹配的 Chromium（revision 1248）。

真实模式需要用户自行配置模型与 Brave Search 凭据和获授权范围；密钥经环境/系统凭据存储，不写进源文件或测试夹具。固定资料 Mock 模式不需要网络或付费模型。依赖采用前先在隔离验证目录验证 SDK 0.2.1rc2 和匹配 runtime 的 Windows 启动、工具拒绝、事件、模型门、无内部推理落盘；任何失败阻止真实模式。

开发环境准备（仓库根 PowerShell，锁文件已提交）：

```powershell
uv sync --project code/backend --python 3.12 --frozen --all-extras
uv run --project code/backend python -m playwright install chromium
npm ci --prefix code/frontend
npm ci --prefix code/runtime-policy
npm run build --prefix code/runtime-policy
uv run --project code/backend alembic -c code/backend/alembic.ini upgrade head
uv run --project code/backend python -m personal_agent.bootstrap.seed
```

种子加载幂等，提供“技术选型对比”和“公开主题资料研究”两个固定流程；空库验收通过单独临时工作区，不删除日常数据。首次设置界面显示数据目录、保留政策、外部传输范围、模型/搜索配置健康状态及上限；用户无需注册。初始配置完成的判据（同 spec.md 假设 14）：迁移已至 head、数据目录可写、`/health` 的 `execution_available` 为真、保留与上限政策已展示并保存。

开发态启动入口（两个进程，前端 Vite 代理 `/api` 与 `/health` 到后端）：

```powershell
uv run --project code/backend python -m personal_agent.bootstrap.run --mode mock
npm run dev --prefix code/frontend -- --host 127.0.0.1
```

后端默认 127.0.0.1:8000，前端开发 127.0.0.1:5173。生产形态使用下一节的 `start.ps1`，由后端同源提供 `code/frontend/dist` 静态资源。

## 实际入口（T101）

所有脚本从仓库根运行，兼容 Windows PowerShell 5.1（`powershell -File`）与 PowerShell 7（`pwsh -File`，本机未安装，未实测）。脚本只读取环境变量/凭据存储，不内嵌任何密钥。

### 生产风格单实例启动：`code/scripts/start.ps1`

```powershell
# 默认：mock 模式、127.0.0.1:8000、%LOCALAPPDATA%\Personal-Agent 数据目录
powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/start.ps1

# 隔离实例：不触碰日常数据，先只做自检再启动
powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/start.ps1 -CheckOnly -Port 8123 -DataDir .cache\start-data
powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/start.ps1 -Port 8123 -DataDir .cache\start-data

# 安装锁定 revision 的 Chromium（联网），然后启动
powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/start.ps1 -InstallBrowsers
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `-Port` | `PERSONAL_AGENT_PORT` 或 8000 | 校验 1–65535，并在启动前探测端口占用 |
| `-Host` | `PERSONAL_AGENT_HOST` 或 127.0.0.1 | PowerShell 自动变量 `$Host` 只读，脚本以内置别名提供 `-Host`（如 `-Host 0.0.0.0`） |
| `-Mode` | `PERSONAL_AGENT_MODE` 或 mock | 仅 `mock`/`live`；live 在接入门禁通过前被拒绝 |
| `-DataDir` | `PERSONAL_AGENT_DATA_DIR` 或 `%LOCALAPPDATA%\Personal-Agent` | 相对路径按仓库根解析；目录必须可写 |
| `-CheckOnly` | 关 | 完成全部预检与离线自检后退出，不启动服务 |
| `-InstallBrowsers` | 关 | 仅当锁定 Chromium 缺失时联网安装；默认只检查、不下载 |
| `-NoSync` | 关 | 跳过 `uv sync`，使用现有后端环境（应急/离线） |

启动前按顺序执行并 fail-fast：

1. `uv` 存在且版本在 `>=0.8,<1`；`uv python find 3.12` 得到 **CPython 3.12.9**。
2. `uv lock --check`，然后 `uv sync --frozen --inexact --project code/backend --python 3.12`。`--frozen` 不重写 `uv.lock`；`--inexact` 保留已安装的 dev 工具链，避免破坏 `check.ps1`。同步后再次确认后端环境为 CPython 3.12.9。
3. `code/backend/uv.lock`、`code/frontend/package-lock.json`、`code/runtime-policy/package-lock.json` 存在；Node 24.x / npm 11.x（不匹配只告警）；`code/frontend/node_modules` 与 `code/runtime-policy/node_modules` 已安装（缺失时给出 `npm ci` 命令）。
4. 后端 import 冒烟：导入 `personal_agent.api.app`，并打印 CPython/Playwright 版本。
5. Playwright 浏览器检查：用 `playwright install --dry-run chromium` 读取**锁定的 Chromium revision（1248）**及安装路径，不下载；与 `docs/dependency-baseline.md` 记录的 revision 交叉核对。缺失时仅告警（mock），live 模式则失败；只有 `-InstallBrowsers` 才下载。
6. 运行时配置：模式/主机/端口/数据目录校验；数据目录写探测；live 接入门禁检查；单实例 OS 锁预检（锁被持有则退出码 2）；TCP 端口占用探测。
7. 环境变量 `PERSONAL_AGENT_MODE/HOST/PORT/DATA_DIR/DB_URL` 按 `bootstrap/run.py` 与 `config.py` 的读取方式设置，随后执行 `alembic upgrade head` 与幂等 `bootstrap.seed`。
8. 离线启动诊断：`python -m personal_agent.bootstrap.run --mode <mode> --data-dir <dir> --check`，要求 `migrations_up_to_date=true` 且 `execution_available=true`；失败即退出。
9. 以 `python -m personal_agent.bootstrap.run` 前台启动：单实例、单 worker、无 reload；存在 `code/frontend/dist` 时同源提供 UI，否则只提供 API。

退出码：`0` 正常停止；`1` 预检/自检失败；`2` 同一数据目录已有实例持有 OS 单实例锁（`<data>\runtime\instance.lock`），第二个进程不会启动 worker。停止用 Ctrl+C。

健康检查与单实例复验：

```powershell
Invoke-RestMethod http://127.0.0.1:8123/health | ConvertTo-Json -Depth 5

# 同一 -DataDir 的第二个启动应被拒绝（退出码 2）
powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/start.ps1 -CheckOnly -Port 8124 -DataDir .cache\start-data
```

### 可复现构建产物：`code/scripts/package.ps1`

```powershell
# 默认输出 dist/personal-agent-<version>/
powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/package.ps1

# 自定义输出目录；跳过单元测试（仍执行类型检查与构建）
powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/package.ps1 -Output .cache\package -SkipTests
```

执行内容：`uv lock --check` + `uv sync --frozen --extra dev`；后端 mypy、前端 `vue-tsc`、策略插件 `tsc` 类型检查；后端 `tests/unit`、前端 Vitest、策略插件 Vitest（`-SkipTests` 时跳过并在 manifest 标记 `unit_tests=skipped`）；前端 `npm run build`；策略插件 `npm run build`；后端 `uv build --project code/backend`（wheel + sdist）。构建前固定 `SOURCE_DATE_EPOCH=946684800`，使同一 checkout 的后端 wheel/sdist 字节可复现。

输出目录结构（默认 `dist/personal-agent-0.1.0/`）：

```text
personal-agent-0.1.0/
├── backend/
│   ├── personal_agent-0.1.0-py3-none-any.whl
│   └── personal_agent-0.1.0.tar.gz
├── frontend/              # code/frontend/dist 的内容（index.html、assets/）
├── runtime-policy/dist/   # tsc 产物（index.js 等）
├── manifest.json
└── SHA256SUMS.txt
```

`manifest.json` 不含时间戳，记录：产品版本（后端/前端/策略插件必须一致）、`schema`、`source.git_commit/git_dirty`、工具链版本（uv/CPython/Node/npm/Playwright）、三个锁文件 SHA-256 与字节数、固定 Chromium revision 及是否安装、类型检查/单元测试状态、全部产物文件的相对路径 + SHA-256 + 字节数。`SHA256SUMS.txt` 额外包含 `manifest.json` 自身的 SHA-256。脚本拒绝写入 `specs/`，重复运行会替换同名版本目录，失败时删除不完整目录。

## 实际入口与验证状态

以下为 2026-10-20 本机实测结果。数据目录均为 `.cache/` 下的临时目录，未触碰日常数据。

| 入口/检查 | 命令（仓库根） | 状态 | 证据与限制 |
|---|---|---|---|
| start 自检（当前工作区） | `powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/start.ps1 -CheckOnly -Port 8138 -DataDir .cache\t101-final-check` | 通过 | CPython 3.12.9、`uv.lock` 一致、node_modules 就绪、后端 import 通过、迁移至 head、种子 2 条、离线诊断 `execution_available=true`；Chromium 1248 缺失仅告警 |
| start 自检（干净快照） | `... -CheckOnly -Port 8134 -DataDir .cache\t101-clean-start` | 通过 | 在 `git archive HEAD` 的干净快照中新建 `.venv`（CPython 3.12.9）、迁移至该快照 head、种子 2 条、离线诊断通过 |
| start 实际服务 | `start.ps1 -Port 8132 -DataDir .cache\t101-serve-data` | 通过 | `/health`：`status=ok`、`mode=mock`、database `ok`、migrations `0014/0014`、runtime `compatible=true`、`execution_available=true`；`/` 返回 200 `text/html`（同源 UI） |
| 单实例拒绝 | 同一 `-DataDir` 再次启动 | 通过 | 第二个进程在预检阶段被 OS 锁拒绝，退出码 2，输出明确错误，未启动第二 worker；停止后健康端口关闭，进程树无残留 |
| package 构建（当前工作区） | `powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/package.ps1` | 通过 | 类型检查通过（mypy 102 文件、vue-tsc、tsc）；后端单测 76、前端 43、策略 16 全通过；wheel+sdist、前端 dist、策略 dist、manifest 与 SHA256SUMS 生成（33 个产物文件、660869 字节）；三个锁文件哈希与基线一致 |
| package 构建（干净快照） | 在 `git archive HEAD` 快照执行同一脚本 | 通过 | 同一流程通过（当时前端单测 38 个、30 个产物文件），证明脚本不依赖本机残留构建产物 |
| package 可复现性 | 干净快照连续两次构建到不同 `-Output` | 通过 | 两次共 32 个文件（含 manifest/SHA256SUMS）逐文件 SHA-256 比较，差异 0；manifest 字节一致 |
| 锁文件指纹 | `package.ps1` manifest | 已记录 | 当前工作区三个锁文件哈希与 `docs/dependency-baseline.md` 一致并写入 manifest；`git archive` 的 LF checkout 哈希不同，脚本会提示基线不一致，跨 checkout 比对需先统一行尾 |
| 真实模式 | `start.ps1 -Mode live` | 未启用 | 无 T021/T100 证据，预检拒绝；`run.py` 同样拒绝 |
| 浏览器工具/E2E | `-InstallBrowsers`、`e2e.ps1` | 未运行 | Chromium 1248 未安装；E2E 需要它或 `PERSONAL_AGENT_CHROMIUM_PATH` |
| 全量检查 | `code/scripts/check.ps1` | T102 负责 | T101 未改该脚本；完整检查与发布证据见 T102 |

## 自动化验证入口

```powershell
# 汇总入口：ruff、mypy、离线 pytest、前端/策略插件类型检查与单测
powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/check.ps1

# 构建同源前端后运行 Playwright E2E（需要 Chromium 1248）
powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/e2e.ps1
```

等价的直接命令：

```powershell
uv run --project code/backend ruff check code/backend
uv run --project code/backend mypy code/backend/src
uv run --project code/backend pytest code/backend/tests/unit code/backend/tests/contract code/backend/tests/integration
npm run typecheck --prefix code/frontend
npm run test --prefix code/frontend -- --run
npm run test:e2e --prefix code/frontend
npm run test --prefix code/runtime-policy -- --run
```

默认测试使用固定 clock/random/HTTP/model fixtures 和临时数据目录，禁止访问真实供应商。真实接入测试另运行 `uv run --project code/backend pytest code/backend/tests/live -m live`，只在用户明确启用 live、已配置凭据且确认费用范围时执行；其结果不能从 Mock 推断。所有模型调用仍受同一单请求门。

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

目标运维入口 `python -m personal_agent.bootstrap.backup` / `restore --manifest <path>` 均由UI维护操作可调用，日常用户不必用命令（对应 T090–T096；T096 的恢复演练尚未完成，本节不表示已验证）。维护模式先阻止新增动作并收敛已在途，SQLite backup API生成DB副本并保存blob/hash清单、schema版本与删除清单，验证恢复到临时目录后才能声明备份可用。

验收一次成功升级、一次迁移中失败后恢复、一次版本不兼容恢复拒绝；恢复后检查外键/hash/引用/删除政策，旧RUNNING记录标INTERRUPTED，确认不会自动续跑。SDK/profile升级同时回归权限、单模型门、事件、取消与检查点，不允许换版本后直接信任旧session。

## 发布证据

保留静态检查/默认测试/真实接入测试结果及锁文件指纹；记录未运行项目和限制。一期发布须全部一期标准通过且真实UI连接实际runtime与文档，两期边界不混用。2026-10-20 T101 已提供启动/打包入口及其自检、健康检查、单实例与可复现构建证据；完整发布结论仍由 T102（全量检查与 `docs/release-evidence.md`）和 T103（计划/证据收口）给出，真实模式仍受 T021/T100 门禁阻塞。
