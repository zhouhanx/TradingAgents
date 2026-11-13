# 控制台 API 备注

## 分析框架 id（WEBUI-008，W68，PM 已裁定接受）

- `POST /api/runs`、`/api/runs/validate` 的 `framework` 字段：先去掉首尾空白再转小写后匹配已注册的 id。
  例如 `"TradingAgents"`、`" tradingagents "`、`"tradingagents\n"` 都会启动 TradingAgents。
- 字段缺失、为 `null`、空串或纯空白：按默认框架 `tradingagents` 处理。
- 其余未注册的值或非文字值：返回中文 400，不写记录、job、日志或队列。
- 任务文件（`<id>.job.json`）里只会写解析后的 id。worker 按它从注册表选择运行函数（W66）；
  没有 `framework` 的旧任务按 TradingAgents 运行；其他未注册或格式不对的值直接失败，写出中文失败原因，不启动分析。

## 任务详情 `GET /api/runs?id=<id>`（W65）

- 带了 `id` 但找不到这个任务（或 id 格式不对）：仍返回 200 和任务列表，但 `run` 与 `selected` 为 `null`，
  并带 `"not_found": true` 和 `"error": "没有这个任务。"`。不再回落到最新的任务。
- 不带 `id`（或为空）：照旧显示最新的任务。

## 同一目录只能有一个控制台（W40）

- 控制台启动时对 `<TRADINGAGENTS_WEBUI_HOME>/console.lock` 加独占锁。已有控制台持有时，第二个控制台打印中文原因并以退出码 1 结束，
  不做恢复，也不重新拉起排队中的任务。持锁进程退出后锁由系统自动释放。

## 回测占用运行名额（RUN-003b，W44，仅后端）

- 运行中的回测只占 1 个运行名额（以前占满全部名额）。回测运行时，分析仍可在剩余名额里启动或排队。
- `POST /api/backtests`：有空闲名额且队列为空时立即启动；名额已满或已有任务在排队时返回 409，中文原因
  「运行名额已满或有任务在排队……回测没有启动。回测不排队……」，不写记录或任务文件。
  以前只要有任何任务在运行或排队就拒绝，现在只在没有空闲名额（或要插队）时拒绝。
- 回测暂不进入队列：页面还没有回测的「排队中」提示（启动后固定显示「回测已启动」）。让回测排队需要改页面，留到 UI 批次。
- 回测和分析一样使用控制台的单次调用时限与总时限（`timeout` / `llm_call_budget` 留空时的默认值），并默认开启断点
  （页面或 `TRADINGAGENTS_CHECKPOINT_ENABLED` 可关闭）。带 `run_id` 的回测断点放在 `checkpoints/runs/backtest-<run_id>/`，
  用同一 `run_id` 再次启动时能从中断的那一格续跑；不带 `run_id` 的放在本次任务 id 下。

## 结果目录（W24）

- 设置了 `TRADINGAGENTS_WEBUI_HOME`、没有设置 `TRADINGAGENTS_RESULTS_DIR`、页面也没有指定结果目录时，结果写到
  `<TRADINGAGENTS_WEBUI_HOME>/results`。`TRADINGAGENTS_RESULTS_DIR` 仍优先；两个变量都没设时不变（`~/.tradingagents/logs`）。

## 断点自动清理（W45）

- 控制台启动时、之后队列调度时最多每 10 分钟一次，按以下规则清理本目录任务记录对应的 `checkpoints/runs/<scope>/`：
  - 已完成的任务：删除；
  - 失败 / 已停止 / 已取消 / 中断的任务：结束 7 天后删除（7 天内仍可「从断点重新启动」）；没有结束时间的不删；
  - 运行中、排队中的任务自己的断点，以及排队或运行中的续跑要复制的来源断点：不删；
  - 本目录没有记录的断点目录不动（缓存目录可能与另一个控制台或命令行共用），用「立即清除断点」清除。

## RUN-003c（B3）

