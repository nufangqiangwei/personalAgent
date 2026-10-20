# 一期验收矩阵（T130 / T098）

记录日期：2026-10-21（Asia/Shanghai）。分母按“例”计数；结果只记录实际执行过的证据，
未运行项如实标注，不以测试发现或单次 Mock 成功替代验收。证据均为可重复执行的测试文件，
E2E 与真实供应商用例单独标注。

## 成功标准

| 标准 | 分母 | 结果 | 证据 |
|---|---|---|---|
| SC-001 完整闭环 | 3 类输入（完整、缺失、补充纠正） | 离线契约/集成覆盖；UI 端到端未运行 | `tests/contract/test_requirements.py`、`tests/integration/test_fixed_research.py`；`tests/e2e/complete-journey.spec.ts`（未运行） |
| SC-003 严格执行 | 8 个样例（正常/分支/失败/版本变化） | 离线样例通过；未授权跳步/版本替换为 0 | `tests/integration/test_fixed_research.py`、`tests/contract/test_workflows.py`、`tests/contract/test_branches.py` |
| SC-005 可信交付 | 全部合格事实 | 重要事实均带来源或未证实标记；无来源合格推荐为 0 | `tests/unit/test_engine.py`、`tests/unit/test_document_rendering.py` |
| SC-006 格式与要求 | 3 结构 × 2 组喜好 | 3 结构、2 详略、相反优先序排序通过；未跨任务偏好 | `tests/unit/test_document_rendering.py`、`tests/unit/test_engine.py` |
| SC-007 版本记录 | 2 流程 × 3 次保存 | 旧版本保留、默认最新版本正确；A→B→A 生成新版本 | `tests/contract/test_workflows.py` |
| SC-008 异常反馈 | 11 类异常 | 缺输入/空库/失败/无合格结果/超时/取消/中断/冲突等有状态；误报完成为 0 | `tests/contract/test_run_controls.py`、`tests/integration/test_recovery.py`、`tests/contract/test_artifact_delivery.py`、`tests/unit/test_requirement_confirmation.py` |
| SC-009 UI 可用性 | 3 次独立研究 + 1 停止 + 1 失败重发 | E2E 未运行（Chromium 未安装） | `tests/e2e/complete-journey.spec.ts`、`recovery-branches.spec.ts`（未运行） |
| SC-012 稳定性 | 12 次执行（2 流程 × 3 输入 × 2 次） | **12/12 均产生可获取文档**；超时/依赖失败正确收敛；取消与中断沿用既有用例 | `tests/integration/test_phase1_stability.py`；`tests/contract/test_run_controls.py`（取消）；`tests/integration/test_recovery.py`（中断） |
| SC-013 要求确认 | 6 场景 | 前五类不启动、确认后按最终内容启动；未确认采用为 0 | `tests/contract/test_requirements.py`、`tests/unit/test_requirement_confirmation.py` |
| SC-014 历史与分支 | 6 情形 | 恢复/分叉/再次分叉/缺前置成果通过；原分支成果未被覆盖 | `tests/contract/test_branches.py`、`tests/integration/test_recovery.py` |
| SC-015 分支要求变更 | 4 情形 | 影响范围/拒绝前移/无法确定通过；未覆盖旧输入 | `tests/contract/test_branches.py`、`code/frontend/tests/unit/branch-panel.spec.ts` |
| SC-016 暂停取消与上限 | 7 情形 | 暂停/取消/上限求助通过；取消不可直接恢复 | `tests/contract/test_run_controls.py`、`tests/integration/test_phase8_budget_gate.py` |
| SC-017 串行执行 | 6 情形 | 单执行槽/单模型在途；被阻止操作有反馈 | `tests/contract/test_run_controls.py`、`tests/integration/test_serial_execution.py` |

## A1–A5

| 指标 | 分母 | 结果 | 证据 |
|---|---|---|---|
| A1 授权边界 | 越权访问样例 | 越权被拒绝并说明原因 | `tests/integration/test_security_boundaries.py` |
| A2 外部资料边界 | 含指令注入材料 | 目标/权限/流程/上限保持不变 | `tests/integration/test_security_boundaries.py`、`tests/unit/test_redaction.py` |
| A3 隐私保护 | 含凭据材料 | 文档/状态/日志不出现可用凭据 | `tests/unit/test_redaction.py`、`tests/integration/test_security_boundaries.py` |
| A4 数据生命周期 | 保留期/删除样例 | 到期与删除后不可普通访问，记录标记已删除 | `tests/integration/test_data_lifecycle.py` |
| A5 单用户模式 | 工作区/文档访问 | 无账号/租户/角色过滤（架构约束） | 代码审查：`api/app.py`、`bootstrap/acl.py` |

## 未运行 / 缺口

- E2E（T088/T096/T131）：Playwright Chromium 二进制未安装，未作为发布证据运行。
- 真实供应商（T021/T100/T132）：无用户凭据与付费授权，未运行；`adopted=false`。
- SC-005/SC-006 的比率口径见 `spec.md`；两处比率由上述测试逐例统计，尚无独立发布级汇总。

T130 的 12 次执行与失败/超时收敛已由 `test_phase1_stability.py` 覆盖；取消/中断收敛沿用既有用例，
未新增重复实现。
