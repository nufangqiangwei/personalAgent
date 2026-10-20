# 性能基线（T099）

记录日期：2026-10-20（Asia/Shanghai）。
范围：10,000 条执行记录下的执行列表 / 详情 / 步骤读取，以及停止意图的接收延迟。
模型调用不在口径内，也没有运行任何真实供应商请求。

## 结论

| 指标 | 阈值 | 优化前 | 优化后 | 结果 |
|---|---|---|---|---|
| `GET /api/v1/runs?limit=20` p95 | < 500 ms | 41,339 ms | 119 ms | **PASS**（优化后） |
| `GET /api/v1/runs/{id}` p95 | < 500 ms | 7 ms | 8 ms | **PASS** |
| `GET /api/v1/runs/{id}/steps` p95 | < 500 ms | 7 ms | 6 ms | **PASS** |
| `POST /api/v1/runs/{id}/pause`（无在途动作） | < 1,000 ms | 未单独测量 | 33 ms（max） | **PASS** |
| 提交事件到执行进度 UI p95 | < 1,000 ms | 未执行 | 未执行 | **NOT RUN**（按任务要求不跑 e2e） |
| 点击停止到 UI 反馈 | < 1,000 ms | 未执行 | 未执行 | **NOT RUN**（按任务要求不跑 e2e） |

优化前列表接口确实未达标，且瓶颈已定位、修复并复验；详情/步骤接口在 10k 记录下本来就达标。
前端两条阈值只建立了可执行用例与口径，未在本轮环境中运行，不能视为通过。

## 环境

| 项 | 值 |
|---|---|
| OS | Windows 10 专业版 10.0.19045（64 位） |
| CPU | AMD Ryzen 7 3700X，8 核 / 16 线程 |
| 内存 | 64 GiB（68,641,910,784 bytes） |
| Python | CPython 3.12.9（uv 管理的 3.12 解释器） |
| uv | 0.13.1 |
| SQLite | 3.47.1（aiosqlite 驱动） |
| Node.js | v24.21.0 |
| npm | 11.19.0 |
| Playwright | 1.64.1（`@playwright/test`，前端锁定版本） |
| 后端启动方式 | `uv run --project code/backend --python 3.12 ...`，`--mode mock`，默认 `synchronous=FULL` + WAL |

### 环境备注

本会话的 Windows 默认临时目录（`%TEMP%`）对 SQLite 库不可写：优化前的大结果集排序
需要落临时文件时会报 `sqlite3.OperationalError: unable to open database file`。基线测量时把
`TEMP` / `TMP` 指到工作区内的 `.cache/sqltmp` 后复现/测量正常；优化后查询只取一页
（top-N，不再落盘），最终复验使用默认环境即可。这是本机运行环境的限制，不是产品行为。

## 测量口径

### 后端（`code/backend/tests/integration/test_performance.py`）

- 通过仓库既有 ASGI `client` fixture 发请求：包含 HTTP 路由、中间件、响应序列化和 SQLite 读取；
  不含任何模型调用、网络访问或前端。
- 数据：直接向数据库 `add_all` 批量写入 10,000 条 `WorkflowRun`；每条记录带最小必需关联行
  （`RequirementDraft` + `Confirmation`，均满足外键）和 1 条 `BudgetLedger`；第 9,999 条
  （倒数第二条，index 9998）带 3 个 `StepRun` 和 3 个 `StepAttempt`，供详情/步骤接口做真实读取。
  批量写入约 4 秒（3 次 `flush` + 1 次 `commit`），没有逐行 HTTP 请求。
- 采样：先做 1 次预热（不计入），再连续采样 20 次；p50 取中位数，p95 使用 nearest-rank
  （`sorted[ceil(0.95·n)-1]`）。列表请求固定 `limit=20`。
- 停止：直接构造 5 个 `RUNNING` 且无 `StepAttempt` 在途的执行，逐个 `POST .../pause`
  （带 `If-Match` 与 `Idempotency-Key`），要求返回 `PAUSED`、`draining=false`，并记录 HTTP 往返。
- 默认离线；`@pytest.mark.slow` 未使用（仓库只配置了 `live` 标记），10k 用例本身约 5–6 秒
  （含第二个暂停用例的整个文件约 8 秒）。

### 前端（`code/frontend/tests/e2e/progress-latency.spec.ts`）

按任务要求**未运行**（需要已构建前端 + 后端）。用例口径：

- 先通过公开 API（与 UI 相同的 CSRF / Idempotency-Key / If-Match）把新执行在极早期暂停；
  打开 `/runs/{id}` 后点击“继续（同 ID）”，使事件在页面 SSE 已建立后才提交。
