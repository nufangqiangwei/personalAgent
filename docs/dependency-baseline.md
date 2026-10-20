# 依赖基线与指纹（T006）

记录日期：2026-10-21（Asia/Shanghai）。本文件记录**本机实际解析并安装**的版本与锁文件指纹。
锁文件已提交，禁止手工编辑；升级依赖必须重新生成、复核并更新本文件。
T136 起后端/前端/策略插件的运行与开发直接依赖一律使用精确版本（`==`/无前缀版本号），
锁文件由 `uv lock`、`npm install --package-lock-only` 重新生成；不得手改锁文件。
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

| 包 | 版本 | 锁定 |
|---|---|---|
| fastapi | 0.148.0 | `==0.148.0` |
| pydantic | 2.14.1 | `==2.14.1`（Harness SDK 0.2.1rc2 要求 `pydantic>=2.12`） |
| sqlalchemy | 2.0.55 | `==2.0.55` |
| alembic | 1.20.0 | `==1.20.0` |
| aiosqlite | 0.22.1 | `==0.22.1` |
| httpx | 0.28.1 | `==0.28.1` |
| jsonschema | 4.26.0 | `==4.26.0` |
| playwright | 1.64.0 | `==1.64.0` |
| pytest | 8.4.2 | `==8.4.2` |
| pytest-asyncio | 1.4.0 | `==1.4.0` |
| ruff | 0.17.1 | `==0.17.1` |
| mypy | 1.20.2 | `==1.20.2` |
| uvicorn | 0.54.0 | `==0.54.0` |

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
| jsdom | 26.1.0 |
| @vue/test-utils | 2.5.1 |
| @types/node | 24.19.2 |
| @vitejs/plugin-vue | 6.0.10 |

## 策略插件（`code/runtime-policy/package-lock.json`）

| 包 | 版本 |
|---|---|
| typescript | 5.9.3 |
| vitest | 3.2.7 |
| @types/node | 24.19.2 |

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

## 候选真实运行时（已安装，采用未通过）

| 包 | 版本 | 状态 |
|---|---|---|
| deepseek-harness-sdk | 0.2.1rc2 | 2026-10-20 已通过 uv frozen 安装，采用未通过 |
| deepseek-harness-runtime-bin | 0.2.1rc2 | 已安装，由 SDK 同版本传递依赖；真实接线未通过 |

- SDK wheel `sha256:30fc72c67f20ff4b46c6952087ea596fc70c2692001aa372185a24f9866bdc46`（PyPI 0.2.1rc2）。
- 采用门禁见 `docs/runtime-adoption.md`（T021）。门禁未通过前：
  `--mode live` 必须返回不可用，真实 provider 凭据不会被读取，发布不得宣称真实模式完成。
- 2026-10-20 使用 `uv sync --project code/backend --python 3.12 --frozen --extra live --extra dev` 安装固定闭包，SDK 可 import；三个锁文件未改。安装成功不构成 Windows 运行时、provider/guard、无旁路或会话恢复通过的证据，T021 仍未闭合。

## 2026-10-20 CLI 载体追加决定

主调用方式固定为 npm `@deepseek-ai/dsh@0.2.1-rc.2` 的 headless CLI，Node v24.21.0；不再以 Python SDK API 为默认入口。两次独立真实模型连通通过，完整边界见 [harness-cli-usage.md](harness-cli-usage.md)。这不是完整项目运行时采用通过；原三个项目锁文件未改，正式 CLI 依赖闭包的分发锁定仍须由 T121/T136 完成。

成功探针直接启动当前已安装包的版本校验入口，记录以下 SHA-256 以便复核；npx 包装器的缓存/权限错误不作为 CLI 实际模型失败混淆，也不隐瞒。

| 本机已安装资料 | SHA-256 |
|---|---|
| `@deepseek-ai/dsh/lib/bin.js` | `80112efe368efb1a39720c7ada0af00442e5131fb2744edaaff8a3e8bb499e6f` |
| 所在 npx 安装闭包的 `package-lock.json` | `88fb50fe05c6a3ea2355ce40627aa76a01f5b39c83743438bc4eca22cdae5f8d` |
| 诊断 profile 覆盖文件 | `2b834e3c95571229b3896e004c78f4c1131fccc5e85405a2fa7ea3032d60e3e0` |

以上是本机安装证据，不是把用户 npm-cache 哈希目录作为发布路径，也不代替应提交的项目锁文件。

## 锁文件指纹（SHA-256）

| 文件 | SHA-256 |
|---|---|
| `code/backend/uv.lock` | `89053f6b813cd7397f548d53499cb39c01aff5933d602bba65af74f6130b65f0` |
| `code/frontend/package-lock.json` | `42a74f3a42a79c9666a5652135f73f1924b4296d92db1ae5d1daabc377fc0a73` |
| `code/runtime-policy/package-lock.json` | `cf20c038cc7550b5a15d99c39f41d9d052fb9ecba77c8f11fe96999f8e58147d` |

重新生成后必须更新上表；`code/scripts/check.ps1` 会输出当前实际 hash 以便比对。
