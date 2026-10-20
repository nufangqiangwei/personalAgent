# 新窗口交接：一期实施与 Harness CLI（2026-10-20）

本文件是本轮交接入口，优先用于恢复上下文；最终要求仍以用户指令、AGENTS.md、宪章及最新规格/契约为准。早期交接及验证记录中的 SDK 主入口、缺网页 token、真实费用为零等结论已经被本轮后续结果更新。

## 1. 当前状态

- 工作目录：`C:\Code\spiderAgent`；当前分支 `main`。
- **工作区有大量已修改和未跟踪文件，均未提交、未合并、未发布。** 包含前几轮与本轮工作；先检查 git status/diff，不得 reset、clean 或整体覆盖。
- 只做一期。全部代码在 `code/` 下；`docs/`、`specs/` 只放文档、契约、验证证据。
- 当前 138 项任务中 **94 已勾选、44 未勾选**。T001 已完成，T086/T087 已勾选，Phase 8 的 Mock 任务验收已闭合。
- **独立 headless CLI 真实模型连通已通过；项目完整 live 执行、技术采用与发布仍未通过。** 不能把这两个结论混为一谈。

| 未闭合 Phase | 剩余 | 任务 |
|---|---:|---|
| Phase 2：公共基础 | 1 | T021，真实运行时采用门禁 |
| Phase 9：完整 UI | 2 | T088、T096 |
| Phase 10：发布收口 | 6 | T097、T098、T099、T100、T102、T103 |
| Phase 11：跨阶段问题收敛 | 35 | T104–T138 |

Phase 1、3–8 原任务已勾选不代表 Phase 11 中针对这些功能的整改已完成。清单勾选不代替需求评审或发布验收。

## 2. 已确认的用户决定与授权

1. 继续执行本仓库的 `speckit-implement` 工作流，遵循任务、前置门禁及测试先行。
2. 自定义需求清单 `workflow.md` 还有 29 项未裁定，用户此前已明确允许继续；**不要重复请求同一许可，不修改评审者勾选**。`requirements.md` 为 16/16，`workflow.md` 为 9/38。
3. 真实测试的**模型、付费搜索与重试合计上限为人民币 25 元**。此额度跨用例、跨聊天窗口累计，**不是新窗口重新获得 25 元**。
4. 用户原来使用 `npx @deepseek-ai/dsh web --port 8730`。用户说明模型 API Key 是在 Web 设置中填写的，不是环境变量，也不是平台账号登录。
5. 用户要求先真实测试 CLI，成功后将使用方式写入技术文档并固定。已完成测试与文档追加：**Web 独立使用，项目通过官方 headless CLI 子进程调用**，不再以 Python SDK API 或 8730 Web RPC 为主入口。
6. 没有要求提交、合并、发布或停止现有 Web。不要擅自重启/终止它。

本轮对授权的依据来自用户原话“测试调用金额上限为25元人民币”及“先测试……可以的话……将……使用方式写死成这样”。后续需要执行真实调用时，先核对本文件费用记录和最新审计；不要再向用户索取网页 token 或重复确认已授权金额。

## 3. 已冻结的 Harness 接入方式

**先读 [harness-cli-usage.md](harness-cli-usage.md)。** 它是当前运行时选择的详细约定，已同步以下文档：

