"""CLI surface, mapped to the Chinese console.

The lists here are the contract the WebUI must cover. Tests compare them with
the launcher help text and the Typer command signatures so a new CLI flag cannot
silently stay CLI-only.
"""

from __future__ import annotations

from cli.prompts import ANALYST_CHOICES
from cli.selections import DATE_PROMPT
from tradingagents.llm_clients.factory import REASONING_EFFORT_PROVIDERS

# Launcher commands in cli/launcher.py HELP, plus the console entry itself.
LAUNCHER_COMMANDS = (
    {
        "cli": "start",
        "zh": "启动分析",
        "summary": "按当前步骤启动一次分析。前台可看进度；后台只表示进程已启动，不表示分析完成。",
    },
    {
        "cli": "stop",
        "zh": "停止",
        "summary": "停止本控制台或启动脚本管理的分析进程。",
    },
    {
        "cli": "restart",
        "zh": "重新启动",
        "summary": "停止当前分析，并用上一次或新的参数再启动。",
    },
    {
        "cli": "status",
        "zh": "查看状态",
        "summary": "查看分析进程是否仍在运行。未运行不是环境错误。",
    },
    {
        "cli": "logs",
        "zh": "查看日志",
        "summary": "查看分析过程日志。对应 CLI 的 logs。",
    },
    {
        "cli": "login",
        "zh": "登录订阅",
        "summary": "登录 ChatGPT 或 Grok 订阅。不填写 API Key。",
    },
    {
        "cli": "logout",
        "zh": "退出登录",
        "summary": "退出指定订阅并清除本机凭据。",
    },
    {
        "cli": "auth-status",
        "zh": "登录状态",
        "summary": "只读取本机登录材料，不证明额度，也不联网刷新。",
    },
    {
        "cli": "cli",
        "zh": "原始命令",
        "summary": "CLI 的原始透传。WebUI 不调用它；同等操作在对应页面完成。",
    },
    {
        "cli": "web",
        "zh": "打开控制台",
        "summary": "启动本页面。之后的操作都在页面内完成。",
    },
    {
        "cli": "backtest",
        "zh": "回测",
        "summary": "tradingagents 子命令，不是启动脚本命令。启动脚本入口是 cli backtest。本页完成同等操作，不调用启动脚本。",
    },
)

ANALYZE_FLAGS = (
    {"cli": "--ticker", "zh": "标的", "field": "ticker"},
    {"cli": "--date", "zh": "分析日期", "field": "date"},
    {"cli": "--analysts", "zh": "分析师", "field": "analysts"},
    {"cli": "--save / --no-save", "zh": "保存报告", "field": "save"},
    {"cli": "--show / --no-show", "zh": "显示完整报告", "field": "show"},
    {"cli": "--html / --no-html", "zh": "HTML 报告", "field": "html"},
    {"cli": "--checkpoint / --no-checkpoint", "zh": "断点续跑", "field": "checkpoint"},
    {"cli": "--clear-checkpoints", "zh": "清除断点", "field": "clear_checkpoints"},
    {"cli": "--portfolio", "zh": "持仓", "field": "portfolio"},
    {"cli": "--background", "zh": "后台运行", "field": "background"},
)

BACKTEST_FLAGS = (
    {"cli": "tickers", "zh": "标的列表", "field": "tickers"},
    {"cli": "--start", "zh": "开始日期", "field": "start"},
    {"cli": "--end", "zh": "结束日期", "field": "end"},
    {"cli": "--every", "zh": "间隔天数", "field": "every"},
    {"cli": "--analysts", "zh": "分析师", "field": "analysts"},
    {"cli": "--asset-type", "zh": "资产类型", "field": "asset_type"},
    {"cli": "--portfolio", "zh": "持仓", "field": "portfolio"},
    {"cli": "--run-id", "zh": "继续上次回测", "field": "run_id"},
)

# Same order and box titles as cli/selections.py.
SELECTION_STEPS = (
    {
        "step": 1, "cli": "ticker", "zh": "标的", "flag": "--ticker",
        "title": "Step 1: Ticker Symbol",
        "body": "Enter the ticker, with exchange suffix when needed (e.g. SPY, 0700.HK, BTC-USD)",
    },
    {
        "step": 2, "cli": "analysis date", "zh": "分析日期", "flag": "--date",
        "title": "Step 2: Analysis Date",
        "body": DATE_PROMPT,
    },
    {
        "step": 3, "cli": "output language", "zh": "输出语言", "flag": "TRADINGAGENTS_OUTPUT_LANGUAGE",
        "title": "Step 3: Output Language",
        "body": "Select the language for analyst reports and final decision",
    },
    {
        "step": 4, "cli": "analysts", "zh": "分析师", "flag": "--analysts",
        "title": "Step 4: Analysts Team",
        "body": "Select your LLM analyst agents for the analysis",
    },
    {
        "step": 5, "cli": "research depth", "zh": "研究深度", "flag": "TRADINGAGENTS_MAX_DEBATE_ROUNDS",
        "title": "Step 5: Research Depth",
        "body": "Select your research depth level",
    },
    {
        "step": 6, "cli": "LLM provider", "zh": "模型供应商", "flag": "TRADINGAGENTS_LLM_PROVIDER",
        "title": "Step 6: LLM Provider",
        "body": "Select your LLM provider",
    },
    {
        "step": 7, "cli": "thinking agents", "zh": "快模型与深模型", "flag": "TRADINGAGENTS_QUICK_THINK_LLM",
        "title": "Step 7: Thinking Agents",
        "body": "Select your thinking agents for analysis",
    },
    {
        "step": 8, "cli": "reasoning effort", "zh": "思考强度", "flag": "TRADINGAGENTS_OPENAI_REASONING_EFFORT",
        "title": "Step 8: Reasoning Effort",
        "body": "Configure OpenAI reasoning effort level",
    },
)

# Step 8's box changes with the provider. Grok has no Step 8 prompt.
def _step_eight() -> dict[str, dict[str, str]]:
    from cli.prompts import EFFORT_LEVEL_PROMPT, REASONING_EFFORT_PROMPT, THINKING_MODE_PROMPT

    return {
        "openai": {
            "title": "Step 8: Reasoning Effort",
            "body": "Configure OpenAI reasoning effort level",
            "prompt": REASONING_EFFORT_PROMPT,
        },
        "google": {
            "title": "Step 8: Thinking Mode",
            "body": "Configure Gemini thinking mode",
            "prompt": THINKING_MODE_PROMPT,
        },
        "anthropic": {
            "title": "Step 8: Effort Level",
            "body": "Configure Claude effort level",
            "prompt": EFFORT_LEVEL_PROMPT,
        },
    }


STEP_EIGHT = _step_eight()

LANGUAGE_ZH = {
    "Chinese": "中文",
    "English": "英文",
    "Japanese": "日文",
    "Korean": "韩文",
    "Hindi": "印地文",
    "Spanish": "西班牙文",
    "Portuguese": "葡萄牙文",
    "French": "法文",
    "German": "德文",
    "Arabic": "阿拉伯文",
    "Russian": "俄文",
    "custom": "自定义",
}
LANGUAGE_PAGE_ORDER = (
    "Chinese", "English", "Japanese", "Korean", "Hindi", "Spanish",
    "Portuguese", "French", "German", "Arabic", "Russian", "custom",
)


def _language_rows() -> tuple[dict, ...]:
    from cli.prompts import OUTPUT_LANGUAGE_CHOICES

    by_value = {value: label for label, value in OUTPUT_LANGUAGE_CHOICES}
    if set(by_value) != set(LANGUAGE_PAGE_ORDER):
        raise KeyError("language choices drifted from the CLI")
    return tuple(
        {"value": value, "zh": LANGUAGE_ZH[value], "cli": by_value[value]}
        for value in LANGUAGE_PAGE_ORDER
    )


LANGUAGES = _language_rows()

DEPTH_ZH = {
    1: ("浅层", "1 轮辩论。第一次真实分析建议用这个。"),
    3: ("中等", "3 轮辩论。"),
    5: ("深入", "5 轮辩论。这是轮数，不是模型思考强度。"),
}


def _depth_rows() -> tuple[dict, ...]:
    from cli.prompts import DEPTH_OPTIONS, RESEARCH_DEPTH_PROMPT

    rows = []
    for label, value in DEPTH_OPTIONS:
        if value not in DEPTH_ZH:
            raise KeyError(value)
        zh, detail = DEPTH_ZH[value]
        rows.append({
            "value": value,
            "zh": zh,
            "prompt": RESEARCH_DEPTH_PROMPT,
            "cli": label,
            "detail": detail,
        })
    return tuple(rows)


DEPTHS = _depth_rows()


def _selection_prompts() -> dict[str, str]:
    from cli.prompts import (
        ANALYST_CHECKBOX_INSTRUCTION,
        ANALYST_TEAM_PROMPT,
        LLM_PROVIDER_PROMPT,
        RESEARCH_DEPTH_PROMPT,
        SUBSCRIPTION_SELECT_PROMPT,
        subscription_menu_label,
    )

    return {
        "analyst": ANALYST_TEAM_PROMPT,
        "analyst_instruction": ANALYST_CHECKBOX_INSTRUCTION,
        "research_depth": RESEARCH_DEPTH_PROMPT,
        "provider": LLM_PROVIDER_PROMPT,
        "subscription": SUBSCRIPTION_SELECT_PROMPT,
        "subscription_choice": subscription_menu_label(),
    }


