# 一期实施与验证记录

日期：2026-10-20（Asia/Shanghai）。状态：**实施中，不可声明一期完成或发布**。

## 当前任务状态

`tasks.md` 共 138 项，最新为 94 项已勾选、44 项未勾选：Phase 8 的 T086/T087 已完成 Mock 阶段验收。下文先保留先前实施历史，最新继续实施结果见文末。需求清单评审标记未改；用户此前已同意在 `workflow.md` 仍有 29 个未勾选项时继续实施。

本次优先处理 T104 的模型边界。以下已落地并经过离线回归；T104 整项仍未完成，因为真实官方 Loop 的接线及全部内部请求纳管尚无验证证据。

- 应用装配的 Mock Runtime 通过同一个 ModelGateway 调用固定模型 transport；研究请求包含真实 run/attempt、epoch 和独立预留 operation，而不只记录 submit。
- 应用网关校验已配置模型、有效 owner/attempt、当前执行状态/epoch 和所属账本中未结算的预留。持久记录保存 epoch/预留关联，并防止重复使用预留。
- 数据库唯一部分索引从存储层拒绝两个 `RESERVED/IN_FLIGHT/UNKNOWN` 请求，同时发生的独立网关竞争也不能绕过。请求摘要包含消息内容的 hash，不保存原始消息。
- 本地取消、超时、传输异常、缺少结束证据均保留 UNKNOWN；UNKNOWN 不写远端结束时间，重建网关后仍阻止新请求。只有可信结束记录才释放模型槽；已明确结束的失败响应仍保留其已发生用量。
- 未绑定网关的 Mock Runtime 不再宣称模型已纳管。live 缺少 transport 时明确拒绝，不能回落到 Mock。
- 研究请求在暂停期间被权限撤销后会收敛停止，不转成普通失败或重试。停止/恢复测试现在区分“尚未发送”与“已提交、结果尚未由引擎观察”两个边界。
- 串行回归同时检查实际 transport 调用、非空 ModelRequest 样本、attempt 关联、完成证据和时间区间，拒绝以空样本证明并发上限。

## 验证

全部 Python 命令均经 uv 使用项目 CPython 3.12.9；使用临时数据目录、固定响应和禁止真实网络的默认测试。未调用真实供应商。

| 检查 | 结果 |
|---|---|
| 新增失败用例 | 已先复现取消状态残留、传输错误/缺结束证据、摘要漏消息、未知用量被当零、缺模型白名单、混合 owner、缺 owner/预留校验、研究 ModelRequest 空样本、可信失败不释放槽、能力标志提前宣称纳管及 live 回落 Mock |
| `powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/check.ps1` | 首轮后端 264 项、前端 43 项、策略 16 项通过；三项类型检查通过；该次整体退出 1，原因是全仓 ruff 的 4 处既有样式问题 |
| `uv run --offline --no-sync --project code/backend --python 3.12 ruff check code/backend` | 修正上述未使用变量、导入顺序及多余括号后通过 |
| `uv run --offline --no-sync --project code/backend --python 3.12 mypy code/backend/src` | 最终源码通过，102 个文件 |
| `uv run --offline --no-sync --project code/backend --python 3.12 python -m pytest code/backend/tests/unit code/backend/tests/contract code/backend/tests/integration -q` | 最终 266 项通过，65.65 秒 |
| 新迁移与模型边界定向回归 | 22 项通过（随后又增加 2 项防回退用例，纳入最终全量回归） |
| UI E2E | 本次未运行；前端单元测试通过不替代浏览器验收 |
| live / 官方 Loop 采用与交付验证 | 未运行，保持禁用；无显式 live 授权及费用范围 |

初始沙箱无法访问 uv 全局缓存并启动项目 Python；离线测试在自动审批通过的权限范围下执行，没有更换解释器或绕过网络隔离。

## 数据迁移及回退

新增 `0015_single_model_request.py`，追加 `model_requests.epoch`、可空 `reservation_id`、预留唯一索引和非终结请求唯一部分索引，不改旧迁移。旧记录保留，旧预留关联未知时保持空值；不能伪造关联或完成证据。

迁移在任何模式修改前先核验遗留非终结请求数。多于一个时拒绝升级，保留旧 revision、所有 UNKNOWN 和原表结构，需可信核验处置；禁止为通过迁移直接删除未知请求。已覆盖干净升级、downgrade/重新升级与遗留歧义拒绝。生产回退仍应按维护流程恢复验证过的迁移前备份，本次测试中的 downgrade 不代表生产回退已验收。未运行实际用户数据库迁移。