- **W43 任务列表上限**：`GET /api/runs` 的 `runs` 仍只列最近 30 个（运行中、排队中在前），但 `counts` 与排队位置按全部任务计算；新增 `listing: {shown, total, limit, truncated}`。页面在截断时显示「只显示最近 30 个任务（共 N 个）；上方计数包含全部任务。」
- **W56 成本价规则（保持宽松默认，已文档化）**：只有 `portfolio_source == "form"`（第 1 步「是否持仓」）要求每个持仓填写 `average_price`，与命令行 `--portfolio` 一致；导入的持仓 JSON 或不带 `portfolio_source` 的请求（WEBUI-006 之前的客户端只能导入 JSON）沿用旧规则：每个持仓有数量或成本价之一即可。这是有意保留的兼容行为（W50），不统一为严格规则。
- **W49 持仓错误**：持仓不可用时返回中文逐条说明（第 N 个持仓的哪个字段、应为何值），不再包含 pydantic 原文；保存与续跑抽屉在保存前按同样规则校验。
- **W51 现金**：`cash` 必须是 JSON 数字，拒绝 true/false、数字字符串、负数与非有限数（命令行 `--portfolio` 同样适用）。
- **W58**：读不到进程启动时间而拒绝停止/重启时（409），说明附带下一步操作。
- **W44（界面部分，选较小的安全方案）**：回测仍不排队；被拒时页面显示控制台返回的真实原因（错误样式、role=alert），表单提示「回测不排队」。未实现回测排队。
- **W67**：已注销框架的任务显示「未知工具（id）」。

## 费用用量与单价（V2-COST-STATS，V2-INPROGRESS-2）

- **用量文件**：控制台运行的分析结束时（完成或失败）在结果文件旁写 `<run>.usage.json`（`schema: cost-usage-1`），每次模型调用一行：`role`（图里的角色；子图分析师按外层节点归到具体分析师，不写成 `agent`）、`node`、`model`、`response_id`、`input_tokens`、`output_tokens`、`total_tokens`、`input_token_details`（如 `cache_read`）、`output_token_details`、`ended_at`；另有 `llm_calls`、`usage_reported`、`usage_unknown`（没有返回用量的调用数）。被「停止」的运行（SIGTERM）不写用量文件；命令行运行不写。
- **没有供应商合计**：文件里没有 `reported`，所以页面只显示合计、不显示各角色数字和金额（「没有供应商合计，不把调用合计当成已核对。」）。核对（REGRESSION-01）之后才写 `reported: {input_tokens, output_tokens}`，相等（容差 0）时显示各角色用量和金额。
- **单价**：`<TRADINGAGENTS_WEBUI_HOME>/cost-prices.json`，格式 `{"models": {"<模型 id>": {"input_per_million": "…", "output_per_million": "…", "currency": "USD"}}}`，模板 `webui/cost-prices.example.json`（空）。没有列出的模型显示「价格未知」。负数、NaN、无穷（含溢出为无穷的 `1e999`）的单价只让该模型显示「价格未知」，不计入合计（W234）。文件不是 UTF-8、不是 JSON（含 `Infinity`/`NaN` 字面量）或不是 `{"models": {…}}` 时，`/api/cost` 仍正常返回，所有单价按未知处理，`prices_unreadable: true`，页面显示「单价文件无法读取」和路径（W233）。页面不能编辑单价。`GET /api/cost` 新增 `prices_path`、`prices_models`（可用单价的模型数）、`prices_unreadable`，每个运行新增 `usage_unknown`。
- **V2-COST-PRICING**：
  - 单价可选 `cache_read_per_million`（每百万缓存命中输入 token）。有它时，`input_token_details.cache_read` 按它计，其余输入按 `input_per_million` 计。没有它而这次调用有缓存命中时，全部输入按输入单价计，该角色旁注「缓存命中按输入单价计」。
  - DeepSeek 的缓存命中：langchain-openai 只把 `prompt_tokens_details.cached_tokens` 写进 `cache_read`（DeepSeek 现在两者都返回，数值相同）。只有 `prompt_cache_hit_tokens` 时，用量记录从原始 `token_usage` 补上 `cache_read`，并写 `cache_read_source: "prompt_cache_hit_tokens"`。已有 `cache_read` 时不再相加。
  - 单价只接受 JSON 数字或普通小数字符串（如 `"0.50"`；不接受 `"1_000"`、`"1e3"`、带符号或空格），范围 0–10000（每百万 token），否则该模型「价格未知」（W250）。
  - `currency` 去空格转大写后须为三个字母（USD、CNY…）。不写时为 USD；写了但不合法时该模型「价格未知」（W246）。
  - 一次运行里已计价的角色用了不止一种币种时，合计显示「币种不一致，不计入合计」，各角色仍各自显示金额和币种（W245）。页面没有跨运行的总计。
  - 单价文件可带 UTF-8 BOM（W247）。