_SELECTION = _selection_prompts()
ANALYST_PROMPT = _SELECTION["analyst"]
ANALYST_INSTRUCTION = _SELECTION["analyst_instruction"]
PROVIDER_PROMPT = _SELECTION["provider"]
SUBSCRIPTION_PROMPT = _SELECTION["subscription"]
SUBSCRIPTION_CHOICE = _SELECTION["subscription_choice"]


def login_paste_notes() -> dict[str, str]:
    """The paste sentence belongs to ChatGPT only. Grok never prints it."""
    return {
        "chatgpt": "ChatGPT 登录正在等待。只有这时命令行才会打印粘贴句：",
        "grok": "Grok 登录不询问粘贴地址，也不打印 ChatGPT 的粘贴句。请看登录日志里的设备码。",
        "idle": "当前没有等待粘贴的 ChatGPT 登录。这句只在 ChatGPT 登录时打印，Grok 不打印：",
    }


def login_sentences() -> tuple[str, ...]:
    return tuple(item["cli"] for item in login_rows())


def login_rows() -> list[dict]:
    """Login sentences the CLI prints, Chinese first.

    The Grok device line is a slot template. It is not printed until the
    verification address and code are known.
    """
    from tradingagents.llm_clients.chatgpt_auth import (
        CHATGPT_LOGIN_PROMPT,
        CHATGPT_PASTE_PROMPT,
        CHATGPT_PASTE_REJECTED,
    )
    from tradingagents.llm_clients.grok_auth import GROK_DEVICE_PROMPT, GROK_LOGIN_PROMPT

    return [
        {"zh": "ChatGPT 登录", "cli": CHATGPT_LOGIN_PROMPT, "slot": False},
        {"zh": "ChatGPT 粘贴地址", "cli": CHATGPT_PASTE_PROMPT, "slot": False},
        {"zh": "ChatGPT 粘贴无效", "cli": CHATGPT_PASTE_REJECTED, "slot": False},
        {"zh": "Grok 登录", "cli": GROK_LOGIN_PROMPT, "slot": False},
        {"zh": "Grok 设备码", "cli": GROK_DEVICE_PROMPT, "slot": True},
    ]


LOGIN_SENTENCES = login_sentences()

ANALYST_ZH = {
    "market": "市场分析师",
    "social": "情绪分析师",
    "news": "新闻分析师",
    "fundamentals": "基本面分析师",
}

DATA_ENV = (
    {
        "env": "FRED_API_KEY",
        "zh": "FRED 宏观密钥",
        "masked": True,
        "cli": "FRED_API_KEY",
        "note": "宏观供应商 fred 需要。这是数据密钥，不是模型订阅。",
    },
    {
        "env": "ALPHA_VANTAGE_API_KEY",
        "zh": "Alpha Vantage 密钥",
        "masked": True,
        "cli": "ALPHA_VANTAGE_API_KEY",
        "note": "选择 alpha_vantage 时需要。",
    },
    {
        "env": "SEC_EDGAR_USER_AGENT",
        "zh": "SEC 联系标识",
        "masked": False,
        "cli": "SEC_EDGAR_USER_AGENT",
        "note": "读取财报时发给 SEC 的姓名和联系地址。不是密钥。留空则使用项目占位地址。",
    },
)

CLOUD_ENV = (
    {
        "env": "AZURE_OPENAI_ENDPOINT",
        "zh": "Azure 终结点",
        "masked": False,
        "cli": "AZURE_OPENAI_ENDPOINT",
        "note": "例如 https://资源名.openai.azure.com/。页面的推理地址也会传给 Azure。",
    },
    {
        "env": "AZURE_OPENAI_DEPLOYMENT_NAME",
        "zh": "Azure 部署名",
        "masked": False,
        "cli": "AZURE_OPENAI_DEPLOYMENT_NAME",
        "note": "未设置时，Azure 客户端使用所选模型 ID。",
    },
    {
        "env": "OPENAI_API_VERSION",
        "zh": "Azure 接口版本",
        "masked": False,
        "cli": "OPENAI_API_VERSION",
        "note": "Azure OpenAI 需要，例如 2025-03-01-preview。",
    },
    {
        "env": "AWS_REGION",
        "zh": "Bedrock 优先区域",
        "masked": False,
        "cli": "AWS_REGION",
        "note": "Bedrock 客户端先读这个变量。设置后会覆盖 AWS_DEFAULT_REGION。",
    },
    {
        "env": "AWS_DEFAULT_REGION",
        "zh": "Bedrock 区域",
        "masked": False,
        "cli": "AWS_DEFAULT_REGION",
        "note": "客户端先读 AWS_REGION，没有时才读这个变量。",
    },
    {
        "env": "AWS_PROFILE",
        "zh": "AWS 配置档",
        "masked": False,
        "cli": "AWS_PROFILE",
        "note": "使用本机 AWS 凭证链时的配置档。不是访问密钥。",
    },
    {
        "env": "AWS_BEARER_TOKEN_BEDROCK",
        "zh": "Bedrock 令牌",
        "masked": True,
        "cli": "AWS_BEARER_TOKEN_BEDROCK",
        "note": "Bedrock API 密钥。不回显。没有它时使用 AWS 凭证链。",
    },
)

EFFORT_ZH = {
    ("openai", "medium"): "中",
    ("openai", "high"): "高",
    ("openai", "low"): "低",
    ("google", "high"): "开启思考",
    ("google", "minimal"): "最低思考",
    ("anthropic", "high"): "高",
    ("anthropic", "medium"): "中",
    ("anthropic", "low"): "低",
}


def _effort_rows(family: str, choices) -> tuple[dict, ...]:
    rows = []
    for label, value in choices:
        key = (family, value)
        if key not in EFFORT_ZH:
            raise KeyError(key)
        rows.append({"value": value, "zh": EFFORT_ZH[key], "cli": label})
    return tuple(rows)


def _reasoning() -> dict[str, tuple[dict, ...]]:
    from cli.prompts import ANTHROPIC_EFFORT_CHOICES, GEMINI_THINKING_CHOICES, OPENAI_EFFORT_CHOICES

    return {
        "openai": _effort_rows("openai", OPENAI_EFFORT_CHOICES),
        "google": _effort_rows("google", GEMINI_THINKING_CHOICES),
        "anthropic": _effort_rows("anthropic", ANTHROPIC_EFFORT_CHOICES),
    }


REASONING = _reasoning()

# Chinese first. The CLI display name stays in the counterpart field.
PROVIDER_ZH = {
    "openai": "OpenAI",
    "google": "Google",
    "anthropic": "Anthropic",
    "chatgpt": "ChatGPT 订阅",
    "grok": "Grok 订阅",
    "xai": "xAI",
    "deepseek": "DeepSeek",
    "qwen": "通义千问",
    "glm": "智谱 GLM",
    "minimax": "MiniMax",
    "openrouter": "OpenRouter",
    "mistral": "Mistral",
    "kimi": "Kimi",
    "groq": "Groq",
    "nvidia": "NVIDIA NIM",
    "azure": "Azure OpenAI",
    "bedrock": "Amazon Bedrock",
    "ollama": "Ollama",
    "openai_compatible": "兼容端点",
}


# Official simple-icons marks already vendored. Missing brands stay text-only;
# a letter tile is not an official icon.
PROVIDER_ICONS = {
    "openai": "openai.svg",
    "chatgpt": "openai.svg",
    "google": "google.svg",
    "anthropic": "anthropic.svg",
    "deepseek": "deepseek.svg",
    "qwen": "qwen.svg",
    "qwen-cn": "qwen.svg",
    "mistral": "mistralai.svg",
    "nvidia": "nvidia.svg",
    "azure": "microsoftazure.svg",
    "bedrock": "amazonaws.svg",
    "ollama": "ollama.svg",
    "openrouter": "openrouter.svg",
    "kimi": "kimi.svg",
    # Z.AI mark from simple-icons (same source as the other vendored marks).
    # BigModel China, Grok, xAI and Groq still have no permitted file here.
    "glm": "zdotai.svg",
    # MiniMax mark, simple-icons 16.35.0 icons/minimax.svg, bytes unchanged.
    # MiniMax confirmed that file matches its design guidelines.
    "minimax": "minimax.svg",
    "minimax-cn": "minimax.svg",
}

REGION_ZH = {
    "qwen": "国际站",
    "qwen-cn": "中国站",
    "glm": "Z.AI 国际",
    "glm-cn": "BigModel 中国",
    "minimax": "国际",
    "minimax-cn": "中国",
}


def _region_rows(prompt: str, choices) -> tuple[dict, ...]:
    rows = []
    for label, (value, url) in choices:
        if value not in REGION_ZH:
            raise KeyError(value)
        rows.append({
            "value": value,
            "zh": REGION_ZH[value],
            "prompt": prompt,
            "cli": label,
            "url": url,
        })
    return tuple(rows)


def _regions() -> dict[str, tuple[dict, ...]]:
    from cli.prompts import (
        GLM_REGION_CHOICES,
        GLM_REGION_PROMPT,
        MINIMAX_REGION_CHOICES,
        MINIMAX_REGION_PROMPT,
        QWEN_REGION_CHOICES,
        QWEN_REGION_PROMPT,
    )

    return {
        "qwen": _region_rows(QWEN_REGION_PROMPT, QWEN_REGION_CHOICES),
        "glm": _region_rows(GLM_REGION_PROMPT, GLM_REGION_CHOICES),
        "minimax": _region_rows(MINIMAX_REGION_PROMPT, MINIMAX_REGION_CHOICES),
    }