## 宪章与未解决边界

| 原则 | 本次证据及限制 |
|---|---|
| 确定性 | 模型白名单、owner/epoch/预留校验；官方 Loop 中压缩/重试仍待 T121/T129 真实接线与验证 |
| 可恢复性 | UNKNOWN 跨网关重建阻塞、唯一占槽、迁移冲突保留原数据；完整写入/恢复缺口仍见 T109–T112/T118 |
| 解耦 | 通过现有 Runtime/ModelGateway/transport 端口接线，没有实现供应商 Agent Loop |
| 自动化测试 | 新缺陷先补失败用例，默认回归离线；未运行的 E2E/live 不报告为通过 |
| 数据安全 | 没有读取或保存真实凭据；真实模式缺少 transport 时失败关闭；T113–T120 等安全收敛任务仍未完成 |

`importlib.metadata.version("deepseek-harness-sdk")` 实际返回 PackageNotFoundError。`LiveRuntime.create_session/submit` 仍未实现真实调用，本次没有安装或执行官方 live 依赖，也没有生成采用证据。真实模式仍须完成 T021/T104/T121/T129 及 T100/T132，不可由 Mock 成功替代。

T105/T106 的完整逐调用预算结算、有效时间与 deadline，T107/T108 的输入/确认校验，以及其余未勾选收敛任务仍保留。当前直接依赖仍包含版本范围，T136 的精确固定也未完成。本记录不代表这些缺口已被解决。

## 环境与锁文件

Node 24.21.0、npm 11.19.0、uv 0.13.1、CPython 3.12.9；未升级依赖或修改锁文件。

| 锁文件 | SHA-256 |
|---|---|
| `code/backend/uv.lock` | `e8af2afdf1064b3cc2c852ba82219a44b8850fa6913a862ef3f423cc41d68e66` |
| `code/frontend/package-lock.json` | `8a65e181177e9f30cb761fb9fb28a101976117d5460c0c81caf921a1267e6529` |
| `code/runtime-policy/package-lock.json` | `18a0ded1673315621a9a06959552c90d00d438456bcf177c3f82500b65fa12a1` |

`.specify/extensions.yml` 不存在，前置及后置 implement 钩子均无可分派项。未提交、合并或发布。

## 实施停止点

T104 的官方 Loop 部分未通过：固定 SDK 尚不可用，真实适配器仍未接线，也没有全部内部请求纳管证据。按照 speckit-implement 的 “Halt execution if any non-parallel task fails”，本轮在该未通过任务处收口，不越过该任务将后续收敛工作标成完成。这里将缺少必需接线及验证视为该非并行任务未通过；不是说现有 Mock 测试失败，也不是说剩余 45 项都只需授权即可完成。后续仍需实际实现 T121 对应的运行时/transport/provider 连接及相关离线验证，再在显式授权和费用边界内完成采用及真实交付门禁。

## 2026-10-20 后续 Phase 8 验证（先前停止点）

用户再次请求完成下一个 Phase，并在清单门禁处明确回复“继续”。本次按最早可继续的 Mock 阶段补齐 T086/T087；没有改变 T021 的独立真实接入条件，也没有进入二期。工作区原有修改均保留，以下仅描述本轮新增结果。

已修复事件独立游标/多页补读/reset 重订阅/事件触发快照刷新、1001 条历史分页，以及分支派生草稿账本/摘要、BRANCH 显式确认/返回提案、实际分叉起点、限制前置输出继承和文档证据恢复。分支差异纳入偏好及输出约定，未声明的差异保守阻止。先补失败回归再修复；未将这些局部改动当作 T109/T111/T127/T128 整项收敛完成。

详细场景分母与剩余缺口见 [recovery-verification.md](recovery-verification.md)；17 个执行、6 个分支、最高 epoch=3 的无内容验证记录见 [phase8-recovery-evidence.json](audits/2026-10-20-phase8-recovery-evidence.json)。其中保留实际版本、输入/配置/运行时 hash、事件控制决策 hash 及 attempt/epoch/status，不包含真实凭据或模型内部推理。

| 最终检查 | 结果 |
|---|---|
| `UV_OFFLINE=1` + `powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/check.ps1` | 整体退出 1；仅后端预算门禁测试失败 |
| ruff | PASS |
| mypy | PASS，102 个源码文件 |
| 后端默认离线测试 | **273 PASS / 2 FAIL**，79.27 秒；失败均为新增实际预算门禁 |
| 前端类型检查 / 单元测试 | PASS / 46 PASS |
| 策略类型检查 / 测试 | PASS / 16 PASS |
| 前端 build | PASS |
| Phase 8 Playwright（recovery-branches / stop-budget-serial） | 11 PASS，35.9 秒；不是全部 SC-014—SC-017 验收 |
| `git diff --check` | PASS |
| live / 真实采用和交付 | NOT RUN，未获得独立 live 及费用范围授权 |