- 事件提交时间取自 `GET /api/v1/runs/{id}/event-history` 返回的 `occurred_at`；渲染时间取
  浏览器内检测到该 `sequence` 出现在事件列表时的 `performance.timeOrigin + performance.now()`。
  服务端与浏览器在同一台机器上，时钟可直接比较。
- 每个被暂停后恢复的执行产生约 7 个已提交事件；最多使用 8 个执行累计到至少 20 个样本，
  按 nearest-rank 计算 p95，要求 < 1,000 ms。
- 停止反馈：在已暂停的页面上点击“继续”，等“暂停”按钮由服务端 `allowed_actions` 变为可用后点击；
  记录点击时刻与 `.controls [role="status"]` 首次出现“正在收敛…”（draining）或
  “状态已更新为 PAUSED。”的时刻，要求 < 1,000 ms。若恢复后的执行先结束导致按钮不可用，
  用例会换一个新执行重试（最多 3 次）。

## 优化前基线（失败）

`list_runs` 先 `SELECT` 全部 10,000 行，再对每行调用 `serialize_run`（每次约 7 条查询：
额度、求助、文档、事件序号、在途动作、未结求助、allowed_actions），最后才在内存里
`paginate`。因此约 7 万次查询，列表接口耗时 40 秒级。

`pytest ... -s -k read_paths`，预热 1 次后采样 3 次（每次约 40 秒，未跑满 20 次）：

| 接口 | n | p50 | p95 | min | max |
|---|---:|---:|---:|---:|---:|
| 列表 `limit=20` | 3 | 40,927 ms | 41,339 ms | 39,570 ms | 41,339 ms |
| 详情 | 3 | 7 ms | 7 ms | 7 ms | 7 ms |
| 步骤 | 3 | 6 ms | 6 ms | 5 ms | 6 ms |

判定：列表 **FAIL**（p95 41.3 s > 0.5 s），详情、步骤 PASS。

## 优化

只改 `code/backend/src/personal_agent/api/runs.py` 的 `list_runs`：

- 在 SQL 层分页：`ORDER BY created_at DESC, id` + `OFFSET start` + `LIMIT limit+1`，
  只对返回的这一页（最多 20 条）调用 `serialize_run`；
- 多取 1 行只为判断 `next_cursor`，响应结构与游标语义保持不变（`items` + `next_cursor`，
  非法游标仍返回 `INVALID_CURSOR`，`limit` 仍夹在 1–100）；
- 没有改索引、缓存或阈值，也没有放宽任何测量口径。

## 优化后复验（通过）

同一测试、默认 20 次采样。下表是最后一次复验（工作区同时有其他测试进程在跑）；
在此之前的一次复验为列表 p50 94 ms / p95 97 ms，两次都远低于 500 ms：

| 接口 | n | p50 | p95 | min | max |
|---|---:|---:|---:|---:|---:|
| 列表 `limit=20` | 20 | 105 ms | 119 ms | 91 ms | 140 ms |
| 详情 | 20 | 7 ms | 8 ms | 7 ms | 8 ms |
| 步骤 | 20 | 6 ms | 6 ms | 5 ms | 6 ms |
| 暂停（无在途动作） | 5 | 25 ms | 33 ms | 22 ms | 33 ms |

判定：四项全部 PASS。列表 p95 从 41,339 ms 降到约 100–120 ms（另一次复验为 97 ms）。

## 复现命令

```powershell
# 后端 10k 基线（默认 20 样本；约 8 秒）
uv run --project code/backend --python 3.12 pytest code/backend/tests/integration/test_performance.py -q

# 需要看 p50/p95 时加 -s
uv run --project code/backend --python 3.12 pytest code/backend/tests/integration/test_performance.py -q -s

# 前端类型检查与用例发现（本轮实际执行；e2e 本身未运行）
npm run typecheck --prefix code/frontend
$env:PERSONAL_AGENT_E2E_NO_SERVER='1'; npm run test:e2e --prefix code/frontend -- --list
```

## 未运行 / 限制

- Playwright e2e 未运行：任务明确要求不跑（需要 `npm run build` 的前端产物和运行中的后端）。
  因此“提交事件到 UI p95 < 1 s”和“停止反馈 < 1 s”没有实测数字，不能报告为通过；
  本轮只保证 TypeScript 类型检查通过且 Playwright 能发现两条用例。
- 后端测量是单进程 ASGI 直连，不含真实网络栈、浏览器渲染和进程间调度；它衡量的是
  服务端读取路径，不等价于端到端 UI 延迟。
- 10k 基线在共享工作区完成；若其他测试进程同时运行，可能影响绝对毫秒数，但优化前后
  的差距（40 秒级 → 约 100 ms）远大于该噪声。
- 未做并发/多用户压测；单执行槽、单用户本地面板的场景不在本轮范围内。