REGIONS = _regions()


VENDOR_ZH = {
    "core_stock_apis": "行情",
    "technical_indicators": "技术指标",
    "fundamental_data": "基本面",
    "news_data": "新闻",
    "macro_data": "宏观",
    "prediction_markets": "预测市场",
}

TOOL_ZH = {
    "get_stock_data": "行情",
    "get_indicators": "技术指标",
    "get_fundamentals": "公司概况",
    "get_balance_sheet": "资产负债表",
    "get_cashflow": "现金流量表",
    "get_income_statement": "利润表",
    "get_news": "个股新闻",
    "get_global_news": "宏观新闻",
    "get_insider_transactions": "内部人交易",
    "get_macro_indicators": "宏观指标",
    "get_prediction_markets": "预测市场",
}


def vendor_options() -> list[dict]:
    """Category chains the router actually accepts. Not a CLI menu."""
    from tradingagents.dataflows.router import OPTIONAL_CATEGORIES, TOOLS_CATEGORIES, VENDOR_METHODS
    from tradingagents.default_config import DEFAULT_CONFIG

    defaults = DEFAULT_CONFIG.get("data_vendors") or {}
    rows = []
    for category, info in TOOLS_CATEGORIES.items():
        vendors: list[str] = []
        for method in info["tools"]:
            for name in VENDOR_METHODS.get(method, {}):
                if name not in vendors:
                    vendors.append(name)
        rows.append({
            "category": category,
            "zh": VENDOR_ZH.get(category, info["description"]),
            "cli": f"data_vendors.{category}",
            "vendors": vendors,
            "default": defaults.get(category, "default"),
            "optional": category in OPTIONAL_CATEGORIES,
        })
    return rows


def tool_options() -> list[dict]:
    """Tool chains that override a category. Not a CLI menu."""
    from tradingagents.dataflows.router import VENDOR_METHODS

    return [
        {
            "method": method,
            "zh": TOOL_ZH.get(method, method),
            "cli": f"tool_vendors.{method}",
            "vendors": list(vendors),
        }
        for method, vendors in VENDOR_METHODS.items()
    ]


def analyst_options() -> list[dict]:
    return [
        {
            "value": analyst.value,
            "zh": ANALYST_ZH[analyst.value],
            "cli": label,
        }
        for label, analyst in ANALYST_CHOICES
    ]


def reasoning_family(provider: str) -> str | None:
    key = (provider or "").lower()
    if key in REASONING_EFFORT_PROVIDERS:
        return "openai"
    if key == "google":
        return "google"
    if key == "anthropic":
        return "anthropic"
    return None


ROLE_SPLIT = {
    "quick_zh": "快模型调用数据工具，并撰写分析师、研究员、辩论者、交易员和复盘文本。它不是数据源。",
    "deep_zh": "深模型只负责研究经理和组合经理。",
    "data_zh": "行情、财报、新闻、宏观和预测市场来自 data_vendors，不是 Grok 搜索。",
    "cli": "TRADINGAGENTS_QUICK_THINK_PROVIDER / TRADINGAGENTS_DEEP_THINK_PROVIDER",
}


COMMAND_PAGES = {
    "start": "analyze",
    "stop": "runs",
    "restart": "runs",
    "status": "runs",
    "logs": "reports",
    "login": "accounts",
    "logout": "accounts",
    "auth-status": "accounts",
    "cli": "analyze",
    "web": "analyze",
    "backtest": "backtest",
}


def search_entries() -> list[dict]:
    """CLI commands and flags the top search can open. Pages match the shell nav."""
    rows = []
    for item in LAUNCHER_COMMANDS:
        rows.append({
            "zh": item["zh"],
            "cli": item["cli"],
            "summary": item["summary"],
            "page": COMMAND_PAGES[item["cli"]],
            "kind": "command",
        })
    for item in ANALYZE_FLAGS:
        rows.append({"zh": item["zh"], "cli": item["cli"], "page": "analyze", "kind": "flag"})
    for item in BACKTEST_FLAGS:
        rows.append({"zh": item["zh"], "cli": item["cli"], "page": "backtest", "kind": "flag"})
    for item in SELECTION_STEPS:
        rows.append({"zh": item["zh"], "cli": item["title"], "page": "analyze", "kind": "step"})
    for choices in REASONING.values():
        for choice in choices:
            rows.append({"zh": choice["zh"], "cli": choice["cli"], "page": "analyze", "kind": "reasoning"})
    for item in DATA_ENV:
        rows.append({"zh": item["zh"], "cli": item["cli"], "page": "accounts", "kind": "env"})
    for item in CLOUD_ENV:
        rows.append({"zh": item["zh"], "cli": item["cli"], "page": "accounts", "kind": "env"})
    rows.append({"zh": "保存与续跑", "cli": "trading_memory.md", "page": "analyze", "kind": "page"})
    rows.append({"zh": "模型高级设置", "cli": "TRADINGAGENTS_TEMPERATURE", "page": "models", "kind": "page"})
    rows.append({"zh": "数据来源", "cli": "data_vendors", "page": "accounts", "kind": "page"})
    for item in provider_rows():
        rows.append({"zh": item["zh"], "cli": item["cli"], "summary": item["key"], "page": "models", "kind": "provider"})
    return rows


def provider_rows() -> list[dict]:
    """Every CLI provider, Chinese first, with the menu display name unchanged."""
    from cli.prompts import _llm_provider_table

    rows = []
    for display, key, url in _llm_provider_table():
        if key not in PROVIDER_ZH:
            raise KeyError(key)
        rows.append({"key": key, "zh": PROVIDER_ZH[key], "cli": display, "url": url})
    return rows


def question_defaults(now=None) -> dict[str, str]:
    """The Default lines the CLI question boxes print for steps 1 and 2."""
    from cli.selections import (
        analysis_date_question_default,
        question_default_line,
        ticker_question_default,
    )

    return {
        "1": question_default_line(ticker_question_default()),
        "2": question_default_line(analysis_date_question_default(now)),
    }


def _annotation_help(param) -> str | None:
    from typing import get_args, get_origin

    help_text = getattr(param.default, "help", None)
    if isinstance(help_text, str) and help_text.strip():
        return " ".join(help_text.split())
    if get_origin(param.annotation) is not None and str(get_origin(param.annotation)).endswith("Annotated"):
        for meta in get_args(param.annotation)[1:]:
            text = getattr(meta, "help", None)
            if isinstance(text, str) and text.strip():
                return " ".join(text.split())
    return None


def _parameter_help(func) -> dict[str, str]:
    """Typer help strings the CLI --help actually prints, keyed by parameter name."""
    import inspect

    found = {}
    for name, param in inspect.signature(func).parameters.items():
        help_text = _annotation_help(param)
        if help_text:
            found[name] = help_text
    return found


def _launcher_line(prefix: str) -> str | None:
    """One exact line from the launcher help. None when that line is absent."""
    from cli.launcher import HELP

    for raw in HELP.splitlines():
        text = raw.strip()
        if text.startswith(prefix):
            return text
    return None


def _launcher_command_line(command: str) -> str | None:
    """The launcher help line for one command. Combined login lines stay combined."""
    if command in {"login", "logout", "auth-status"}:
        return _launcher_line("login/logout/auth-status")
    return _launcher_line(command + " ")


def launcher_preamble() -> str:
    """The launcher help text above the command list, unchanged."""
    from cli.launcher import HELP

    head, _sep, _rest = HELP.partition("commands:")
    return head.rstrip()


def flags_with_cli_help(flags, helps: dict[str, str]) -> list[dict]:
    rows = []
    for item in flags:
        row = dict(item)
        text = helps.get(item["field"])
        if text:
            row["help"] = text
        rows.append(row)
    return rows


def _commands_with_help() -> list[dict]:
    import inspect

    from cli.main import auth_status, backtest, login, logout

    docs = {
        "backtest": inspect.getdoc(backtest) or "",
        "login": inspect.getdoc(login) or "",
        "logout": inspect.getdoc(logout) or "",
        "auth-status": inspect.getdoc(auth_status) or "",
    }
    rows = []
    for item in LAUNCHER_COMMANDS:
        row = dict(item)
        line = _launcher_command_line(item["cli"])
        if line:
            row["help"] = line
        if docs.get(item["cli"]):
            row["doc"] = docs[item["cli"]]
        rows.append(row)
    return rows


def subscription_commands() -> list[dict]:
    import inspect

    from cli.main import auth_status, login, logout

    specs = (
        ("login", "登录订阅", login),
        ("logout", "退出登录", logout),
        ("auth-status", "登录状态", auth_status),
    )
    rows = []
    for cli, zh, func in specs:
        help_text = _parameter_help(func).get("plan", "")
        rows.append({
            "cli": cli,
            "zh": zh,
            "doc": inspect.getdoc(func) or "",
            "help": help_text,
        })
    return rows


