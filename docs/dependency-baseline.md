# 依赖基线与指纹（T006）

记录日期：2026-10-19（Asia/Shanghai）。本文件记录**本机实际解析并安装**的版本与锁文件指纹。
锁文件已提交，禁止手工编辑；升级依赖必须重新生成、复核并更新本文件。
"候选"表示尚未通过真实接入门禁，不得据此宣称真实模式可用。

## 工具链

| 项目 | 实际值 | 要求 |
|---|---|---|
| OS | Windows x64 | Windows x64 |
| Python | CPython 3.12.9 | `>=3.12,<3.13` |
| 解释器路径 | `C:\Users\qiangwei\AppData\Roaming\uv\python\cpython-3.12-windows-x86_64-none\python.exe` | 禁止系统 Python 3.11.3 |
| uv | 0.13.1 | `>=0.8,<1` |
| Node.js | 24.21.0 | 24.x |
| npm | 11.19.0 | 11.x |

## 后端（`code/backend/uv.lock`）

`uv sync --project code/backend --python 3.12 --extra dev` 解析 45 个包，关键直接依赖：

| 包 | 版本 | 计划范围 |
|---|---|---|
| fastapi | 0.148.0 | `>=0.115,<1` |
| pydantic | 2.14.1 | `>=2.12,<3`（Harness SDK 0.2.1rc2 要求 `pydantic>=2.12`） |
| sqlalchemy | 2.0.55 | `>=2.0,<2.1` |
| alembic | 1.20.0 | `>=1.14,<2` |
| aiosqlite | 0.22.0 | `>=0.20,<1` |
| httpx | 0.28.1 | `>=0.28,<1` |
| jsonschema | 4.26.0 | `>=4.23,<5` |
| playwright | 1.64.0 | `>=1.50,<2` |
| pytest | 8.4.2 | `>=8,<9` |
| pytest-asyncio | 1.4.0 | `>=0.24,<2` |
| ruff | 0.17.1 | `>=0.7,<1` |
| mypy | 1.20.2 | `>=1.13,<2` |
| uvicorn | 0.54.0 | `>=0.30,<1` |

## 前端（`code/frontend/package-lock.json`）

| 包 | 版本 |
|---|---|
| vue | 3.5.44 |
| vue-router | 4.6.4 |
| vite | 7.3.7 |
| vitest | 3.2.7 |
| typescript | 5.9.3 |
| vue-tsc | 3.3.13 |
| @playwright/test | 1.64.1 |
| jsdom | 26.x |
| @vue/test-utils | 2.4.6 |

## 策略插件（`code/runtime-policy/package-lock.json`）

| 包 | 版本 |
|---|---|
| typescript | 5.9.3 |
| vitest | 3.2.7 |
| @types/node | 24.x |

## 浏览器

| 项目 | 值 |
|---|---|
| Playwright Python | 1.64.0 |
| @playwright/test | 1.64.1 |
| Chromium revision | 1248 |
| Chromium version | 156.0.8078.4 |

Python `playwright` 与 npm `@playwright/test` 在补丁号上相差一位（1.64.0 / 1.64.1），
但两者的 Chromium revision 均为 1248，浏览器二进制一致。E2E 只使用
`code/frontend` 的 `@playwright/test` 启动 Chromium；后端只用 Python Playwright 做
`browser.read` 工具。升级任一侧必须同时核对 revision。

Chromium 二进制尚未安装（`uv run --project code/backend python -m playwright install chromium`
只在需要浏览器工具/E2E 时执行）。未安装不阻塞离线单元/契约测试。

## 候选真实运行时（未安装、未验证）

| 包 | 版本 | 状态 |
|---|---|---|
| deepseek-harness-sdk | 0.2.1rc2 | 候选，声明在 `[project.optional-dependencies].live`；**未安装** |
| deepseek-harness-runtime-bin | 0.2.1rc2 | 由 SDK 同版本传递依赖；**未安装** |

- SDK wheel `sha256:30fc72c67f20ff4b46c6952087ea596fc70c2692001aa372185a24f9866bdc46`（PyPI 0.2.1rc2）。
- 采用门禁见 `docs/runtime-adoption.md`（T021）。门禁未通过前：
  `--mode live` 必须返回不可用，真实 provider 凭据不会被读取，发布不得宣称真实模式完成。
- `--extra live` 尚未安装，因此本文件不对其依赖闭包或 Windows 行为作任何已验证声明。

## 锁文件指纹（SHA-256）

| 文件 | SHA-256 |
|---|---|
| `code/backend/uv.lock` | `e8af2afdf1064b3cc2c852ba82219a44b8850fa6913a862ef3f423cc41d68e66` |
| `code/frontend/package-lock.json` | `8a65e181177e9f30cb761fb9fb28a101976117d5460c0c81caf921a1267e6529` |
| `code/runtime-policy/package-lock.json` | `18a0ded1673315621a9a06959552c90d00d438456bcf177c3f82500b65fa12a1` |

重新生成后必须更新上表；`code/scripts/check.ps1` 会输出当前实际 hash 以便比对。
