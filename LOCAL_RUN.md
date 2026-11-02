# 本地运行（Conda）

在项目根目录执行。统一使用 `tradingagent` 环境，Python 3.13。

## 统一管理脚本（环境已安装）

无需手动激活 Conda，统一使用 `./tradingagents.sh <命令> [参数]`：

| 命令 | 用途 |
| --- | --- |
| `./tradingagents.sh start` | 前台启动交互式分析（无参数执行脚本也是此行为） |
| `./tradingagents.sh stop` | 停止当前项目脚本管理的分析进程 |
| `./tradingagents.sh restart` | 复用上次分析参数和运行方式重新启动 |
| `./tradingagents.sh status` | 查看当前分析进程状态 |
| `./tradingagents.sh logs` | 查看后台日志 |
| `./tradingagents.sh login chatgpt` | 登录 ChatGPT 订阅 |
| `./tradingagents.sh login grok` | 登录 Grok 订阅 |
| `./tradingagents.sh logout chatgpt` | 退出 ChatGPT 订阅（Grok 使用 `grok`） |
| `./tradingagents.sh auth-status` | 查看本地订阅登录状态 |
| `./tradingagents.sh --help` | 查看管理脚本帮助 |
| `./tradingagents.sh cli --help` | 查看原始项目 CLI 帮助 |

`start`、`restart` 后面的分析参数会传给项目，例如：

```bash
./tradingagents.sh start --ticker NVDA --date 2026-10-27 --analysts market --save --no-show
```

需要后台执行时增加 `--background`：

```bash
./tradingagents.sh start --background --ticker NVDA --date 2026-10-27 --analysts market --save --no-show
./tradingagents.sh status
./tradingagents.sh logs
./tradingagents.sh restart
./tradingagents.sh stop
```

后台启动前，应先登录订阅，并在 `.env` 中设置供应商、快／深模型 ID、输出语言和两项辩论轮数（`TRADINGAGENTS_MAX_DEBATE_ROUNDS`、`TRADINGAGENTS_MAX_RISK_ROUNDS`），保证 CLI 不需要交互提问。缺少配置时进程会退出，原因保存在日志里。只有完成分析并生成报告才算一次分析成功，启动后台进程不表示分析已完成。

前台运行时，在另一个终端执行 `stop` 即可关闭。`restart` 会关闭旧进程；前台任务在执行 restart 的终端中重新启动。分析是一次性任务，正常完成后状态为停止，不会自动循环重跑。停止可能中断当前分析；需要恢复时使用项目的 `--checkpoint` 选项。

运行记录和后台日志按项目目录隔离，存放在 `~/.tradingagents/runtime/` 下。脚本不会停止未由它管理的 Python 进程，也不会将订阅登录输出写入后台分析日志。`status` 在没有由本脚本管理的进程时退出码为 1，这只表示未运行，不是环境错误。

脚本可从任意目录通过绝对路径调用，自动定位项目并使用 `tradingagent` 环境；保留已有代理排除项并补充回环地址。不会自动安装依赖或更新环境。

## 首次创建环境

```bash
conda env create -f environment.yml
conda activate tradingagent
python -m cli.main --help
```

环境已存在时更新依赖：

```bash
conda activate tradingagent
python -m pip install -e '.[dev]'
```

## 本地验证

```bash
conda activate tradingagent
NO_PROXY=localhost,127.0.0.1,::1 no_proxy=localhost,127.0.0.1,::1 python -m pytest -q
python -m ruff check .
python -m pip check
```

回环地址需绕过本机代理，否则 OAuth 本地回调测试可能失败。上述设置仅影响该测试进程。
默认测试不运行 integration；未安装可选 Bedrock 依赖时，对应测试会跳过。

WebUI 的 DOM 交互单测使用固定数据，不连接浏览器、账户或模型：先执行 `rtk npm ci --prefix webui`，再执行 `rtk proxy env TRADINGAGENTS_TEST_PYTHON=/path/to/tradingagent/bin/python npm --prefix webui test`。它覆盖向导校验、前后导航、模型选择、回测预估及各页面的信息展示，不能替代浏览器视觉验收。