CONFIG_ENV_ZH = {
    "TRADINGAGENTS_LLM_PROVIDER": "主供应商",
    "TRADINGAGENTS_DEEP_THINK_LLM": "深模型",
    "TRADINGAGENTS_QUICK_THINK_LLM": "快模型",
    "TRADINGAGENTS_DEEP_THINK_PROVIDER": "深模型供应商",
    "TRADINGAGENTS_QUICK_THINK_PROVIDER": "快模型供应商",
    "TRADINGAGENTS_DEEP_THINK_BACKEND_URL": "深模型推理地址",
    "TRADINGAGENTS_QUICK_THINK_BACKEND_URL": "快模型推理地址",
    "TRADINGAGENTS_LLM_BACKEND_URL": "主推理地址",
    "TRADINGAGENTS_OUTPUT_LANGUAGE": "输出语言",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS": "研究辩论轮数",
    "TRADINGAGENTS_MAX_RISK_ROUNDS": "风险辩论轮数",
    "TRADINGAGENTS_MAX_TOOL_ROUNDS": "工具调用轮数",
    "TRADINGAGENTS_CHECKPOINT_ENABLED": "断点续跑",
    "TRADINGAGENTS_BENCHMARK_TICKER": "基准标的",
    "TRADINGAGENTS_TEMPERATURE": "采样温度",
    "TRADINGAGENTS_LLM_MAX_RETRIES": "模型重试次数",
    "TRADINGAGENTS_MAX_TOKENS": "输出长度上限",
    "TRADINGAGENTS_GOOGLE_THINKING_LEVEL": "Gemini 思考强度",
    "TRADINGAGENTS_OPENAI_REASONING_EFFORT": "OpenAI 思考强度",
    "TRADINGAGENTS_ANTHROPIC_EFFORT": "Claude 思考强度",
    "TRADINGAGENTS_RESULTS_DIR": "结果目录",
    "TRADINGAGENTS_CACHE_DIR": "缓存目录",
    "TRADINGAGENTS_MEMORY_LOG_PATH": "记忆日志",
}


def config_env_rows() -> list[dict]:
    """Every user config env var the CLI folds into a run, Chinese first.

    ``field`` is the config key, so the page can show the env name without
    hardcoding it.
    """
    from tradingagents.default_config import _ENV_OVERRIDES

    extra = {
        "TRADINGAGENTS_RESULTS_DIR": "results_dir",
        "TRADINGAGENTS_CACHE_DIR": "data_cache_dir",
        "TRADINGAGENTS_MEMORY_LOG_PATH": "memory_log_path",
    }
    mapping = {**_ENV_OVERRIDES, **extra}
    rows = []
    for name in list(_ENV_OVERRIDES) + list(extra):
        if name not in CONFIG_ENV_ZH:
            raise KeyError(name)
        rows.append({"cli": name, "zh": CONFIG_ENV_ZH[name], "field": mapping[name]})
    return rows


def config_key_rows() -> list[dict]:
    """Config keys the CLI reads that are not environment variables.

    Defaults come from DEFAULT_CONFIG. ``timeout`` is not a CLI setting; the
    console applies its own bound when the page leaves it empty.
    """
    from tradingagents.default_config import DEFAULT_CONFIG
    from webui.service import CONSOLE_CALL_BUDGET, CONSOLE_MODEL_TIMEOUT

    specs = (
        ("news_article_limit", "个股新闻条数", "留空沿用命令行默认。", True),
        ("global_news_article_limit", "宏观新闻条数", "留空沿用命令行默认。", True),
        ("global_news_lookback_days", "宏观回看天数", "留空沿用命令行默认。", True),
        ("holding_period_days", "持有天数", "留空沿用命令行默认。", True),
        ("memory_log_max_entries", "记忆条数上限", "留空不轮换。", True),
        ("global_news_queries", "宏观新闻查询", "一行一条。留空沿用内置查询。", False),
    )
    rows = []
    for field, zh, note, limit in specs:
        default = DEFAULT_CONFIG[field]
        if isinstance(default, list):
            placeholder = str(default[0]) if default else ""
        elif default is None:
            placeholder = "留空"
        else:
            placeholder = f"留空沿用 {default}"
        rows.append({
            "field": field,
            "zh": zh,
            "cli": field,
            "note": note,
            "default": default,
            "placeholder": placeholder,
            "limit": limit,
            "env": False,
        })
    rows.append({
        "field": "timeout",
        "zh": "模型调用超时",
        "cli": "timeout",
        "note": f"留空时控制台使用 {CONSOLE_MODEL_TIMEOUT} 秒（流式读取的最长静默；连接 15 秒、写入 60 秒另计）。命令行不设这一项时使用供应商默认。",
        "default": CONSOLE_MODEL_TIMEOUT,
        "placeholder": f"留空使用 {CONSOLE_MODEL_TIMEOUT}",
        "limit": False,
        "env": False,
        "console_only": True,
    })
    rows.append({
        "field": "llm_call_budget",
        "zh": "单次调用总时限",
        "cli": "llm_call_budget",
        "note": (f"一次模型调用连同重试和等待最多用这么多秒，留空时控制台使用 {CONSOLE_CALL_BUDGET} 秒。"
                 "只重试超时、连接错误、5xx 和 429，不重试 400 和登录失效；重试次数见 llm_max_retries，订阅默认 3 次。"),
        "default": CONSOLE_CALL_BUDGET,
        "placeholder": f"留空使用 {CONSOLE_CALL_BUDGET}",
        "limit": False,
        "env": False,
        "console_only": True,
    })
    return rows


def report_sections() -> list[dict]:
    """The five --show headings, in the order the CLI prints them."""
    from tradingagents.reporting import _SECTIONS

    zh = {
        "I. Analyst Team Reports": "一、分析师团队报告",
        "II. Research Team Decision": "二、研究团队决定",
        "III. Trading Team Plan": "三、交易团队计划",
        "IV. Risk Management Team Decision": "四、风险管理决定",
        "V. Portfolio Manager Decision": "五、组合经理决定",
    }
    rows = []
    for heading, _folder, _agents in _SECTIONS:
        if heading not in zh:
            raise KeyError(heading)
        rows.append({"cli": heading, "zh": zh[heading]})
    return rows



LIVE_REPORT_TITLE_ZH = {
    "Market Analysis": "市场分析",
    "Social Sentiment": "社交情绪",
    "News Analysis": "新闻分析",
    "Fundamentals Analysis": "基本面分析",
    "Research Team Decision": "研究团队决定",
    "Trading Team Plan": "交易团队计划",
    "Portfolio Management Decision": "组合管理决定",
    "Bull Researcher Analysis": "多头研究员分析",
    "Bear Researcher Analysis": "空头研究员分析",
    "Research Manager Decision": "研究经理决定",
    "Aggressive Analyst Analysis": "激进分析师分析",
    "Conservative Analyst Analysis": "保守分析师分析",
    "Neutral Analyst Analysis": "中性分析师分析",
    "Portfolio Manager Decision": "组合经理决定",
}


def live_report_titles() -> list[dict]:
    """Panel titles, then streamed headings that are not already panel titles."""
    from cli.display import LIVE_REPORT_SECTION_TITLES, STREAM_REPORT_HEADINGS

    rows = []
    seen = set()
    for key, title in LIVE_REPORT_SECTION_TITLES.items():
        if title not in LIVE_REPORT_TITLE_ZH:
            raise KeyError(title)
        rows.append({"key": key, "cli": title, "zh": LIVE_REPORT_TITLE_ZH[title], "kind": "panel"})
        seen.add(title)
    for key, title in STREAM_REPORT_HEADINGS:
        if title in seen:
            continue
        if title not in LIVE_REPORT_TITLE_ZH:
            raise KeyError(title)
        rows.append({"key": key, "cli": title, "zh": LIVE_REPORT_TITLE_ZH[title], "kind": "stream"})
    return rows



def progress_teams() -> list[dict]:
    """CLI Progress panel roles, Chinese first."""
    from cli.display import LIVE_PROGRESS_TEAMS
    from webui.progress import AGENT_ZH, TEAM_ZH

    rows = []
    for team, agents in LIVE_PROGRESS_TEAMS:
        for agent, analyst in agents:
            rows.append({
                "team": team,
                "team_zh": TEAM_ZH[team],
                "agent": agent,
                "zh": AGENT_ZH[agent],
                "analyst": analyst,
            })
    return rows



def show_agents() -> list[dict]:
    """Agents printed by --show, in saved-report order, not progress-panel order."""
    from tradingagents.reporting import _SECTIONS
    from webui.progress import AGENT_ZH

    section_zh = {item["cli"]: item["zh"] for item in report_sections()}
    rows = []
    for heading, folder, agents in _SECTIONS:
        if heading not in section_zh:
            raise KeyError(heading)
        for agent, filename, _path in agents:
            if agent not in AGENT_ZH:
                raise KeyError(agent)
            rows.append({
                "heading": heading,
                "heading_zh": section_zh[heading],
                "agent": agent,
                "zh": AGENT_ZH[agent],
                "file": filename,
                "folder": folder,
            })
    return rows


def wall_time_pending_sentence() -> str:
    """The CLI summary printed when no analyst clock has stopped."""
    from cli.display import AnalystWallTimeTracker
    from tradingagents.graph.analyst_execution import build_analyst_execution_plan

    return AnalystWallTimeTracker(build_analyst_execution_plan(["market"])).format_summary()





