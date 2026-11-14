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
- **V2-COST-PRICING-2**：
  - 运行合计：各角色金额显示时仍各自舍入到 0.000001；合计用各角色未舍入的精确金额（Decimal）相加，最后只舍入一次（W251）。所以合计和页面上各角色金额之和最多相差 角色数 × 0.0000005（W257）。
  - 舍入规则：运行合计和各角色金额都用四舍六入五成双（银行家舍入，Decimal `ROUND_HALF_EVEN`），例如 0.0065485 → 0.006548（W256）。
  - `cache_read_per_million: null` 等同于没写：缓存命中按输入单价计，旁注「缓存命中按输入单价计」（W254）。
  - `cache_read`（`cached_tokens`）不是整数、为负数或大于 `input_tokens` 时，如果原始 `token_usage` 里有合法的 `prompt_cache_hit_tokens`（0 ≤ 命中数 ≤ 输入），用量记录改用它，只记一次，并写 `cache_read_source: "prompt_cache_hit_tokens:invalid_cache_read"`（W252）。只有 `prompt_cache_hit_tokens` 时仍写 `"prompt_cache_hit_tokens"`。
  - 回退之后仍不合法的缓存命中数按 0 个命中计价，即全部输入按 `input_per_million` 计（W254）。

## 自选提醒规则（V2-WATCHLIST）
- `GET /api/kline/latest`（日线路径）多给 `prev_close`、`prev_day`、`trading_day`：最新一根之前那根日线的收盘价和日期。加密货币用行情源的 UTC 日线，和日 K 线上前一根的收盘价相同，页面写「较昨收（北京 08:00）」；股票、贵金属写「较昨收」（R1）。没有前一根时为 `null`。
- 行情源限流（HTTP 429、`YFRateLimitError`、OKX `50011`）且没有别的源拿到价时，`/api/kline/latest` 回 HTTP 429 和 `{"error": "行情源限流，稍后重试。", "code": "rate_limited"}`；其他失败仍是 400、没有 `code`（R7）。
- `POST /api/watchlist/rules`：`{symbol, op: add|update|toggle|delete, kind: above|below|pct|win, value, rule_id, enabled}`。每个代码最多 5 条（`code: "rule_cap"`，「每个代码最多 5 条。」），重复规则 `code: "duplicate"`（R11）。
- `POST /api/watchlist/poll`：`{minutes: 0|1|5|15}`。没存过时，有启用的规则默认 5 分钟，没有就关（R2）。
- `POST /api/watchlist/refresh` 的 `quotes` 可以是数字，也可以是 `{price, prev_close, trading_day, fetched_at, delayed}`。只有规则命中才记价格触发（R4），回包多 `fired`。高于 / 低于命中一次，价格回到另一侧后重新生效；涨跌幅同一方向每个交易日最多一次；只有在规则生效之后取到的价才判断；同一份缓存回答只判断一次（R2）。
- 命中写进定时的提醒发件箱（`kind: "price"`，已发送）。`schedule.json` 新增 `local_events`：由价格提醒第一次建出来的文件不打开决定变化和运行完成提醒；打开定时页或新增定时才打开；没有这个字段的旧文件照旧（R5）。
- `GET /api/watchlist` 多 `unseen_count`、`trigger_total`、`poll_minutes`、`has_rules`；`visible_triggers` 是最近 20 条加已检查的，最多存 500 条（R6）。`POST /api/watchlist/seen` 把未看数清零，各标签页一致（R8）。
- 截图（14 个状态 × 1440/390）：`/workspace/ta-sync/watchlist-logs/shots/`，脚本在 `/workspace/ta-sync/watchlist-logs/harness/`。