## 控制台

不调用 CLI 即可完成登录、选模型、思考强度、分析和报告查看。只在本机回环地址启动：

```bash
./tradingagents.sh web
```

浏览器打开 `http://127.0.0.1:8765`。启动分析按「分析对象、研究范围、模型选择、确认启动」四步设置；运行状态、报告、回测和账户优先展示结果与可执行操作，技术详情按需展开。CLI 对照可在帮助中查看。订阅令牌和 API Key 不会回显。进程启动仍然不等于分析完成。

### 同时运行与排队

- 控制台最多同时运行 2 个分析，最多再排队 5 个。可用环境变量 `TRADINGAGENTS_WEBUI_MAX_RUNS`（1–8）和 `TRADINGAGENTS_WEBUI_MAX_QUEUE`（0–20），或控制台配置 `~/.tradingagents/webui/config.json` 里的 `max_concurrent_runs` / `max_queued_runs` 调整；环境变量优先。
- 运行位已满时，新的分析进入「排队中」（0/11，等待开始），按提交顺序在有任务结束后自动开始；有排队时，新的分析也排在队尾，不会插队。队列也满时启动被拒绝（「队列已满」），表单保持不变。
- 排队中的任务可以在查看状态里「取消」：它没有启动，不会调用模型，也不会写断点。停止、重新启动（含从断点重新启动）和取消排队都必须指定任务 id（`POST /api/runs/stop|restart|cancel {"id": "…"}`）：不带 id、id 为空、只有空格或不是文字的请求一律拒绝（400），什么都不做，不会再默认作用于最新的任务。WebUI 的这些按钮总是带上当前任务的 id。
- 排队中的「从断点重新启动」任务，如果它要用的断点在排队期间被清除了，轮到它时不会从头开始，而是标记为失败并说明原因。
- 回测仍然独占：有分析在运行或排队时不能启动回测，回测运行时新的分析排队。
- 每个任务的记录在 `~/.tradingagents/webui/runs/<任务 id>.json`（原子写入，密钥已脱敏）；旧的 `current-run.json` 会在第一次读取时迁移进来，原文件改名为 `current-run.json.migrated-<时间戳>`（每次迁移都用新名字，不会覆盖以前的备份）。
- 断点按任务隔离：`<数据缓存>/checkpoints/runs/<任务 id>/<标的>.db`。同一标的同时跑两个任务互不影响。旧版本留下的 `checkpoints/<标的>.db` 仍可读：新任务从它复制起点（只读，不改原文件），完成后清掉旧库里对应的那条记录。
- 控制台重启：仍在运行的分析进程继续运行并占用运行位；进程已不在的任务：结果文件已写出的算结束，否则标记为失败（有断点时可「从断点重新启动」）；还没启动完成的任务标记为失败；记录的进程号已被其他程序占用（启动时间对不上）的任务标记为失败、释放运行位，控制台不会向那个程序发送任何信号；读不到进程启动时间（/proc 和 ps 都失败）时无法确认，任务仍算运行中并继续占用运行位，不标记失败，「停止任务」和「重新启动」会拒绝（409）且不发送任何信号；排队的任务按原顺序保留，并在有空位时自动开始。同一个控制台目录只应运行一个控制台。

## 交互分析

```bash
conda activate tradingagent
python -m cli.main
```

按提示选择标的、分析日期、分析师和模型供应商。真实分析需要对应供应商凭据，或已经运行且兼容的本地模型服务。
密钥只通过本机环境变量或被 Git 忽略的 `.env` 配置，不写入文档或 AWR 台账。

也可以使用账号订阅：先运行 `tradingagents login chatgpt` 或 `tradingagents login grok`，再在模型供应商菜单选择 Subscription。订阅无需填写 API Key，详见 [订阅接入说明](SUBSCRIPTIONS.md)。

命令行分析是一次性任务；WebUI 可通过 `./tradingagents.sh web` 单独启动。只有分析流程完成并生成报告，才算完成真实分析验证。

不激活环境也可以执行：

```bash
conda run --no-capture-output -n tradingagent python -m cli.main
```