def picker_prompts() -> dict[str, str]:
    """Model, URL, and menu sentences taken from the CLI prompt constants."""
    from cli.prompts import (
        AZURE_DEPLOYMENT_REQUIRED,
        CUSTOM_MODEL_CHOICE,
        MENU_NAVIGATION_INSTRUCTION,
        MODEL_ID_PROMPT,
        MODEL_ID_REQUIRED,
        OPENAI_COMPATIBLE_URL_PROMPT,
        OPENROUTER_ID_PROMPT,
        URL_SCHEME_REQUIRED,
        model_picker_title,
    )

    return {
        "quick_engine": model_picker_title("openai", "quick"),
        "deep_engine": model_picker_title("openai", "deep"),
        "azure_quick": model_picker_title("azure", "quick"),
        "azure_deep": model_picker_title("azure", "deep"),
        "azure_required": AZURE_DEPLOYMENT_REQUIRED,
        "model_id": MODEL_ID_PROMPT,
        "custom_choice": CUSTOM_MODEL_CHOICE,
        "model_id_required": MODEL_ID_REQUIRED,
        "openrouter_id": OPENROUTER_ID_PROMPT,
        "compatible_url": OPENAI_COMPATIBLE_URL_PROMPT,
        "url_scheme": URL_SCHEME_REQUIRED,
        "menu_navigation": MENU_NAVIGATION_INSTRUCTION,
    }



def key_prompts() -> dict[str, str]:
    """API-key sentences, with placeholders the page fills."""
    from cli.prompts import (
        missing_key_sentence,
        paste_key_prompt,
        saved_key_sentence,
        skipped_key_sentence,
        unset_key_sentence,
    )

    return {
        "paste": paste_key_prompt("{env_var}"),
        "unset": unset_key_sentence("{env_var}"),
        "missing": missing_key_sentence("{env_var}"),
        "skipped": skipped_key_sentence("{env_var}"),
        "saved": saved_key_sentence("{env_var}", "{env_path}"),
    }


def report_prompts() -> dict[str, str]:
    """Post-run questions and the save lines the CLI prints."""
    from cli.run import (
        ANALYSIS_COMPLETE,
        BROWSER_FAILED_PREFIX,
        COMPLETE_REPORT_PREFIX,
        HEADLESS_SETUP_PREFIX,
        HTML_PROMPT,
        HTML_REPORT_PREFIX,
        OPEN_BROWSER_PROMPT,
        REPORT_SAVED_PREFIX,
        REVIEW_SENTENCE,
        SAVE_ERROR_PREFIX,
        SAVE_PATH_PROMPT,
        SAVE_REPORT_PROMPT,
        SHOW_PROMPT,
    )

    return {
        "save": SAVE_REPORT_PROMPT,
        "save_path": SAVE_PATH_PROMPT,
        "html": HTML_PROMPT,
        "show": SHOW_PROMPT,
        "open": OPEN_BROWSER_PROMPT,
        "open_failed": BROWSER_FAILED_PREFIX,
        "saved_to": REPORT_SAVED_PREFIX,
        "complete_report": COMPLETE_REPORT_PREFIX,
        "html_report": HTML_REPORT_PREFIX,
        "save_error": SAVE_ERROR_PREFIX,
        "complete": ANALYSIS_COMPLETE,
        "review": REVIEW_SENTENCE,
        "headless": HEADLESS_SETUP_PREFIX,
    }


def _portfolio_shape() -> str:
    """Field names from the portfolio model. No invented cash amount or currency."""
    import json

    from tradingagents.portfolio import PortfolioContext, Position

    position = dict.fromkeys(Position.model_fields)
    book = dict.fromkeys(PortfolioContext.model_fields)
    book["positions"] = [position]
    return json.dumps(book, ensure_ascii=False)


def form_defaults() -> dict:
    """Initial analysis form values from the CLI config, not a chosen subscription."""
    from cli.prompts import DEPTH_OPTIONS, OPENAI_EFFORT_CHOICES
    from tradingagents.default_config import DEFAULT_CONFIG

    provider = DEFAULT_CONFIG["llm_provider"]
    return {
        "llm_provider": provider,
        "quick_think_provider": DEFAULT_CONFIG["quick_think_provider"] or provider,
        "deep_think_provider": DEFAULT_CONFIG["deep_think_provider"] or provider,
        "quick_think_llm": DEFAULT_CONFIG["quick_think_llm"],
        "deep_think_llm": DEFAULT_CONFIG["deep_think_llm"],
        "openai_reasoning_effort": OPENAI_EFFORT_CHOICES[0][1],
        "research_depth": DEPTH_OPTIONS[0][1],
    }


def placeholders() -> dict[str, str]:
    """Input hints taken from CLI examples and defaults, not invented rows."""
    import inspect

    from cli.display import LIVE_REPORT_SECTION_TITLES
    from cli.main import backtest
    from cli.prompts import (
        OPENAI_COMPATIBLE_URL_PROMPT,
        OPENROUTER_ID_PROMPT,
        TICKER_PROMPT_DEFAULT,
    )
    from tradingagents.llm_clients.chatgpt_auth import CALLBACK_PATH, DEFAULT_PORT

    def example_after(prompt: str) -> str:
        marker = "e.g. "
        start = prompt.find(marker)
        if start < 0:
            return ""
        rest = prompt[start + len(marker):]
        for stop in (" for ", ",", ")"):
            if stop in rest:
                rest = rest.split(stop, 1)[0]
        return rest.strip().rstrip(":")

    every = inspect.signature(backtest).parameters["every"].default
    every_default = getattr(every, "default", every)
    section = next(iter(LIVE_REPORT_SECTION_TITLES))
    return {
        "ticker": TICKER_PROMPT_DEFAULT,
        "openrouter_id": example_after(OPENROUTER_ID_PROMPT),
        "compatible_url": example_after(OPENAI_COMPATIBLE_URL_PROMPT),
        "section_file": f"{section}.md",
        "login_paste": f"http://127.0.0.1:{DEFAULT_PORT}{CALLBACK_PATH}?",
        "every": str(every_default),
        "portfolio": _portfolio_shape(),
    }


def setup_prompts() -> dict[str, str]:
    """Visible setup prompts, taken from the CLI question text."""
    from cli.prompts import (
        CUSTOM_LANGUAGE_INVALID,
        CUSTOM_LANGUAGE_PROMPT,
        TICKER_INVALID,
        TICKER_PROMPT,
    )
    from cli.selections import DATE_PROMPT

    return {
        "ticker": TICKER_PROMPT,
        "ticker_invalid": TICKER_INVALID,
        "date": DATE_PROMPT,
        "language": "Select Output Language:",
        "custom_language": CUSTOM_LANGUAGE_PROMPT,
        "custom_language_invalid": CUSTOM_LANGUAGE_INVALID,
    }



def outcome_sentences() -> dict[str, str]:
    """Backtest, sign-in, and checkpoint sentences the console emits."""
    from cli.main import (
        CHATGPT_SIGNOUT_UNCONFIRMED,
        NO_TICKER_SENTENCE,
        auth_status_absent_sentence,
        auth_status_error_sentence,
        auth_status_signed_in_sentence,
        backtest_progress_line,
        checkpoint_cleared_sentence,
        continue_sweep_sentence,
        failed_cell_sentence,
        local_signout_unconfirmed,
        not_signed_in_sentence,
        ran_cells_sentence,
        signed_in_sentence,
        signed_out_sentence,
        unsettled_cell_sentence,
    )

    return {
        "no_ticker": NO_TICKER_SENTENCE,
        "progress": backtest_progress_line("{done}", "{total}", "{ticker}", "{date}"),
        "cleared": checkpoint_cleared_sentence("{count}"),
        "ran": ran_cells_sentence("{cells_run}", "{skipped}", "{log_path}"),
        "continue_sweep": continue_sweep_sentence("{run_id}"),
        "failed": failed_cell_sentence("{ticker}", "{date}", "{reason}"),
        "unsettled": unsettled_cell_sentence("{ticker}", "{reason}"),
        "signed_in": signed_in_sentence("{label}", "{email}"),
        "signed_out": signed_out_sentence("{label}"),
        "chatgpt_signout_unconfirmed": CHATGPT_SIGNOUT_UNCONFIRMED,
        "local_signout_unconfirmed": local_signout_unconfirmed("{label}"),
        "not_signed_in": not_signed_in_sentence("{label}"),
        "auth_error": auth_status_error_sentence("{label}", "{error}"),
        "auth_signed_in": auth_status_signed_in_sentence("{label}", "{account}"),
        "auth_absent": auth_status_absent_sentence("{label}"),
    }





def guard_sentences() -> dict[str, str]:
    """Exit and file sentences the console includes in its errors."""
    from cli.prompts import ANALYST_REQUIRED, NO_ANALYSTS_EXIT, NO_ENDPOINT_EXIT
    from cli.selections import tier_model_sentence
    from tradingagents.portfolio import portfolio_unusable_sentence

    return {
        "analyst_required": ANALYST_REQUIRED,
        "no_analysts": NO_ANALYSTS_EXIT,
        "no_endpoint": NO_ENDPOINT_EXIT,
        "tier_model": tier_model_sentence("{tier}", "{provider}", "{variable}"),
        "portfolio": portfolio_unusable_sentence("<path>", "{error}"),
    }