- [技术方案第 15 节](技术方案-DeepSeek-Harness.md#15-2026-10-20-追加决策固定使用-headless-cli)。该文件前面是历史 TypeScript/React 方案，不能因此把现有 Python/Vue 改回去。
- [plan.md](../specs/001-workflow-research-assistant/plan.md)：Summary、运行时载体、当前验证状态。
- [research.md 的 R0](../specs/001-workflow-research-assistant/research.md)：R1 等 SDK 调查保留为历史候选，不再决定主入口。
- [runtime-adapter.md](../specs/001-workflow-research-assistant/contracts/runtime-adapter.md)：业务 RuntimePort 保持，具体实现改映射 CLI。
- [tasks.md](../specs/001-workflow-research-assistant/tasks.md)：T021/T121/T129 描述已与 CLI 方向同步，仍未勾选。

固定 npm 包为 `@deepseek-ai/dsh@0.2.1-rc.2`。项目入口为 `headless --patch <受限配置> --json -`，用户任务通过 stdin 传入。Python 适配器管理 Node + 已安装、已校验版本的 CLI 入口，禁止 shell 拼接，不在每个任务启动时联网安装或自动选择 latest。

Web 可以继续独立运行：

```powershell
npx @deepseek-ai/dsh@0.2.1-rc.2 web --port 8730
```

Web 和 CLI 是不同进程/会话；项目不接管 Web 的活动 Session。项目应使用独立的 `DSH_HOME` 与工作目录，不依赖 Web 端口。交接时 127.0.0.1:8730 仍由原 PID 26948 监听；新窗口应重新核对，不能永久假定 PID 不变。

### 不要再走错的鉴权判断

- 8730 根地址无 Cookie 返回 401 是预期的 Web 鉴权行为，**不阻塞 headless CLI**。
- Web 启动 URL 的 token 不是模型 API Key。无需用户另外为 SDK/CLI 创建一个 Web token。
- 默认 headless 会关闭托管凭据服务，不能自动读到 Web 保存的 Key；本轮裸命令确实失败为 `MISSING_CREDENTIAL`。
- 诊断覆盖配置仅启用 Harness 自有凭据服务引用已有存储位置，未读取/导出/复制 Key，没有改用户 Web profile。
- 正式项目仍须把供应商秘密和所有模型请求保留在统一网关边界，遵守 AGENTS.md 的环境/系统凭据存储要求。**诊断直连成功不能替代正式网关接线。**

## 4. 本机验证入口与证据

探针：[test_headless_cli_smoke.py](../code/backend/tests/live/test_headless_cli_smoke.py)。

覆盖配置：[headless-smoke.patch.yml](../code/backend/tests/live/fixtures/headless-smoke.patch.yml)。

机器可读证据：[2026-10-20-headless-cli-smoke.json](audits/2026-10-20-headless-cli-smoke.json)。

已验证的本机非秘密路径如下；它们是当前机器安装位置，不得作为发布业务常量：

```text
Node:
C:\Program Files\nodejs\node.exe

CLI JS:
C:\Users\qiangwei\AppData\Local\npm-cache\_npx\1e7f6d9597241db0\node_modules\@deepseek-ai\dsh\lib\bin.js

Harness 已有凭据存储位置（不要打开/打印其内容）：
C:\Users\qiangwei\.dsh\.credentials.yaml
```

`PERSONAL_AGENT_DSH_CLI_PATH` 和 `PERSONAL_AGENT_DSH_CREDENTIAL_STORE` 目前是**探针读取的环境变量**，不表示主应用已经支持这些配置。探针先检查 package.json 的名称/版本，再通过 Harness 自身的凭据服务解析已有 Key；测试代码从未读取密钥文件内容。

npx 包装器在本执行器中先遇 `ENOTCACHED` / `EPERM`，因此成功测试直接启动同版本已安装 CLI 入口。成功 CLI 仍有非致命 EPERM 诊断，未宣称 npm 缓存权限或所有日志警告均已修复。

两次真实模型请求都返回固定 `CLI_SMOKE_OK`、退出码 0；第二次还验证：

- 一个 `completed` 模型步骤；0 工具调用、0 thinking 事件、0 持久会话日志。
- 313 个非缓存输入 + 1280 个缓存读取输入 + 7 个输出 tokens，总计 1600。
- 约 3.50 秒；首次约 3.78 秒。
- profile 禁用工具、子 Agent、thinking、重试、自动压缩与模型标题等额外模型入口，输出上限 32 tokens。
- 原始 stdout/stderr 仅内存解析，不持久化；只保存白名单证据。

注意 Harness 的 `inputTokens` 在本次用量中不包含单独报告的 `cacheReadTokens`；不能只把 313 当作全部输入。探针的保守估价已经修正为包含缓存输入，不必为纯文档/公式变更再次付费调用。

### 费用必须继承

| 项目 | 金额/状态 |
|---|---|
| 用户总授权 | 25 元人民币 |
| 首次成功探针 | 未采集用量，按受限单请求上界保留 2.01 元，不记为 0 |
| 复核探针 | 所有输入保守按峰值缓存未命中估价 0.003242 元 |
| 当前保守预算占用 | 2.013242 元，约 2.014 元 |
| 后续可用总额上界 | 22.986758 元；若有新调用须继续扣减并更新证据 |
| 平台精确账单 | 未核验；以上是预留/估算，不是已核实实扣金额 |

不要因新窗口、重试、切换任务而清空这笔占用；未核实首轮账单前不能把其费用改成零。

## 5. Phase 8 已完成的工作

详细记录：[recovery-verification.md](recovery-verification.md)、[release-evidence.md](release-evidence.md)；最后完整 UI 证据为 [phase8-budget-evidence.json](audits/2026-10-20-phase8-budget-evidence.json)。

- 修复流程更严格额度未采用、有效研究时间未累计、到限不进入求助；两项原失败回归已修复。
- 增加到限/预留不足求助、错误维度或不足追加拒绝、显式继续与费用确认、等待不计时、同 ID 继续及累计消耗保留。
- 节点/模型/工具等待时限与重试前复核，节点尝试次数采用节点/流程/全局较小值，工具 attempt 使用独立 operation 与 deadline。
- 修复已走显式 exhausted 路径后的恢复；保留原失败事实，不因恢复重获尝试次数。
- 求助面板随新快照刷新；历史页新增实际文件写入核验面板。
- 此前事件独立游标、1001 条分页、分支起点/确认/证据恢复等改动也在同一脏工作区内，须保留。

重要文件：`application/engine.py`、`budget_service.py`、`coordinator.py`、`assistance_service.py`、`domain/budget.py`；前端 `AssistancePanel.vue`、`SideEffectsPanel.vue`、`ExecutionHistoryPage.vue`；`test_phase8_budget_gate.py`、`tests/e2e_server.py` 和两组 Phase 8 E2E。以上均位于 `code/`。

这些是原任务的 Mock 验收，不代表 T104–T138 全部整改完成，尤其不能把网关/预算/恢复的局部修复冒充完整官方 Loop 验收。

## 6. 最新验证口径

| 检查 | 最近实际结果 |
|---|---|
| Phase 8 统一检查 `code/scripts/check.ps1` | PASS，7 组检查，整体退出 0 |
| 后端 unit/contract/integration | 285 PASS，92.50 秒 |
| 前端类型 / 单元 | PASS / 47 PASS |
| 策略类型 / 单元 | PASS / 16 PASS |
| 后端 ruff / mypy | PASS / PASS，102 个源码文件 |
| 前端 build | PASS |
| Phase 8 recovery-branches + stop-budget-serial E2E | 18 PASS，约 1.2 分钟 |
| 独立 CLI live 探针 | 最后实跑 1 PASS，3.65 秒；本轮共两次真实成功请求 |
| 新 live 测试的默认入口 | 1 SKIP，不执行真实请求 |
| CLI 文档收尾检查 | 全后端 ruff、git diff --check、文档相对链接、profile 指纹、预算证据校验 PASS |
| 项目采用 T021、两个完整流程真实交付 T100/T132 | 未通过/未运行，保持禁用 |

285/47/16 和 E2E 是 Phase 8 那次实际验收结果，不能在新窗口说成本次重新跑过。后续纯 CLI 文档追加没有改默认应用行为或三个锁文件，也没有新迁移。

已知警告：浏览器断开 SSE 时 aiosqlite 取消清理/Windows 连接重置；CLI 非致命 EPERM。没有宣称修复。

已有未跟踪迁移 `0015_single_model_request.py` 属于此前网关串行约束工作。测试覆盖迁移/回退，不代表已对用户真实数据库执行迁移；不要改旧迁移或擅自迁移用户数据。

## 7. 真正尚未实现的接线与建议继续点

先核对 `tasks.md` 当前依赖，再处理最早真实门禁 T021 的 CLI 接线依赖（T121/T129），并与 T104 的所有模型请求纳管衔接。不要重新研究网页 token，也不要因 CLI 小探针成功跳过网关/工具采用门禁。

当前源码事实：

1. `code/backend/src/personal_agent/adapters/harness/runtime.py` 的 `LiveRuntime.model_transport` 仍拒绝调用，create_session/submit 等仍是未完成入口。**主应用尚无真正 CLI 适配器。**
2. `bootstrap/lifespan.py` 的 live 工具仍为 `UnavailableToolService`；光开环境开关不可能完成真实研究。
3. `code/backend/tests/live/test_runtime_adoption.py` 仍是旧 SDK import/已有证据式门禁，T129 必须改成真正可执行的 CLI/权限/网关/恢复采用测试。
4. `ADOPTION_RECORD = Path(__file__).resolve().parents[4] / 'docs/runtime-adoption.json'` 当前落在 `code/backend/docs`，与仓库根 docs 的文字路径不一致；T121 需统一真实路径、版本/profile 指纹及历史 wheel_interfaces 字段语义，不能伪造 adopted=true。
5. 默认 CLI 可直连并不证明所有生成/压缩/重试已纳管。正式 provider/guard/MCP 仍须实际挂载；拒绝 scope 内后注册越权与网关旁路。
6. CLI 成功必须同时验证进程退出码、完整 final、completed 原因、结构/语义、可信结束及用量。超时或杀进程不等于远端结束，UNKNOWN 保留槽和预留。
7. 无持久化诊断 profile 不支持原 Session 恢复；按业务检查点追加新 attempt/Session 时必须如实标明。`--session-id` 只可用于已验证兼容/存在/未被占用的持久会话。

优先用离线假 CLI/固定事件证明端口、停止/未知/晚到/用量/权限行为，再在剩余额度内做必要真实采用验证。运行时与采用门禁通过后再做 T100/T132 两流程真实交付。Phase 9/10 及其余收敛任务继续按任务依赖推进，不以本交接替代完成。

## 8. 开发与执行入口

强制 CPython **3.12.9**，只经 uv 调用；禁止系统 Python 3.11。Node 24.x、npm 11.x、uv 0.13.1；锁定浏览器 Chromium revision 1248。

```powershell
# 新窗口先阅读技能和约束，再跑前置检查
Get-Content AGENTS.md
Get-Content .agents/skills/speckit-implement/SKILL.md
& .specify/scripts/powershell/check-prerequisites.ps1 -Json -RequireTasks -IncludeTasks
git status --short

# 默认离线检查；避免检查入口移除已安装的可选候选包
$env:UV_OFFLINE = '1'
$env:UV_NO_SYNC = '1'
powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/check.ps1
```

uv 位置：`C:\Users\qiangwei\.local\bin\uv.exe`。Python 解释器由项目 uv 固定选择 3.12，真实路径为 `C:\Users\qiangwei\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\python.exe`。

Phase 8 UI 复验（仅本机隔离 Mock 工作区；需要时再跑）：

```powershell
$env:PLAYWRIGHT_BROWSERS_PATH = 'C:/Code/spiderAgent/.cache/playwright'
$env:NO_PROXY = '127.0.0.1,localhost'
$env:HTTP_PROXY = ''
$env:HTTPS_PROXY = ''
$env:ALL_PROXY = ''
npm run build --prefix code/frontend
npm run test:e2e --prefix code/frontend -- recovery-branches.spec.ts stop-budget-serial.spec.ts
```

真实探针命令在 `docs/release-evidence.md` 最新 CLI 节及测试文件中。**不必为了接收交接再付费跑一次**；若确需实测，先扣除上面的已占用费用，显式启用 `--live` 和 `PERSONAL_AGENT_LIVE=1`，只输出安全元数据。

`.specify/extensions.yml` 在交接时不存在，没有前/后置扩展 hook。Windows 普通写入/格式化遇到拒绝访问时，优先 apply_patch；必要时阅读 windows-safe-write 技能，做小范围 UTF-8 无 BOM 写入及读回验证，不通过清理/改 ACL 破坏现有工作区。