本轮将匹配锁文件的 Chromium revision 1248（156.0.8078.4）安装到 `.cache/playwright`，未使用任意本机浏览器替代。默认验证的外部网络和真实模型请求均未启用；浏览器仅访问本机服务，使用固定响应。浏览器断开 SSE 时仍观察到 SQLAlchemy/aiosqlite 的连接取消清理警告，未声称修复。

**当前停止点是 T087 的真实失败门禁**：`code/backend/tests/integration/test_phase8_budget_gate.py` 分别将工作流模型上限设为 1 次、有效时间设为 1 秒，经公开草稿/分析/确认/启动接口运行；实际均为 `SUCCEEDED` 且无预算求助，违背应进入 `WAITING_USER/BUDGET_LIMIT` 的预期。失败用例保留在默认测试中，没有 skip 或 xfail。根因涉及 T105/T106 的更严格流程预算、有效时间累计及到限求助。依 speckit-implement 的非并行任务失败停止规则，在此处停止后续阶段实施。

T086 尚缺完整 UI 的前置不足/写入核验用例，T087 尚未通过预算与授权继续闭环；两项均保留未勾选，总任务数仍为 92 已勾选 / 46 未勾选。需求清单保持原状。Phase 8、一期及发布均不能声明完成。

本轮没有新迁移、依赖升级或锁文件改动，三个锁文件指纹与上表“环境与锁文件”一致。撤销本轮源代码并重新构建前端即可回退本轮行为修改，不应删除用户数据或回退既有迁移。未提交、合并或发布。`.specify/extensions.yml` 仍不存在，前后 implement 钩子均无可分派项。

## 2026-10-20 Phase 8 继续实施结果（最新）

用户授权真实测试合计人民币 25 元，包含模型、付费搜索和重试；需要时使用当前 DeepSeek Harness 实例。本轮只发送一次未认证本机 HTTP 探测（401），没有发送模型 prompt 或供应商计费请求，费用 0 元。通过 uv frozen 安装锁定 SDK/runtime 0.2.1rc2，未修改依赖清单或三个锁文件；现有 dsh 包为 0.2.1-rc.2。真实适配器仍未完成，费用授权不代表采用门禁通过。

修复流程较小预算未生效、有效时间未累计及到限不求助三项根因，并覆盖正余额不足以预留请求、追加不足不能解除阻塞、等待不计时、逐次重试前复核、节点/模型/工具等待上限。节点尝试次数采用节点、流程、全局最小值；独立工具 attempt 使用独立 operation 和 deadline。真实 Loop 内逐动作预留与所有未知用量路径仍属于 T105/T106/T121 后续整改，本轮不勾选这些整项。

新增实际文件写入核验界面、求助面板随提交快照刷新，以及已选择 exhausted 路径的恢复；保留原失败节点事实，不通过恢复重新获得尝试次数。新增用例先复现失败，再修复。所有测试使用临时工作区或 `.cache/e2e-data`，测试宿主只注入外部模型、时间和文件发布观测，不直接构造业务成功/求助记录。

| 最终检查 | 结果 |
|---|---|
| `UV_OFFLINE=1 UV_NO_SYNC=1 powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/check.ps1` | PASS，7 组检查整体退出 0 |
| ruff / mypy | PASS / PASS，102 个源码文件 |
| 后端 unit/contract/integration | 285 PASS，92.50 秒，含 foundational 写入、迁移与恢复回归 |
| 前端类型 / 单元 | PASS / 47 PASS |
| 策略类型 / 单元 | PASS / 16 PASS |
| `npm run build --prefix code/frontend` | PASS |
| `npm run test:e2e --prefix code/frontend -- recovery-branches.spec.ts stop-budget-serial.spec.ts` | 18 PASS，约 1.2 分钟，锁定 Chromium 1248 |
| 真实 Loop / 两流程真实交付 | NOT RUN，接线/采用未完成；现有实例未认证探测 401 |

场景分母映射见 recovery-verification.md。新增 audits/2026-10-20-phase8-budget-evidence.json 只读提取 27 个执行、8 个分支及 65 个实际 Mock 网关请求，保存控制决策 hash、版本、epoch、attempt、预算和写入核验状态，不含原始输入、凭据或内部推理。T086/T087 已勾选，不替评审者修改 checklist，也不以此替代 Phase 11 的安全/一致性整改或一期发布评审。