def validation_sentences() -> dict[str, str]:
    """Parser errors the console returns, taken from the CLI functions."""
    from cli.main import every_type_error
    from cli.models import AssetType
    from cli.prompts import (
        ANALYST_NAME_REQUIRED,
        DATE_RETRY_PREFIX,
        date_format_error,
        future_date_error,
        ticker_symbol_error,
        unknown_analyst_error,
    )

    example = "bond"
    try:
        AssetType(example)
    except ValueError as exc:
        asset_example = str(exc)
    else:
        raise RuntimeError("bond must stay outside AssetType")
    return {
        "asset_type_example": example,
        "ticker": ticker_symbol_error("{value}"),
        "date": date_format_error("{value}"),
        "future": future_date_error("{value}"),
        "date_retry_prefix": DATE_RETRY_PREFIX,
        "unknown_analyst": unknown_analyst_error(["{name}"], "market, sentiment, news, fundamentals"),
        "analyst_name_required": ANALYST_NAME_REQUIRED,
        "every": every_type_error("{value}"),
        "asset_type": asset_example,
    }


def confirmation_sentences() -> dict[str, str]:
    """Confirmation and refusal sentences the console emits."""
    from cli.prompts import unavailable_analyst_sentence
    from cli.selections import (
        DATE_FLAG_PREFIX,
        SELECTED_ANALYSTS_PREFIX,
        TICKER_FLAG_PREFIX,
        ticker_flag_sentence,
    )
    from tradingagents.llm_clients.openai_client import chatgpt_plan_refusal

    return {
        "ticker_flag": ticker_flag_sentence("{ticker}"),
        "ticker_flag_prefix": TICKER_FLAG_PREFIX,
        "date_flag_prefix": DATE_FLAG_PREFIX,
        "selected_analysts_prefix": SELECTED_ANALYSTS_PREFIX,
        "unavailable_crypto": unavailable_analyst_sentence(["fundamentals"], "crypto"),
        "refuse_temperature": chatgpt_plan_refusal("temperature"),
        "refuse_max_tokens": chatgpt_plan_refusal("max_tokens"),
    }




def _footer_elapsed_prefix() -> str:
    """The stopwatch prefix format_footer_stats appends. Not a current duration."""
    from cli.display import MessageBuffer, format_footer_stats

    piece = format_footer_stats(MessageBuffer(), start_time=0, now=0).rsplit(" | ", 1)[-1]
    return piece.split(" ", 1)[0]


def display_sentences() -> dict[str, str]:
    """Live-view titles the CLI panel actually prints."""
    from cli.display import (
        AGENTS_STAT,
        COMPLETE_REPORT_RULE,
        CURRENT_REPORT_TITLE,
        LIVE_CREDIT,
        LIVE_WELCOME_BODY,
        LLM_STAT,
        MESSAGE_COLUMNS,
        MESSAGE_LIMIT,
        MESSAGES_TITLE,
        PROGRESS_COLUMNS,
        PROGRESS_TITLE,
        REPORTS_STAT,
        TOKENS_EMPTY,
        TOKENS_STAT,
        TOKENS_UNKNOWN,
        TOOLS_STAT,
        WAITING_REPORT,
        WELCOME_TITLE,
    )
    from cli.launcher import LOG_PREFIX
    from cli.updates import WHATS_NEW_TITLE
    from tradingagents.backtest import (
        NO_DIRECTION_CLAIMED,
        PENDING_CELLS_SENTENCE,
        alpha_measure_sentence,
        resolved_cells_line,
    )
    from tradingagents.reporting import (
        ANALYSIS_DATE_LABEL,
        DATA_VENDORS_LABEL,
        GENERATED_LABEL,
        MEMORY_LOG_LABEL,
        RATING_LABEL,
    )

    return {
        "welcome_title": WELCOME_TITLE,
        "welcome_cli": LIVE_WELCOME_BODY,
        "live_credit": LIVE_CREDIT,
        "progress": PROGRESS_TITLE,
        "progress_columns": " / ".join(PROGRESS_COLUMNS),
        "messages": MESSAGES_TITLE,
        "message_columns": " / ".join(MESSAGE_COLUMNS),
        "message_limit": str(MESSAGE_LIMIT),
        "current_report": CURRENT_REPORT_TITLE,
        "waiting_report": WAITING_REPORT,
        "complete_report": COMPLETE_REPORT_RULE,
        "resolved_cells": resolved_cells_line("{resolved}", "{pending}"),
        "no_direction": NO_DIRECTION_CLAIMED,
        "pending_cells": PENDING_CELLS_SENTENCE,
        "alpha": alpha_measure_sentence("{holding}"),
        "agents": AGENTS_STAT,
        "llm": LLM_STAT,
        "tools": TOOLS_STAT,
        "tokens": TOKENS_STAT,
        "tokens_empty": TOKENS_EMPTY,
        "tokens_unknown": TOKENS_UNKNOWN,
        "elapsed_prefix": _footer_elapsed_prefix(),
        "reports": REPORTS_STAT,
        "log_prefix": LOG_PREFIX,
        "whats_new": WHATS_NEW_TITLE,
        "analysis_date": ANALYSIS_DATE_LABEL,
        "rating": RATING_LABEL,
        "generated": GENERATED_LABEL,
        "data_vendors": DATA_VENDORS_LABEL,
        "memory_log": MEMORY_LOG_LABEL,
    }


def log_sentences() -> dict[str, str]:
    """Log and memory tokens the writers actually emit.

    The stale-runtime launcher sentence is not included. This console does not
    query that supervisor, so the page must not present that sentence as its own.
    """
    from cli.launcher import NOT_RUNNING
    from cli.run import (
        MESSAGE_TOOL_LOG_NAME,
        TOOL_CALL_LABEL,
        message_tool_message_line,
        selected_ticker_message,
    )
    from tradingagents.memory.log import (
        DECISION_MARKER,
        PENDING_STATUS,
        REFLECTION_MARKER,
        RESOLVED_PREFIX,
    )
    from tradingagents.memory.reflection import ALPHA_VS_LABEL

    return {
        "not_running": NOT_RUNNING,
        "message_tool_log": MESSAGE_TOOL_LOG_NAME,
        "tool_call": TOOL_CALL_LABEL,
        "message_row": message_tool_message_line("{timestamp}", "{type}", "{content}").rstrip("\n"),
        "tool_row": f"{{timestamp}} [{TOOL_CALL_LABEL}] {{name}}({{args}})",
        "selected_ticker": selected_ticker_message("{ticker}"),
        "decision": DECISION_MARKER,
        "reflection": REFLECTION_MARKER,
        "pending": PENDING_STATUS,
        "resolved": RESOLVED_PREFIX,
        "alpha_vs": ALPHA_VS_LABEL,
        "alpha_vs_line": f"{ALPHA_VS_LABEL} {{benchmark}}: {{alpha}}",
    }


MESSAGE_PANEL_TYPES = (
    ("User", "用户"),
    ("Agent", "代理"),
    ("Data", "数据"),
    ("Control", "控制"),
    ("System", "系统"),
    ("Tool", "工具"),
)


def progress_statuses() -> list[dict]:
    """Status words the CLI progress panel prints, Chinese first."""
    from webui.progress import STATUS_ZH

    order = ("pending", "in_progress", "completed", "error")
    missing = [key for key in order if key not in STATUS_ZH]
    if missing or set(order) != set(STATUS_ZH):
        raise KeyError(missing or sorted(set(STATUS_ZH) - set(order)))
    return [{"cli": key, "zh": STATUS_ZH[key]} for key in order]


def message_panel_types() -> list[dict]:
    """Types the CLI message panel prints, Chinese first.

    User, Agent, Data and Control come from classify_message_type. System is
    added with add_message. Tool is the tool-call row in the same panel.
    """
    return [{"cli": cli, "zh": zh} for cli, zh in MESSAGE_PANEL_TYPES]



def backtest_route() -> dict[str, str]:
    """How backtest is actually invoked, and the launcher rejection if it is not."""
    from cli.launcher import unknown_command_sentence

    return {
        "typer": "backtest",
        "launcher": "cli backtest",
        "rejected_command": "backtest",
        "rejected": unknown_command_sentence("backtest"),
    }




_SHORT_OPTION_ERRORS: list[dict] | None = None


def subscription_short_option_errors() -> list[dict]:
    """``-h`` is not help for the subscription commands. Each command has its own sentence."""
    global _SHORT_OPTION_ERRORS
    if _SHORT_OPTION_ERRORS is not None:
        return _SHORT_OPTION_ERRORS
    from typer.testing import CliRunner

    from cli.main import app

    runner = CliRunner()
    rows = []
    for name in ("login", "logout", "auth-status"):
        result = runner.invoke(app, [name, "-h"], prog_name="python -m cli.main")
        if result.exit_code != 2 or not result.stderr:
            raise RuntimeError(f"{name} -h did not produce the expected error")
        rows.append({
            "key": f"{name}_short_help",
            "zh": f"{name} 的 -h",
            "when": (
                f"只有 {name} -h 才把这句打印到标准错误，退出码 2。"
                f"{name} --help 打印的是帮助，不是这句。别的无效选项不套用 -h。"
            ),
            "cli": result.stderr,
        })
    _SHORT_OPTION_ERRORS = rows
    return rows


def _inherited_lock_nope_sentence() -> str:
    """The stderr body for TRADINGAGENTS_LAUNCHER_LOCK_FD=nope. Not every illegal value."""
    from cli.launcher import invalid_inherited_lock_sentence

    try:
        int("nope")
    except ValueError as exc:
        return invalid_inherited_lock_sentence(exc)
    raise RuntimeError("nope was accepted as a lock descriptor")


