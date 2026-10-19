# 交接文档（2026-10-19 第二轮）

> 用途：本轮在 US9 后端控制面收尾后主动停止，供**新对话**无损接续。
> 新对话建议开场：「继续 /speckit-implement，先读 docs/handoff-2026-10-19-round2.md」。
> 目标（session goal）如仍暂停，恢复时用 goal resume 或直接按本文档继续。

## 1. 一句话状态

一期 103 个任务已完成 **70 项**；US4（文档交付）全部完成；US9 已完成
停止/恢复/求助后端的**可验证部分**（T071、T074、T076、T077），
**T072/T073/T075/T078–T080 未完成或未验收**，T081–T103 未开始；
真实运行时仍按计划禁用（T021 未运行）。

## 2. 验证证据（本轮实测）

| 检查 | 命令 | 结果 |
|---|---|---|
| 静态检查 | `uv run --project code/backend ruff check code/backend` | All checks passed |
| 类型检查 | `uv run --project code/backend mypy code/backend/src` | no issues（88 files） |
| 离线测试 | `uv run --project code/backend python -m pytest code/backend/tests -q` | **176 passed, 7 skipped**（live） |
| 前端类型 | `npm run typecheck --prefix code/frontend` | 通过 |
| 前端单测 | `npm run test --prefix code/frontend -- --run` | **38 passed** |
| 端到端 | `code/scripts/e2e.ps1`（需 `PERSONAL_AGENT_CHROMIUM_PATH`） | **13 passed** |
| 迁移 | `test_migrations.py` | 通过；head=`0011` |
| live 用例 | `code/backend/tests/live/` | **未运行**（需 `--live`、`PERSONAL_AGENT_LIVE=1`、凭据与授权） |

## 3. 本轮完成内容

| 任务 | 说明 |
|---|---|
| T063 | `tests/contract/test_artifact_delivery.py`（9 例）：列表/元数据/document、ETag=sha256、安全文件名、重复读取不重跑研究、410/INTEGRITY_ERROR、PARTIAL 不伪装 COMPLETE |
| T068 | `ArtifactService.get` 读取时也检查生命周期（删除/过期 410），完整性错误统一 503 脱敏 |
| T069 | `ArtifactPage.vue`、`SourceList.vue` + 5 例单测（阅读/保存/再次获取/不可访问不显示已交付） |
| T070 | `tests/e2e/04-artifacts.spec.ts`（3 例）、`tests/integration/test_document_checkpoint.py`（4 例） |
| T071 | `tests/contract/test_run_controls.py`（6 例）：pause/cancel/resume/recovery/history 与 428/412/409 |
| T074 | 求助实体与迁移 `0010` 已存在，套件验证通过 |
| T076 | `application/stop_control.py`：意图即时返回、收敛后才 PAUSED/CANCELLED、迟到结果隔离 |
| T077 | `application/recovery.py`：7 项核验、原 run 追加 epoch 续跑、已成功节点不重做 |

### 关键实现位置（新增）

- `application/stop_control.py`：pause/cancel/converge/observe
- `application/recovery.py`：inspect / resume（版本、运行时、政策、输入、写入、产物、模型）
- `api/run_controls.py`：`POST /runs/{id}/pause|cancel|resume`、`GET /runs/{id}/recovery|history`
- `application/engine.py`：步进前/后收敛检查；成功后写 `selected_transition` 与 SUCCEEDED；
  `resume=True` 从已成功步骤之后继续；尝试编号续接；取消后迟到结果记 `late_result.quarantined`
- `api/assistance.py` + `application/assistance_service.py`（**未验收**，见 §4）
- `domain/branches.py` + `persistence/branch_models.py` + 迁移 `0011_branches.py`（**仅模型与纯函数**）

### 顺带修复的真实缺陷

已完成步骤此前一直停留在 `RUNNING` 且 `selected_transition` 从不写入，
导致恢复会重做、历史不可信。已在 `test_fixed_research.py` 增加回归用例
`test_every_executed_step_is_recorded_as_succeeded_with_its_transition` 并修复。

## 4. 未完成 / 未验收（不要误标）