本轮无新迁移、无真实用户数据操作，锁文件指纹与上表一致。回退本轮源码并重新构建前端即可撤销本轮行为；不要删除用户数据。SDK 只安装于项目环境，可按锁文件重新同步环境。本轮仍观察到 SSE 断开时 aiosqlite 取消清理及 Windows 连接重置警告，未宣称修复。未提交、合并或发布。

## 2026-10-20 后续 CLI 使用方式验证与文档冻结

用户要求先测试 Web/headless 使用方式，再追加技术文档。已新增独立 live 探针及仅调用时生效的覆盖配置，不改应用实现或用户 Web profile。裸 headless 确认 MISSING_CREDENTIAL；用户说明 Key 保存在 Web 后，通过 Harness 自有凭据服务选中已有存储，两次真实模型调用返回 CLI_SMOKE_OK、退出 0，第二次同时确认一个 completed 步骤、0 工具/0 thinking/0 持久会话日志，313 非缓存输入 + 1280 缓存输入 + 7 输出。

真实 live 命令（由 uv 使用 CPython 3.12.9）：`pytest code/backend/tests/live/test_headless_cli_smoke.py -m live --live -q -s --tb=short`，同时由进程环境显式选中已校验的 CLI 入口与已有凭据存储位置。最后一次 **1 PASS，3.65 秒**；默认未授权入口 **1 SKIP**，不发真实请求。原始 stdout/stderr 不落盘，测试只输出白名单状态与用量，未读取或复制 Key。

追加 [Harness CLI 使用约定](harness-cli-usage.md) 并同步历史技术方案第 15 节、plan/research/runtime-adapter 和待办描述：主入口为固定版本 headless CLI 独立进程，Web 8730 可保留；Python SDK API 与 Web RPC 不再作为默认入口。CLI 适配器尚未实现，T021/T100/T121/T129/T132 不勾选，总计仍为 94/138。

npx 包装器先遇 ENOTCACHED/EPERM，真实成功探针使用同版本已安装入口；非致命 EPERM 诊断仍在。账单精确金额未核验；首轮未采集用量按最大请求保留 2.01 元，复核轮按峰值且不计缓存优惠估计 0.003242 元，保守占用 2.013242 元，仍在原 25 元总授权内。证据及计价来源见 audits/2026-10-20-headless-cli-smoke.json，不把缺用量记为零。

本次仅新增 live 验证和文档/契约说明，无依赖清单、锁文件、迁移、默认业务行为变更。后端 285/前端 47/策略 16 的全量结果属于上一次 Phase 8 验收，本次不重复报告为新跑的结果。

本次最终检查：全后端 ruff PASS；默认入口的新增 live 用例 1 SKIP（未偷偷执行真实请求）；git diff --check PASS；5 份技术文档的相对链接、诊断配置 SHA-256、两次成功证据及保守费用低于 25 元的校验 PASS。没有重复付费运行以验证纯文档或估价公式修改。未提交、合并或发布。

## 2026-10-20 本机模型网关代理与真实 CLI 桥接

本轮新增回环模型网关代理、供应商客户端、受限 CLI patch 与任务环境白名单，并把 ModelGateway 的占槽/UNKNOWN/可信结算抽成可复用的 `exchange()`。真实安装的 `0.2.1-rc.2` CLI 已在固定本机 provider 下实际经代理完成一次模型调用；发现并修复 `agent/created` 载荷形状与 `tools.restrict()` 未知工具名两处真实插件不兼容。未调用供应商，新增付费 0 元。

| 检查 | 结果 |
|---|---|
| `check.ps1`（7 组） | PASS，整体退出 0 |
| ruff / mypy | PASS / PASS，108 个源码文件 |
| 后端 unit/contract/integration | 342 PASS，100.51 秒 |
| 前端类型 / 单元 | PASS / 47 PASS |
| 策略类型 / 单元 | PASS / 20 PASS（新增 2 件 restrict 回归） |
| 真实 CLI 经本机网关 | 1 PASS，4.64 秒；1 个 COMPLETED、13 tokens、供应商请求 0 |
| 采用门禁 | 3 PASS / 1 FAIL，15.09 秒；`gateway_binding_mounted`、`managed_tools_mounted` 仍失败，`gateway_no_bypass` NOT RUN |

三个锁文件指纹与本轮前一致；无新迁移、无依赖变更、未执行用户数据库迁移。T121/T129 已在 Phase 2 收口中勾选，`docs/runtime-adoption.json` 仍未生成，真实模式继续禁用。未提交、合并或发布；下一门槛是 `lifespan` 正式装配代理与 CLI 令牌、受管 MCP 工具及无旁路验收。