def cancel_exit_rows() -> list[dict]:
    """CLI cancel sentences. The console has no cancel and must not print them."""
    from cli.prompts import (
        CANCELLED_EXIT,
        NO_ANALYSTS_EXIT,
        NO_DEPTH_EXIT,
        NO_ENDPOINT_EXIT,
        NO_MODEL_EXIT,
        NO_PROVIDER_EXIT,
        NO_SUBSCRIPTION_EXIT,
        NO_TICKER_EXIT,
        no_thinking_engine_exit,
    )

    return [
        {
            "key": "ticker",
            "zh": "取消标的提问",
            "when": "只有标的提问被取消、返回 None 时打印并退出。空白输入不是这句。控制台没有取消，不打印这句。",
            "cli": NO_TICKER_EXIT,
        },
        {
            "key": "analysts",
            "zh": "分析师复选框结束且没有选择",
            "when": "只有复选框结束且没有选择时打印并退出。空确认的校验句不是这句。控制台空确认用校验句。",
            "cli": NO_ANALYSTS_EXIT,
        },
        {
            "key": "depth",
            "zh": "取消研究深度",
            "when": "只有研究深度菜单被取消时打印并退出。控制台没有取消，不打印这句。",
            "cli": NO_DEPTH_EXIT,
        },
        {
            "key": "provider",
            "zh": "取消供应商菜单",
            "when": "只有供应商菜单被取消时打印并退出。没进入菜单时不打印。控制台没有取消。",
            "cli": NO_PROVIDER_EXIT,
        },
        {
            "key": "subscription",
            "zh": "取消订阅第二步",
            "when": "只有已经进入订阅第二步并且那一步被取消时打印。没选订阅菜单项时不打印。",
            "cli": NO_SUBSCRIPTION_EXIT,
        },
        {
            "key": "model",
            "zh": "取消实时模型列表",
            "when": "只有订阅或 OpenRouter 的模型列表被取消时打印。空手填 ID 用校验句，不是这句。",
            "cli": NO_MODEL_EXIT,
        },
        {
            "key": "quick",
            "zh": "取消快模型目录菜单",
            "when": "只有快模型的目录菜单被取消时打印。深模型不套用这句。",
            "cli": no_thinking_engine_exit("quick"),
        },
        {
            "key": "deep",
            "zh": "取消深模型目录菜单",
            "when": "只有深模型的目录菜单被取消时打印。快模型不套用这句。",
            "cli": no_thinking_engine_exit("deep"),
        },
        {
            "key": "cancelled",
            "zh": "取消必填文本",
            "when": "只有自定义模型 ID 或 Azure 部署名被取消时打印。自定义语言取消回到 English。地址取消不是这句。",
            "cli": CANCELLED_EXIT,
        },
        {
            "key": "endpoint",
            "zh": "取消兼容端点地址",
            "when": "只有 OpenAI-compatible 地址提问没有返回地址时打印。别的供应商不套用这句。",
            "cli": NO_ENDPOINT_EXIT,
        },
    ]


def launcher_conditional_rows() -> list[dict]:
    """Sentences the launcher prints only for a specific invocation."""
    from cli.launcher import (
        ALREADY_RUNNING,
        ALREADY_RUNNING_LOCK,
        LOGS_HELP,
        LOGS_LINES_INTEGER,
        LOGS_USAGE,
        MISSING_INHERITED_LOCK,
        STATUS_USAGE,
        STOP_USAGE,
        not_analysis_argument_sentence,
    )

    rows = [
        {
            "key": "logs_help",
            "zh": "日志帮助",
            "when": (
                "argv 里出现 --help 或 -h 就打印到标准输出，退出码 0，即使同时还有别的参数。"
                "没有这两个标志时，错误参数不打印这句。"
            ),
            "cli": LOGS_HELP,
        },
        {
            "key": "logs_lines_integer",
            "zh": "日志行数不是整数",
            "when": (
                "只有 logs --lines 后面缺少值，或那个值不是整数时，才把这句打印到标准错误，退出码 2。"
                "这句不带上当时的值。负数不是这句。带 --help 或 -h 时不打印这句。"
            ),
            "cli": LOGS_LINES_INTEGER,
        },
        {
            "key": "logs_usage",
            "zh": "日志多余参数或负数行数",
            "when": (
                "没有 --help 或 -h 时，logs 还有无法识别的剩余参数，或 --lines 是负数，"
                "才把这句打印到标准错误，退出码 2。非整数不是这句。"
            ),
            "cli": LOGS_USAGE,
        },
        {
            "key": "stop_usage",
            "zh": "停止用法",
            "when": "只有 stop 后面还有参数时才打印到标准错误，退出码 2。没有多余参数时不打印这句。",
            "cli": STOP_USAGE,
        },
        {
            "key": "status_usage",
            "zh": "状态用法",
            "when": "只有 status 后面还有参数时才打印到标准错误，退出码 2。没有多余参数时不打印这句。",
            "cli": STATUS_USAGE,
        },
        {
            "key": "already_running_lock",
            "zh": "文件锁已被占用",
            "when": (
                "只有 start 或 restart 拿不到本机文件锁时才把这句打印到标准错误，退出码 1。"
                "句尾有句号。控制台已有进程时拒绝第二次启动，用的也是这句。"
                "套接字仍在监听时不套用这句。"
            ),
            "cli": ALREADY_RUNNING,
        },
        {
            "key": "already_running_socket",
            "zh": "控制套接字仍在监听",
            "when": (
                "只有前台 start 或 restart 已经拿到文件锁，但控制套接字仍能连上时，"
                "才把这句打印到标准错误，退出码 1。句尾没有句号。"
                "拿不到文件锁时不套用这句。"
            ),
            "cli": ALREADY_RUNNING_LOCK,
        },
        {
            "key": "inherited_lock_missing",
            "zh": "没有继承到文件锁",
            "when": (
                "只有 python -m cli.launcher --_supervise 且没有设置 "
                "TRADINGAGENTS_LAUNCHER_LOCK_FD 时，才把这句打印到标准错误，退出码 2。"
                "空字符串不是缺少变量。控制台不打印这句，也不代为启动 supervisor。"
            ),
            "cli": MISSING_INHERITED_LOCK,
        },
        {
            "key": "inherited_lock_nope",
            "zh": "继承锁不是整数 nope",
            "when": (
                "只有 TRADINGAGENTS_LAUNCHER_LOCK_FD=nope 才打印下面这句到标准错误，退出码 2。"
                "别的非法值会带上当时的异常，不套用 nope。空字符串也是非法值，不是缺少变量。"
                "控制台不打印这句。"
            ),
            "cli": _inherited_lock_nope_sentence(),
        },
    ]
    for command in ("login", "logout", "auth-status"):
        rows.append({
            "key": f"not_analysis_{command}",
            "zh": f"{command} 不是分析参数",
            "when": f"start 或 restart 的第一个分析参数是 {command} 时打印到标准错误，退出码 2。其他命令不套用这句。",
            "cli": not_analysis_argument_sentence(command),
        })
    from webui.server import console_arg_error, console_help_text

    rows.append({
        "key": "web_help",
        "zh": "控制台帮助",
        "when": "只有 web --help 或 web -h 才打印到标准输出，退出码 0。这不是启动脚本 --help 里的 web 行。",
        "cli": console_help_text(),
    })
    rows.append({
        "key": "web_unrecognized",
        "zh": "控制台无法识别的参数",
        "when": "只有 web --nope 才打印这句到标准错误，退出码 2。别的无法识别参数会带上当时的参数，不套用 --nope。",
        "cli": console_arg_error(["--nope"]),
    })
    rows.append({
        "key": "web_port",
        "zh": "控制台端口不是整数",
        "when": "只有 web --port nope 才打印这句到标准错误，退出码 2。别的非整数值会带上当时的值，不套用 nope。",
        "cli": console_arg_error(["--port", "nope"]),
    })
    rows.extend(subscription_short_option_errors())
    return rows



