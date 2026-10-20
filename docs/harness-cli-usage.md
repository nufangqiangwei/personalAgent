# DeepSeek Harness 使用方式（已冻结，2026-10-20）

本文固定本项目的 Harness 接入选择：**Web 可单独运行；项目通过独立的 headless CLI 子进程使用官方 Agent Loop**。不再把连接 8730 Web 进程或调用 Python SDK API 作为主接入方式。此决策只改变运行时载体，不改变 Python 后端、Vue 前端、确定性业务编排或一期范围。

## 1. 固定选择与边界

| 项目 | 固定约定 |
|---|---|
| 名称 | DeepSeek Harness，npm 包 `@deepseek-ai/dsh`；不是另一个名为 Hermes 的 Agent 产品 |
| CLI 版本 | `0.2.1-rc.2`；禁止运行时自动改用 latest |
| 项目入口 | `headless --patch <受限配置> --json -`，任务经 stdin 传入 |
| 调用方式 | Python 适配器启动 Node + 已安装且校验版本的 CLI JS 入口；不通过 shell 拼接命令 |
| 安装/人工入口 | `npx @deepseek-ai/dsh@0.2.1-rc.2 ...`；交付应锁定完整依赖闭包，并解析本地入口 |
| Web | 可保留 127.0.0.1:8730，供用户交互；项目执行不依赖该端口或网页 token |
| 进程与会话 | Web、headless 是不同进程。项目使用独立 `DSH_HOME`、空工作目录及新 Session，不接管 Web 的活动会话 |
| Python SDK | 保留历史调研/已安装候选包，不作为默认调用路径，不允许缺 CLI 时静默切回 SDK 或 Web RPC |

`web`、`headless` 分别是 `--profile web`、`--profile headless` 的简写。headless 完成一次任务后退出，不打开 HTTP 监听端口；`--json` 返回事件流，并不保证模型最终答案符合项目的 JSON Schema。

## 2. 用户 Web 启动方式

原命令可以继续使用，无需为 CLI 停止或重启当前 Web。后续手工启动固定版本：

```powershell
npx @deepseek-ai/dsh@0.2.1-rc.2 web --port 8730
```

启动 URL 的 token 只用于 Web 登录。浏览器换取 Cookie 后可访问干净 URL；独立请求收到 401 不表示 CLI 不可用，也不是 SDK/CLI 模型鉴权的前置门禁。

## 3. CLI 与凭据

未提供模型凭据时，裸命令虽然能够启动，但真实模型请求会以 `MISSING_CREDENTIAL` 失败。**默认 headless 关闭托管凭据服务，不自动继承 Web 设置中保存的 API Key**。

凭据有两个不同用途：

1. 人工诊断可由 Harness 自己的凭据服务读取现有 Web API Key；诊断代码不读取、导出或复制 Key。API Key 场景只需启用 `credentials`，无需启用平台账号登录模块。
2. 项目正式运行仍遵守环境变量/系统凭据存储与统一模型网关边界：真实供应商凭据由网关持有，CLI 只获得本机网关授权。不得把本次诊断的供应商直连配置当作已通过项目权限/记账门禁的生产配置。

已验证的诊断覆盖文件为 [headless-smoke.patch.yml](../code/backend/tests/live/fixtures/headless-smoke.patch.yml)。它只在本次启动生效：

- 启用 Harness 凭据服务，通过 `PERSONAL_AGENT_DSH_CREDENTIAL_STORE` 指定已有存储**位置**，不写入任何 Key 值；未指定时沿用该隔离 home 的默认来源/环境后备。
- 使用 `deepseek-official` / `deepseek-flash`，输出最多 32 tokens，禁用 thinking 与重试。
- 禁用工具、子 Agent、自动压缩和模型标题等额外模型调用入口。
- 禁用会话持久化与原始会话日志；stdout/stderr 只在内存解析，保存白名单元数据。

人工诊断示例（不是完整项目研究配置）：

```powershell
$env:DSH_HOME = Join-Path $env:LOCALAPPDATA 'Personal-Agent/runtime/cli-smoke'
$env:PERSONAL_AGENT_DSH_CREDENTIAL_STORE = Join-Path $env:USERPROFILE '.dsh/.credentials.yaml'
npx @deepseek-ai/dsh@0.2.1-rc.2 headless --patch 'C:/Code/spiderAgent/code/backend/tests/live/fixtures/headless-smoke.patch.yml' --json 'Reply with exactly CLI_SMOKE_OK. Do not invoke any tool.'
```

环境变量中的存储位置不是 API Key，也不是网页 token。不要把 Key 写进覆盖文件、测试夹具、日志或仓库。

项目适配器应使用受管本地入口，避免每个任务重新运行 npm 解析/安装。实际成功探针执行形式为：