## 2026-10-20 Phase 2 收口：网关/受管 MCP 正式装配与采用门禁

本轮完成 T121/T129：正式装配本机模型网关代理、真实 transport、Brave/httpx/Playwright/提取 handlers 与官方 MCP client；CLI 只接收回环地址与 attempt 令牌，逐工具 scope 令牌由应用签发并在停止/完成/销毁时撤销；工具结果返回正文/结构。运行真实 CLI 时发现并修复两处插件不兼容（`agent/created` 载荷形状、`tools.restrict()` 未知名字）与两处自身缺陷（guard 缺少节点 scope、MCP 工具名含点被改名）。

| 检查 | 结果 |
|---|---|
| `check.ps1`（7 组） | PASS，整体退出 0 |
| 后端 unit/contract/integration | 346 PASS，97.35 秒 |
| 前端类型 / 单元 | PASS / 47 PASS |
| 策略类型 / 单元 | PASS / 22 PASS |
| 真实 CLI 经网关（无工具 / 含 MCP 工具） | 1 PASS / 1 PASS；模型两次调用均经网关，正文进入上下文，供应商 Key 未进入 CLI |
| 采用门禁 | **4 PASS，13.84 秒**；候选证据 12 项全 true，`adopted=false`，供应商请求 0 |

三个锁文件指纹不变，无新迁移、无依赖变更。T121/T129 勾选，合计 96/138；T021 仍需一次经授权的真实供应商请求与评审记录，T105 逐次预留已在下一节收敛。未提交、合并或发布。

## 2026-10-20 Phase 11 运行时收敛（T104/T105）

新增逐次模型/工具预算 meter，并在生产 `enforce_owner=True` 路径上验证：RUNNING run/attempt 下逐次预留被接受并结算，未知模型结果保留预留，并发拒绝只释放被拒调用。EngineDeps 在 live 装配时改由 meter 计费，不再使用 attempt 级单次预留。

| 检查 | 结果 |
|---|---|
| 后端 unit/contract/integration | 354 PASS，98.47 秒 |
| 前端类型 / 单元 | PASS / 47 PASS |
| 策略类型 / 单元 | PASS / 22 PASS |
| `check.ps1`（7 组） | PASS，整体退出 0 |
| 真实供应商 / 两流程交付 | NOT RUN，待 T021/T100/T132 |

T104/T105 勾选，合计 98/138。无迁移、依赖或锁文件变更；未提交、合并或发布。

## 2026-10-20 Phase 11 预算/时钟/求助收敛（T106）

复核 Phase 8 已有实现并补齐工具边界：过期 deadline 在处理器执行前拒绝，代理对超时显式计费并返回 `TOOL_TIMEOUT`；有效研究时间仍由单调时钟累计，等待不计时。回归覆盖更严格的 model/active/tokens 限额、等待 7200 秒不新增消耗、仅讨论/未回应/缺确认标志拒绝、足额追加后原 run id 恢复、节点尝试上限、运行时 deadline 含剩余有效预算及活动时间到限停止等待。

| 检查 | 结果 |
|---|---|
| 后端 unit/contract/integration | 355 收集，全 PASS |
| 前端类型 / 单元 | PASS / 47 PASS |
| 策略类型 / 单元 | PASS / 22 PASS |
| `check.ps1`（7 组） | PASS，整体退出 0 |
| 真实供应商 / 两流程交付 | NOT RUN，待 T021/T100/T132 |

T106 勾选，合计 99/138。无迁移、依赖或锁文件变更；未提交、合并或发布。

## 2026-10-20 Phase 11 恢复事实与残留清理（T109 部分 / T138）

尝试记录真实输入与依赖 hash；submit 前持久化本地会话关联；Output attempt 只在制品写入成功后标成功；恢复与分支复用重建证据和来源。移除 1 字节无引用残留 `domain/zz1.txt`。T109 的运行时事件流消费与“结果/步骤/检查点/事件同事务”仍未实现，保持未勾选；T138 已勾选。

| 检查 | 结果 |
|---|---|
| 后端 unit/contract/integration | 357 PASS，104.59 秒 |
| 前端类型 / 单元 | PASS / 47 PASS |
| 策略类型 / 单元 | PASS / 22 PASS |
| `check.ps1`（7 组） | PASS，整体退出 0 |

T138 勾选，合计 100/138；T109 保持未勾选。无迁移、依赖或锁文件变更；未提交、合并或发布。