def help_forward_examples() -> list[dict]:
    """Exact forwarded help results. Other extra arguments are not these sentences."""
    global _HELP_FORWARD
    if _HELP_FORWARD is not None:
        return _HELP_FORWARD
    import os
    import subprocess
    import sys

    def main_result(args: list[str]):
        env = os.environ.copy()
        env["COLUMNS"] = "96"
        return subprocess.run(
            [sys.executable, "-m", "cli.main", *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    short = main_result(["-h", "extra"])
    ticker = main_result(["--help", "--ticker"])
    if short.returncode != 2 or "No such option: -h" not in short.stderr:
        raise RuntimeError("cli.main -h extra did not produce the expected error")
    if ticker.returncode != 2 or "--ticker" not in ticker.stderr or "requires an argument" not in ticker.stderr:
        raise RuntimeError("cli.main --help --ticker did not produce the expected error: " + ticker.stderr[-300:])
    _HELP_FORWARD = [
        {
            "key": "help_extra",
            "zh": "--help 后面还有一个普通参数",
            "when": (
                "只有 start --help extra、start --help NVDA、restart --help extra、"
                "restart --help NVDA 或 cli --help extra 才把 python -m cli.main --help "
                "的标准输出原样打印，退出码 0，标准错误为空。这不是启动脚本帮助。"
                "--help --ticker 不套用这个结果。"
            ),
            "exit": 0,
            "stream": "stdout",
            "same_as": "cli --help",
        },
        {
            "key": "short_extra",
            "zh": "-h extra",
            "when": (
                "只有 start -h extra、restart -h extra 或 cli -h extra 才把下面这句打印到标准错误，"
                "退出码 2。--help extra 不套用这句。"
            ),
            "exit": 2,
            "stream": "stderr",
            "cli": short.stderr,
        },
        {
            "key": "help_ticker",
            "zh": "--help --ticker 缺少参数",
            "when": (
                "只有 start --help --ticker 或 restart --help --ticker 才把下面这句打印到标准错误，"
                "退出码 2。别的缺少参数的选项会带上当时的选项名，不套用 --ticker。"
            ),
            "exit": 2,
            "stream": "stderr",
            "cli": ticker.stderr,
        },
    ]
    return _HELP_FORWARD


_HELP_FORWARD: list[dict] | None = None


def help_invocation_notes() -> dict[str, str]:
    """When the launcher prints its own help, and when cli --help matches tradingagents --help."""
    return {
        "launcher_only": (
            "只有 start --help、start -h、restart --help 或 restart -h 单独出现时，"
            "才打印下面这段启动脚本帮助，退出码 0。后面还有参数时不打印这段，"
            "而是把参数交给 python -m cli.main。具体例句只对当时的参数。"
        ),
        "cli_same": "cli --help 打印的是下面同一段，退出码 0。",
    }



_BACKTEST_MISSING: dict[str, str] | None = None


def backtest_missing_errors() -> dict[str, str]:
    """Typer errors for a backtest that omits a required argument, in CLI order."""
    global _BACKTEST_MISSING
    if _BACKTEST_MISSING is not None:
        return _BACKTEST_MISSING
    from typer.testing import CliRunner

    from cli.main import app

    runner = CliRunner()
    cases = {
        "tickers": ["backtest"],
        "start": ["backtest", "NVDA"],
        "end": ["backtest", "NVDA", "--start", "2026-10-01"],
    }
    found = {}
    for key, args in cases.items():
        result = runner.invoke(app, args, prog_name="python -m cli.main")
        if result.exit_code != 2 or not result.stderr:
            raise RuntimeError(f"backtest {' '.join(args)} did not produce the expected error")
        found[key] = result.stderr
    _BACKTEST_MISSING = found
    return found


def backtest_missing_note() -> str:
    return (
        "没传标的参数时，命令行先打印缺少 tickers 的那句。"
        "标的传了但没填 --start 或 --end 时，先打印缺少的选项，不套用空标的那句。"
        "标的拆开是空的且日期已填时，才打印空标的那句。"
    )



_PLAN_ERRORS: dict[str, str] | None = None


def plan_cli_error(command: str, plan) -> str:
    """The typer stderr for a missing or illegal subscription plan. Never calls a valid plan."""
    from typer.testing import CliRunner

    from cli.main import app

    args = [command] if plan is None else [command, str(plan)]
    result = CliRunner().invoke(app, args, prog_name="python -m cli.main")
    if result.exit_code == 0 or not result.stderr:
        raise RuntimeError(f"{command} {plan!r} did not produce a plan error")
    return result.stderr


def plan_argument_errors() -> dict[str, str]:
    """Cached examples. Other illegal values are not these sentences."""
    global _PLAN_ERRORS
    if _PLAN_ERRORS is None:
        _PLAN_ERRORS = {
            "login_missing": plan_cli_error("login", None),
            "logout_missing": plan_cli_error("logout", None),
            "login_nope": plan_cli_error("login", "nope"),
            "logout_nope": plan_cli_error("logout", "nope"),
            "auth_status_nope": plan_cli_error("auth-status", "nope"),
        }
    return _PLAN_ERRORS


def plan_argument_note() -> str:
    return (
        "没传 plan 时，login 和 logout 各自打印缺少 plan 的句子。"
        "auth-status 省略 plan 时显示全部订阅，不打印缺少参数。"
        "空字符串是非法值，不是缺少参数。"
        "非法值会带上当时的原文，区分大小写。下面的 nope 只对 nope，不套用到别的值。"
        "auth-status nope 和 login nope 不是同一句。"
    )



def depth_run_override_when() -> str:
    """When the CLI prints a partial depth override. Not the skipped-question sentence."""
    return (
        "只有启动分析、研究深度已经被问过、并且 TRADINGAGENTS_MAX_DEBATE_ROUNDS "
        "或 TRADINGAGENTS_MAX_RISK_ROUNDS 里只有对应的那一个已设置时，命令行才打印下面这句。"
        "两个都设置时打印的是研究深度跳过句，不是这句。打开页面时命令行还没打印这句。"
    )


def public_catalog() -> dict:
    import inspect

    from cli.main import analyze, backtest

    analyze_help = _parameter_help(analyze)
    backtest_help = _parameter_help(backtest)
    start_line = _launcher_line("start ")
    if start_line:
        analyze_help["background"] = start_line
    return {
        "search": search_entries(),
        "commands": _commands_with_help(),
        "subscription_commands": subscription_commands(),
        "launcher_preamble": launcher_preamble(),
        "report_sections": report_sections(),
        "live_report_titles": live_report_titles(),
        "progress_teams": progress_teams(),
        "message_types": message_panel_types(),
        "progress_statuses": progress_statuses(),
        "show_agents": show_agents(),
        "config_env": config_env_rows(),
        "config_keys": config_key_rows(),
        "wall_time_pending": wall_time_pending_sentence(),
        "analyze_flags": flags_with_cli_help(ANALYZE_FLAGS, analyze_help),
        "backtest_flags": flags_with_cli_help(BACKTEST_FLAGS, backtest_help),
        "analyze_doc": inspect.getdoc(analyze) or "",
        "backtest_doc": inspect.getdoc(backtest) or "",
        "steps": list(SELECTION_STEPS),
        "step_eight": {key: dict(value) for key, value in STEP_EIGHT.items()},
        "languages": list(LANGUAGES),
        "setup_prompts": setup_prompts(),
        "placeholders": placeholders(),
        "form_defaults": form_defaults(),
        "report_prompts": report_prompts(),
        "picker_prompts": picker_prompts(),
        "depths": list(DEPTHS),
        "analysts": analyst_options(),
        "analyst_prompt": ANALYST_PROMPT,
        "research_depth_prompt": _SELECTION["research_depth"],
        "login_sentences": list(LOGIN_SENTENCES),
        "login_rows": login_rows(),
        "login_paste": LOGIN_SENTENCES[1],
        "login_paste_notes": login_paste_notes(),
        "plan_errors": plan_argument_errors(),
        "plan_note": plan_argument_note(),
        "key_prompts": key_prompts(),
        "outcome_sentences": outcome_sentences(),
        "confirmation_sentences": confirmation_sentences(),
        "validation_sentences": validation_sentences(),
        "guard_sentences": guard_sentences(),
        "display_sentences": display_sentences(),
        "log_sentences": log_sentences(),
        "analyst_instruction": ANALYST_INSTRUCTION,
        "provider_prompt": PROVIDER_PROMPT,
        "subscription_prompt": SUBSCRIPTION_PROMPT,
        "subscription_choice": SUBSCRIPTION_CHOICE,
        "vendors": vendor_options(),
        "data_env": list(DATA_ENV),
        "cloud_env": list(CLOUD_ENV),
        "tools": tool_options(),
        "reasoning": {key: list(value) for key, value in REASONING.items()},
        "regions": {key: list(value) for key, value in REGIONS.items()},
        "role_split": dict(ROLE_SPLIT),
        "question_defaults": question_defaults(),
        "notes": [
            "研究深度是辩论轮数，不是模型思考强度。",
            "思考强度只有 ChatGPT、OpenAI、Gemini、Claude 有独立选项。Grok 没有这一档。",
            "快模型调用数据工具，并撰写分析师、研究员、辩论者、交易员和复盘文本；它不是数据源。深模型只负责研究经理和组合经理。行情、财报和新闻来自 data_vendors，不是 Grok 搜索。",
            "订阅登录不能代替 OPENAI_API_KEY。供应商名是 chatgpt 和 grok，不是 openai 和 xai。",
            "数据供应商不是命令行菜单。页面改的是运行配置 data_vendors，未知名称会在启动前拒绝。",
            "工具级供应商 tool_vendors 优先于类别链。命令行没有这一步菜单。",
            "结果目录对应 TRADINGAGENTS_RESULTS_DIR。控制台只能写在 ~/.tradingagents 下。",
            "FRED、Alpha Vantage 和 SEC 联系标识在账户页保存，写入本机 .env，页面不回显内容。",
            "Azure 部署名、接口版本、终结点，以及 Bedrock 区域、配置档和令牌也在账户页保存。",
            "进程启动不等于分析完成。",
            "回测不是启动脚本命令。直接把 backtest 交给启动脚本会打印拒绝句；可用入口是 cli backtest。",
        ],
        "backtest_route": backtest_route(),
        "backtest_missing": backtest_missing_errors(),
        "backtest_missing_note": backtest_missing_note(),
        "launcher_conditional": launcher_conditional_rows(),
        "help_forward": help_forward_examples(),
        "cancel_exits": cancel_exit_rows(),
        "help_invocation": help_invocation_notes(),
        "depth_run_when": depth_run_override_when(),
    }