## 自选打磨（V2-WATCHLIST-POLISH）
- **W261**：`watchlist.json` 不是 UTF-8、不是 JSON（含截断、空文件）、不是对象，或 `items`/`triggers`/`admissions`/`checks` 不是列表时，不再按空列表读。原文件复制为同目录的 `watchlist.json.corrupt-<北京时间 YYYYMMDD-HHMMSS>`（同样的坏内容只备份一次），所有 `/api/watchlist*` 读写回 HTTP 409 和 `{"error": "自选数据文件 watchlist.json 已损坏，没有按空列表读取，也不会写入。原文件已备份为 …。请修复或移走 watchlist.json 后刷新页面。", "code": "watchlist_corrupt"}`，文件不被覆盖。读不到文件（权限）时同样 409、同一个 `code`，不备份。页面显示这句话，不显示表格，加入、刷新、自动刷新都停用，状态写「已停止：自选数据文件损坏」。修好或移走文件后刷新页面即恢复。`schedule.json` 仍是旧行为（坏文件按空读），这批没有改。
- **W263**：触发记录总数最多 500 条，已检查的也算在内。超出时先删最旧的未检查记录（最近 20 条除外，即页面上那份列表），还超就删最旧的记录，不论是否已检查。被删触发的检查和放行记录保留。（W265 裁定后的删法见文末。）
- **W262**：每条触发记录有只增不减的序号 `seq`（文件里 `trigger_seq` 记最大值，删记录不回退）；未看按序号算：规则触发 `seq > alerts_seen_seq` 即未看，不再比较到秒的时间。`POST /api/watchlist/seen` 接受 `{"upto_seq": n}`：页面把它显示过的最大序号发上来，服务端取 `min(n, trigger_seq)`，且只增不减；不带 `upto_seq` 时等于全部已看（旧行为）；不是不小于 0 的整数时回 400「upto_seq 须为不小于 0 的整数。」。`GET /api/watchlist` 多 `trigger_seq`、`alerts_seen_seq`，每条触发多 `seq`。旧文件按表内顺序编号，原来的 `alerts_seen_at` 换算成不晚于它的规则触发里的最大序号。
- **W259**：高于 / 低于的阈值须为有限正数，且不超过 1e12，超过时 400「价格不能超过 1 万亿（1e12）。」（`code: "bad_price"`，页面先在本地拦下）。规则的 `label`、`chip`、触发文字和预览只显示页面精度：价格 ≥1000 一位小数、≥1 两位、<1 四位有效数字；涨跌幅最多两位小数（3.14159 → 3.14）。舍入和页面 `toFixed` 一致（按二进制精确值，五入）。保存的 `value` 仍是输入的原值，判断按原值。原来「舍掉会丢数字时保留输入的位数」的做法取消。
- **W260**：`op: "update"` 带的 `kind` 和规则本身的类型不同时回 400「规则类型不能修改，请删除后重新添加。」（`code: "kind_locked"`），规则不变。不带 `kind` 时按规则自己的类型。页面编辑时会带上 `kind`。
- **W-WL1**：自动刷新状态行（上次 / 上次成功 / 下次 / 自动重试）和表格里的「取价 北京」在那个时间不是北京今天时，前面加月-日，例如「上次 北京 11-13 22:35:04」。
- **W-WL2**：触发列表按新到旧排列；同一秒记下的几条按自选表格的行顺序排（同一代码按记录先后）。`visible_triggers` 由服务端这样排好，已检查而保留的旧触发仍在最后；「查看全部」也一样。
- **W-WL3**：宽度 ≤700px 时每条触发同一个版式：代码和文字在前（长文字照常换行），时间单独一行，状态在右上角。
- 截图（pre-open 带日期、同一秒触发、390 长短行、W261 损坏状态，各 1440/390，另有局部特写）：`/workspace/ta-sync/watchlist-polish-logs/shots/`，改动前对照在 `shots-before/`，脚本在 `/workspace/ta-sync/watchlist-polish-logs/harness/`。
- **W259 裁定（取代上面 W259 的「只显示页面精度」）**：规则数值按输入原样显示，不舍入：`1452.35` → 「高于 1452.35」，`1452.30` → 「高于 1452.30」。价格位数少于页面格式时只在后面补 0（`190` → 「高于 190.00」，`0.5` → 「0.5000」），和改动前已有的标签一致。涨跌幅同样原样（`3.14159` → 「涨跌 ≥ 3.14159%」，`3.0` → 「3.0%」；JSON 数字 `3` → 「3%」）。小数超过 6 位回 400「小数最多 6 位。」（`code: "bad_decimals"`），不保存，页面先在本地拦下。仍须有限、大于 0，价格不超过 1e12。规则另存 `value_text`（整理过的输入：去掉首尾空格、前导 0 和 `+`，`.5` → `0.5`，`1e3` → `1000`），`GET /api/watchlist` 的规则多 `value_text`，重新读取后标签不变；页面添加和修改时把输入原文作为 `value` 发出（字符串），仍接受数字。只改写法（`1452.3` → `1452.30`）只更新标签，不重新生效。没有 `value_text` 的旧规则按数值本身的位数显示。行情价格仍按页面格式。
- **W264 裁定（第 7 个提交，属于 W261 范围）**：读文件时逐行校验。`items`/`triggers`/`checks`/`admissions` 里任一行不是对象，`items` 里有条目缺 `symbol`（或不是非空字符串），触发的 `id`/`symbol`、检查和放行记录的 `trigger_id` 有值但不是字符串，或某个条目 `rules` 列表里有一行不是对象、缺字符串 `id`、`arm_seq` 不是非负整数时，整个文件按 W261 当损坏处理：备份一次，所有 `/api/watchlist*`（含页面后台自动刷新）回 409 `watchlist_corrupt`，页面锁住，原文件一个字节不动，不再有 500。坏行不跳过，因为跳过后再写回会把它丢掉。`rules` 整个不是列表时的处理见下面第 8 个提交。
- **W265 裁定（PM 修订）**：取代 W263 的删法。总数仍最多 500 条，超出时分三档删：①最旧的、没有被检查或放行记录引用的已查看触发；②还超就删最旧的、没被引用的未查看触发；③还超才按时间删最旧的被引用触发（不论是否已查看）。已查看/未查看按页面算未看的口径：只有规则触发会是未看（`seq > alerts_seen_seq`），其余都算已查看。最新的 20 条一律不删。被删触发的检查和放行记录照旧保留为历史。
- **W266 裁定**：`watchlist.json` 开头的 UTF-8 BOM 读的时候去掉，不算损坏，和单价文件（W247）一样；只读不改写文件，下次写回时不带 BOM。BOM 后面的内容坏了仍按 W261/W264 处理。
- **W267 裁定**：`POST /api/watchlist/seen` 的 `upto_seq` 只接受非负整数。`true`/`false`、小数（含 `0.0`、`1.0`）、字符串（含 `"0"`）一律 400「upto_seq 须为不小于 0 的整数。」，不写文件。手工改出重复 `seq` 后重新编号进 backlog，这次不做。
- **原样字符串**：页面添加规则和修改规则时，`value` 都是输入框里的原文（只去掉首尾空格）作为字符串发出，不是 `Number(value)`；`1452.30` 发的就是 `"1452.30"`，服务器存 `value_text: "1452.30"`。node 测试覆盖添加和修改两条路径。
- **第 8 个提交：数值只认半角十进制（W269/W270-1/W272，取代 W259 裁定里 `.5` → `0.5`、`1e3` → `1000` 的写法）**：服务器先去掉首尾空白和一个前导 `+`，剩下的必须是 ASCII `[0-9]+(\.[0-9]{1,6})?`。全角数字（`１２３`）、阿拉伯-印度等其他文字的数字（`٣.٥`）、`_`、千分位逗号、`.5`、`5.`、科学计数法（`1e-7`、`1E3`、`2e12`）一律回 400「只能填半角数字和小数点，例如 1452.30。」（`code: "bad_number"`）。小数超过 6 位仍是 `bad_decimals`「小数最多 6 位。」；空、`abc`、`inf`、负数、0 仍是原来的 `bad_price`/`bad_pct`。`value_text` 是去掉前导 0 后的输入（`0190.50` → `190.50`），`value` 由 `Decimal(value_text)` 算出；大于 0、价格不超过 1e12、涨跌幅 0.1 到 50 都按 Decimal 判断，所以 `1000000000000.000001` 超限，`1000000000000.000000` 可以。JSON 数字按 Python 的写法转成文本再按同一规则判断（`190` → `"190"`，`1e-07` 被拒）。页面抽屉用一模一样的规则先在本地拦下，提示句和服务器一字不差（node 测试把同一批输入分别交给页面和服务器 `_rule_value` 逐条比对），`1e-7` 不再出现「1e-7.0」预览。
- **W268 重复规则**：同一类型、并且两个阈值作为十进制数完全相等才算重复：`Decimal(新输入的 value_text) == Decimal(已有规则显示的数)`。所以 `1452.3`、`1452.30`、`01452.3` 是同一条，`1452.300001` 不是 `1452.3`；不再有浮点容差（以前是 `math.isclose` 1e-9）。已有规则的数取它显示的文本（下一条的规则），没有 `value_text` 的旧规则取数值本身的写法。修改时同样判断：数值相等只改写法，不重新生效。
- **W270-3 显示的数就是比较用的数**：读取时如果规则的 `value_text` 和 `value` 对不上，就不用 `value_text`，按 `value` 显示（标签、chip、`value_text` 字段、触发文本、重复判断都一样）。对不上的判断：`float(Decimal(value_text)) != value`。用浮点相等而不是 `Decimal(value_text) != Decimal(str(value))`，是因为浮点只保留约 17 位有效数字，`999999999999.123456` 这样合法输入的文本存成浮点后 `str` 会变成 `999999999999.1235`，按字面比会被误判成对不上；而浮点相等正好表示「这段文本就是这个比较值的来源」。`1452.30` 对 `1452.3` 算对得上；`1452.31` 对 `1452.3` 对不上，显示 `1452.3`。
- **`rules` 不是列表算损坏**：条目里有 `rules` 键、但值不是列表（`"bad"`、对象、`null`）时，和 W264 一样整个文件当损坏：备份一次，409 `watchlist_corrupt`，原文件不动。没有 `rules` 键的旧文件仍按没有规则读。V2-WATCHLIST 原来「`rules: "bad"` 按没有规则读」的测试已改成这个行为。
- W270-2、W271 进 backlog，这次没做。