| 任务 | 现状 | 下一步 |
|---|---|---|
| T072 | 未开始 | `tests/integration/test_recovery.py`：节点完成/未完成、写入已生效未记账、UNKNOWN、迟到结果、版本不兼容、cancel_requested |
| T073 | 未开始 | `tests/integration/test_serial_execution.py`：双击/跨任务/分支切换/运行中聊天/超时重试/停止在途六例 |
| T075 | 仅有模型+迁移 | `BranchProposal`/`ReusedResult` 已建表，未接线、无测试 → 保持未勾选 |
| T078 | 已实现未验收 | `assistance_service.decide` 覆盖 DISCUSS/GRANT_AND_CONTINUE/CANCEL/CHANGE_REQUIREMENTS/RECONCILE，但**没有契约用例**；请先补 `tests/contract/test_assistance.py` 再勾选 |
| T079 | 未开始 | `application/branch_analysis.py`（`domain/branches.py` 的 `compute_impact` 可直接复用） |
| T080 | 未开始 | `application/branch_service.py` |
| T081 | 部分 | 运行控制与历史已实现；分支/求助端点中求助已接、分支未接 |
| T082 | 未开始 | SSE + event-history |
| T083–T087 | 未开始 | 前端控制/求助/历史页与 E2E |
| T088–T103 | 未开始 | US10 与发布收口 |

## 5. 新对话的第一批动作（按顺序）

1. `powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/check.ps1`
   确认基线 `ALL CHECKS PASSED`（脚本含 backend 全套、前端、策略插件）。
2. **T078 验收**：补 `tests/contract/test_assistance.py`：
   - `GET /runs/{id}/assistance` 与 `GET /assistance/{id}`；
   - DISCUSS 只记录、仍 `WAITING_USER`，不自动继续；
   - `GRANT_AND_CONTINUE` 缺少 `approved_increment`/`continue_authorized`/`acknowledge_cost` 时 422
     `ASSISTANCE_DECISION_INVALID`；齐全时追加额度并续跑；
   - CANCEL 走取消；RECONCILE 不信任客户端声明（服务端重新核验）；
   - 过期 revision 412、已关闭求助 409。
3. **T075/T079/T080**：把 `compute_impact` 接到 `branch_analysis`，
   再实现 `branch_service`（派生 draft → BRANCH 确认 → 新 run 仅复用起点前有效成果、
   共享根预算），迁移已就绪（`0011`）。
4. **T072/T073**：可直接复用 `tests/contract/test_run_controls.py` 里的 `GatedRuntime` 模式
   做确定性中断。
5. 之后 T082 SSE 与 T083–T087 前端。

## 6. 环境与工具注意事项（沿用上一轮，全部仍有效）

1. **需要完全权限**：受限沙箱下 npm/Playwright 管道子进程会 EPERM。
2. **PowerShell 为 5.1**：`Get-Content` 默认 ANSI，读 UTF-8 要加 `-Encoding utf8`；
   写文件用 `[System.IO.File]::WriteAllText(..., UTF8Encoding($false))`。
3. **中文提交信息**：写入 UTF-8 文件后 `git commit -F .cache/msg-*.txt`。
4. **ruff --fix 写回失败**（E902 拒绝访问）：由文件工具创建的 `src` 文件手工改样式。
5. **E2E 浏览器**：
   ```powershell
   $env:PERSONAL_AGENT_CHROMIUM_PATH = (Get-ChildItem "$env:LOCALAPPDATA\ms-playwright\chromium-*\chrome-win64\chrome.exe" | Sort-Object FullName -Descending | Select-Object -First 1).FullName
   powershell -NoProfile -ExecutionPolicy Bypass -File code/scripts/e2e.ps1
   ```
6. **解释器**：一律 `uv run --project code/backend ...`（CPython 3.12.9）。
7. **迁移**：head 为 `0011`；生成新迁移前把 `PERSONAL_AGENT_DB_URL` 指向临时库，
   新迁移按 `00NN_name.py` 命名并把 `revision` 改成四位编号（现有约定）。
8. **真实模式**：`PERSONAL_AGENT_RUNTIME_ADOPTED` 未设为 1，`--mode live` 会拒绝启动。
9. **未使用的杂项**：`code/backend/src/personal_agent/domain/zz1.txt` 是早期误提交的
   单字符文件，本轮未删；下轮可一并清理（无引用）。

## 7. 最近提交（交接点）

```
<本轮最后一次提交> test(T071/T074/T076/T077): 运行控制、恢复与求助后端
6b20c62 test(T070): 文档端到端与保存到重新打开的一致性
36cadc4 feat(T069): 文档阅读页与来源列表
8697a05 test(T063/T068): 产物交付契约用例与生命周期读取校验
```

## 8. 交接检查清单

- [x] 工作区提交后干净，无未提交改动
- [x] ruff / mypy / pytest（176 passed）/ 前端单测（38）/ E2E（13）通过
- [x] 任务勾选与代码一致（70/103），未验收项已明确标注
- [x] 下一步动作与命令已写明
- [x] 真实接入门禁状态与授权前提仍写明