```text
node <经 package.json 校验为 0.2.1-rc.2 的 dsh/lib/bin.js> headless --patch <诊断覆盖文件> --json <固定无敏感探针>
```

本机 npm-cache 哈希目录不是业务配置常量，交付时须解析安装位置。生产任务含用户要求，应改用 stdin `-`，不放进进程参数。

## 4. 适配器必须保留的工程约束

- Workflow Engine 仍是唯一业务推进者。CLI 官方 Loop 只执行当前节点，不能修改流程、权限、预算或终态。
- `create_session` 创建本地逻辑关联；`submit` 才启动 CLI。原子记录 run/attempt/epoch、定义/输入/profile/CLI 版本和预留后，才允许模型动作。
- 流式读取 `session`、`status`、`text`、`tool_call`、`tool_result`、`final`；`status.phase=step_end` 提供用量，`turn_end` 提供结束原因。过滤 `thinking`，禁止原始推理落盘。
- 成功需要退出码 0、匹配的完整 final、已知 completed 结束原因、通过结构和语义校验的结果，以及可信结束/用量证据。final 存在但退出码 1 仍是失败；截断事件不能冒充完整结果。
- 正式受限 profile 必须实际挂载 guard/provider/MCP 边界；所有模型生成、压缩和重试仍经过同一个网关并逐次预留/结算。探针的“没有调用工具”不替代越权测试。
- 超时/本地进程退出/强制终止不等于远端模型已结束。结果未知时保留槽与预留，停止新增调用；迟到结果不能覆盖终态或新 epoch。
- 本次无持久化 profile 不提供原 Harness Session 恢复。业务同 ID 继续应追加明确的新 attempt/Session 并注入有效检查点；只有通过兼容/占用/缺失验证的持久会话才能使用 `--session-id`。
- Web 手工调用不自动进入项目账本。项目不接管 Web 会话、不因 Web 正在运行就宣称全机器模型调用已纳管。

这些是保留的采用门禁，不因 CLI 连通成功而豁免。

### 本机模型网关代理（实现，未采用）

正式执行时 CLI 的 `llm-deepseek.baseURL` 指向仅回环的 Messages 代理，`apiKeyEnv` 指向每 attempt 的网关令牌；供应商地址与 Key 只存在于 Python 网关进程。代理对每次模型调用先占用持久 `ModelGateway` 槽并绑定 owner/epoch/预留，再以单次非流式请求转发供应商，把成功回复重建为 Anthropic 事件流；失败、超时与本地终止保留 `UNKNOWN`，不回显供应商错误正文。任务环境只允许网关地址、令牌和 profile 路径三个变量。代理单测与真实 CLI 固定观测已通过，但 `lifespan` 正式装配、受管 MCP 工具与无旁路验收仍未完成，采用记录未生成。

## 5. 本机验证结果与费用

2026-10-20 实际运行 [live 探针](../code/backend/tests/live/test_headless_cli_smoke.py)，两次供应商请求均返回固定答案 `CLI_SMOKE_OK`、退出码 0。第二次同时核验：一个 completed 模型步骤，0 工具调用、0 thinking 事件、0 持久化会话日志；输入 313 个非缓存 tokens、1280 个缓存读取 tokens、输出 7 tokens，合计 1600。耗时分别为 3.78 秒和 3.50 秒。

Web 仍由原 PID 26948 监听 8730；测试没有使用网页 token，没有修改 Web profile，也没有读取或复制凭据值。stdout/stderr 原文不落盘。

真实测试总授权为人民币 25 元，不因用例或重试重置。精确平台账单未核验。按[官方峰值人民币价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)将第二次全部输入保守按未命中缓存计费，估计 0.003242 元；第一次未采集用量，按该受限单请求的最大 1M 输入和 32 输出保留 2.01 元预留，不记作零。两次合计保守占用 2.013242 元，低于授权上限。

执行环境的 npx 包装器先出现 ENOTCACHED/EPERM，因此成功测试使用已安装、已校验版本的 CLI 入口。成功 CLI 仍报告非致命 EPERM 诊断；本记录不将 npm 缓存权限或所有启动诊断描述为已解决。

证据：[2026-10-20-headless-cli-smoke.json](audits/2026-10-20-headless-cli-smoke.json)。默认 pytest 跳过该 live 用例；只有 `--live` 与 `PERSONAL_AGENT_LIVE=1` 才执行。

**通过的是独立 CLI 连通性，不是项目完整执行验收。** `LiveRuntime` 与网关/工具/CLI 接线仍待完成，T021、T100、T121、T129、T132 保持未完成，真实发布仍禁用。此前 SDK stdio 与 Web RPC 主入口设想由本文替代；已有 Python、Vue、数据模型与业务 RuntimePort 保持边界不变。
