import datetime
import os
import subprocess
import sys
from pathlib import Path

import questionary
import typer
from dotenv import find_dotenv, set_key
from rich.markup import escape

from cli.display import console
from cli.models import AnalystType, AssetType
from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.llm_clients.api_key_env import get_api_key_env
from tradingagents.llm_clients.model_catalog import get_model_options
from tradingagents.llm_clients.openai_client import OPENAI_COMPATIBLE_PROVIDERS, SIGNED_IN_PROVIDERS
from tradingagents.llm_clients.subscriptions import SUBSCRIPTIONS, get_subscription

TICKER_INPUT_EXAMPLES = "SPY, 0700.HK, BTC-USD"
NO_TICKER_EXIT = "No ticker symbol provided. Exiting..."
NO_DEPTH_EXIT = "No research depth selected. Exiting..."
NO_PROVIDER_EXIT = "No LLM provider selected. Exiting..."
NO_SUBSCRIPTION_EXIT = "No subscription selected. Exiting..."
NO_MODEL_EXIT = "No model selected. Exiting..."
CANCELLED_EXIT = "Cancelled. Exiting..."


def no_thinking_engine_exit(mode: str) -> str:
    """The curated model-menu cancel sentence. ``mode`` is the CLI word quick or deep."""
    return f"No {mode} thinking llm engine selected. Exiting..."
# Empty interactive input becomes this symbol. A --ticker flag still refuses blank.
TICKER_PROMPT_DEFAULT = "SPY"
TICKER_PROMPT = f"Enter ticker symbol (e.g. {TICKER_INPUT_EXAMPLES}):"
TICKER_INVALID = "Please enter a valid ticker symbol, e.g. AAPL, 000404.SZ, 0700.HK, GC=F."
CUSTOM_LANGUAGE_PROMPT = "Enter language name (e.g. Turkish, Vietnamese, Thai, Indonesian):"
CUSTOM_LANGUAGE_INVALID = "Please enter a language name."
ANALYST_TEAM_PROMPT = "Select Your [Analysts Team]:"
ANALYST_CHECKBOX_INSTRUCTION = (
    "\n- Press Space to select/unselect analysts\n"
    "- Press 'a' to select/unselect all\n"
    "- Press Enter when done"
)
RESEARCH_DEPTH_PROMPT = "Select Your [Research Depth]:"
MENU_NAVIGATION_INSTRUCTION = "\n- Use arrow keys to navigate\n- Press Enter to select"
LLM_PROVIDER_PROMPT = "Select your LLM Provider:"
SUBSCRIPTION_SELECT_PROMPT = "Select your subscription:"
CHATGPT_PLAN_MENU = "Subscription (use your ChatGPT plan)"
SIGNED_IN_ACCOUNT_MENU = "Subscriptions (use a signed-in account)"
MODEL_ID_PROMPT = "Enter model ID:"
CUSTOM_MODEL_CHOICE = "Custom model ID"
MODEL_ID_REQUIRED = "Please enter a model ID."
AZURE_DEPLOYMENT_REQUIRED = "Please enter a deployment name."
OPENROUTER_ID_PROMPT = "Enter OpenRouter model ID (e.g. google/gemma-4-26b-a4b-it):"
OPENAI_COMPATIBLE_URL_PROMPT = (
    "Enter the OpenAI-compatible base URL "
    "(e.g. http://localhost:8000/v1 for vLLM, http://localhost:1234/v1 for LM Studio):"
)
URL_SCHEME_REQUIRED = "Enter a URL starting with http:// or https://"
REASONING_EFFORT_PROMPT = "Select Reasoning Effort:"
THINKING_MODE_PROMPT = "Select Thinking Mode:"
EFFORT_LEVEL_PROMPT = "Select Effort Level:"

ANALYST_CHOICES = [
    ("Market Analyst", AnalystType.MARKET),
    ("Sentiment Analyst", AnalystType.SOCIAL),
    ("News Analyst", AnalystType.NEWS),
    ("Fundamentals Analyst", AnalystType.FUNDAMENTALS),
]

CRYPTO_SUFFIXES = ("-USD", "-USDT", "-USDC", "-BTC", "-ETH")
# V2-METALS: Yahoo futures symbols (GC=F gold, SI=F silver, CL=F crude ...).
COMMODITY_SUFFIXES = ("=F",)


# The style of every selection menu.
_MENU_STYLE = questionary.Style([
    ("selected", "fg:magenta noinherit"),
    ("highlighted", "fg:magenta noinherit"),
    ("pointer", "fg:magenta noinherit"),
])


def is_valid_ticker_input(value: str) -> bool:
    """Whether a ticker entry names a symbol the run can use.

    The symbol it resolves to (``XAUUSD+`` -> ``GC=F``) must pass the data
    layer's own check, so the prompt refuses exactly what the run would.
    Empty input is allowed (it defaults to SPY downstream).
    """
    if not value.strip():
        return True
    try:
        safe_ticker_component(normalize_ticker_symbol(value))
    except ValueError:
        return False
    return True


def get_ticker() -> str:
    """Prompt the user to enter a ticker symbol, preserving exchange suffixes.

    Uses questionary.text (not typer.prompt, which strips trailing dot-suffixes
    like ``000404.SH`` on some shells) and validates the symbol charset so an
    obvious typo is caught before the run starts.
    """
    ticker = questionary.text(
        TICKER_PROMPT,
        validate=lambda x: (
            is_valid_ticker_input(x)
            or TICKER_INVALID
        ),
        style=questionary.Style(
            [
                ("text", "fg:green"),
                ("highlighted", "noinherit"),
            ]
        ),
    ).ask()

    if ticker is None:
        console.print(f"\n[red]{NO_TICKER_EXIT}[/red]")
        exit(1)

    return interactive_ticker_or_default(ticker)



DATE_RETRY_PREFIX = "Error:"
ANALYST_NAME_REQUIRED = "name at least one analyst"


def ticker_symbol_error(value: str) -> str:
    return f"not a ticker symbol: {value!r} (e.g. {TICKER_INPUT_EXAMPLES})"


def date_format_error(value: str) -> str:
    return f"not a date: {value!r}; use YYYY-MM-DD"


def future_date_error(value: str) -> str:
    return f"{value} is in the future"


def date_retry_error(message: str) -> str:
    return f"{DATE_RETRY_PREFIX} {message}"


def unknown_analyst_error(unknown, choices: str) -> str:
    joined = ", ".join(unknown)
    return f"unknown analyst {joined}; choose from {choices}"


def interactive_ticker_or_default(value: str | None) -> str:
    """The interactive ticker: blank becomes SPY; a typed symbol is canonical.

    ``None`` is a cancelled prompt, not a blank field. Callers that already
    treated cancel as an exit should not pass it here.
    """
    text = value or ""
    if not text.strip():
        return TICKER_PROMPT_DEFAULT
    if not is_valid_ticker_input(text):
        raise ValueError(ticker_symbol_error(text))
    return normalize_ticker_symbol(text)


def parse_ticker(value: str) -> str:
    """A ticker given on the command line, canonical; empty or malformed is refused."""
    if not value.strip() or not is_valid_ticker_input(value):
        raise ValueError(ticker_symbol_error(value))
    return normalize_ticker_symbol(value)


def parse_analysis_date(value: str) -> str:
    """An analysis date as YYYY-MM-DD, today or earlier."""
    try:
        day = datetime.datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(date_format_error(value)) from None
    if day > datetime.date.today():
        raise ValueError(future_date_error(value))
    return day.isoformat()


def parse_analysts(value: str, asset_type: AssetType) -> list[AnalystType]:
    """Comma-separated analyst names, in the canonical order, checked against the asset."""
    # "sentiment" is the name users see; the analyst's key is "social".
    names = [{"sentiment": "social"}.get(n, n) for n in (n.strip().lower() for n in value.split(",")) if n]
    if not names:
        raise ValueError(ANALYST_NAME_REQUIRED)
    known = {a.value: a for a in AnalystType}
    unknown = [n for n in names if n not in known]
    if unknown:
        choices = ", ".join("sentiment" if name == "social" else name for name in known)
        raise ValueError(unknown_analyst_error(unknown, choices))
    available = filter_analysts_for_asset_type(list(known.values()), asset_type)
    unavailable = [n for n in names if known[n] not in available]
    if unavailable:
        raise ValueError(unavailable_analyst_sentence(unavailable, asset_type.value))
    return [a for a in available if a.value in names]


def normalize_ticker_symbol(ticker: str) -> str:
    """Resolve user input to its canonical Yahoo symbol (single source of truth).

    Delegates to the data layer's ``normalize_symbol`` so the symbol the CLI
    passes through the pipeline is exactly the one the data path will price
    (e.g. ``BTCUSD`` -> ``BTC-USD``, ``XAUUSD`` -> ``GC=F``). Falls back to the
    plain upper-case if the data layer is unavailable.
    """
    try:
        from tradingagents.dataflows.symbols import normalize_symbol

        return normalize_symbol(ticker)
    except Exception:
        return ticker.strip().upper()


def detect_asset_type(ticker: str) -> AssetType:
    """Classify on the canonical symbol so e.g. BTCUSD and BTC-USDT both read as
    crypto (#981/#982), matching what the data path will actually fetch."""
    canonical = normalize_ticker_symbol(ticker)
    if canonical.endswith(CRYPTO_SUFFIXES):
        return AssetType.CRYPTO
    # V2-METALS: metal / commodity futures (GC=F, SI=F, PL=F ...), which is
    # also where the spot aliases land (XAUUSD, GOLD, XAG, PLATINUM ...).
    if canonical.endswith(COMMODITY_SUFFIXES):
        return AssetType.COMMODITY
    return AssetType.STOCK


# Asset types with no company behind them: the fundamentals analyst is not offered.
NO_FUNDAMENTALS = frozenset({AssetType.CRYPTO, AssetType.COMMODITY})


def filter_analysts_for_asset_type(
    analysts: list[AnalystType], asset_type: AssetType
) -> list[AnalystType]:
    if asset_type not in NO_FUNDAMENTALS:
        return analysts
    return [
        analyst
        for analyst in analysts
        if analyst != AnalystType.FUNDAMENTALS
    ]


def _matching_choice(options, default):
    """The option value equal to ``default``, or None to leave the menu as is."""
    return next((value for _, value in options if value == default), None)


def select_analysts(asset_type: AssetType = AssetType.STOCK, default=None) -> list[AnalystType]:
    """Select analysts using an interactive checkbox.

    ``default`` pre-checks the previous run's analysts; the prompt still shows.
    """
    available_analysts = filter_analysts_for_asset_type(
        [value for _, value in ANALYST_CHOICES],
        asset_type,
    )
    choices = questionary.checkbox(
        ANALYST_TEAM_PROMPT,
        choices=[
            questionary.Choice(display, value=value, checked=value.value in (default or []))
            for display, value in ANALYST_CHOICES
            if value in available_analysts
        ],
        instruction=ANALYST_CHECKBOX_INSTRUCTION,
        validate=lambda x: len(x) > 0 or ANALYST_REQUIRED,
        style=questionary.Style(
            [
                ("checkbox-selected", "fg:green"),
                ("selected", "fg:green noinherit"),
                ("highlighted", "noinherit"),
                ("pointer", "noinherit"),
            ]
        ),
    ).ask()

    if not choices:
        console.print(f"\n[red]{NO_ANALYSTS_EXIT}[/red]")
        exit(1)

    return choices


OUTPUT_LANGUAGE_CHOICES = (
    ("English (default)", "English"),
    ("Chinese (中文)", "Chinese"),
    ("Japanese (日本語)", "Japanese"),
    ("Korean (한국어)", "Korean"),
    ("Hindi (हिन्दी)", "Hindi"),
    ("Spanish (Español)", "Spanish"),
    ("Portuguese (Português)", "Portuguese"),
    ("French (Français)", "French"),
    ("German (Deutsch)", "German"),
    ("Arabic (العربية)", "Arabic"),
    ("Russian (Русский)", "Russian"),
    ("Custom language", "custom"),
)
DEPTH_OPTIONS = (
    ("Shallow - Quick research, few debate and strategy discussion rounds", 1),
    ("Medium - Middle ground, moderate debate rounds and strategy discussion", 3),
    ("Deep - Comprehensive research, in depth debate and strategy discussion", 5),
)
OPENAI_EFFORT_CHOICES = (
    ("Medium (Default)", "medium"),
    ("High (More thorough)", "high"),
    ("Low (Faster)", "low"),
)
GEMINI_THINKING_CHOICES = (
    ("Enable Thinking (recommended)", "high"),
    ("Minimal/Disable Thinking", "minimal"),
)
ANTHROPIC_EFFORT_CHOICES = (
    ("High (recommended)", "high"),
    ("Medium (balanced)", "medium"),
    ("Low (faster, cheaper)", "low"),
)


def select_research_depth(default=None) -> int:
    """Select research depth using an interactive selection."""

    choice = questionary.select(
        RESEARCH_DEPTH_PROMPT,
        choices=[
            questionary.Choice(display, value=value) for display, value in DEPTH_OPTIONS
        ],
        default=_matching_choice(DEPTH_OPTIONS, default),
        instruction=MENU_NAVIGATION_INSTRUCTION,
        style=questionary.Style(
            [
                ("selected", "fg:yellow noinherit"),
                ("highlighted", "fg:yellow noinherit"),
                ("pointer", "fg:yellow noinherit"),
            ]
        ),
    ).ask()

    if choice is None:
        console.print(f"\n[red]{NO_DEPTH_EXIT}[/red]")
        exit(1)

    return choice


# Mainstream OpenRouter chat-LLM provider namespaces. We surface the newest
# models from these rather than the universal-newest, which is dominated by
# niche/experimental releases. These are the general-purpose chat providers;
# more enterprise/specialised namespaces (nvidia, cohere, amazon, ...) tend to
# ship research/safety variants as their newest, so they're left out of the
# shortlist. Provider names are stable (unlike model IDs), so this rarely needs
# touching; anything not here is still reachable via Custom ID.
_OPENROUTER_MAINSTREAM = {
    "openai", "anthropic", "google", "deepseek", "qwen", "mistralai",
    "meta-llama", "x-ai", "z-ai", "minimax", "moonshotai",
}


def _fetch_openrouter_models_or_raise() -> list[tuple[str, str]]:
    """Fetch available models from the OpenRouter API, or raise."""
    import requests
    resp = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
    resp.raise_for_status()
    models = resp.json().get("data", [])
    # Newest first so the top-N shown really is the latest available — the
    # API currently returns this order, but sort explicitly so the prompt's
    # "latest available" label holds regardless of response ordering.
    models.sort(key=lambda m: m.get("created") or 0, reverse=True)
    return [(m.get("name") or m["id"], m["id"]) for m in models]



ANALYST_REQUIRED = "You must select at least one analyst."
NO_ANALYSTS_EXIT = "No analysts selected. Exiting..."
NO_ENDPOINT_EXIT = "No endpoint URL provided. Exiting..."


def openrouter_fetch_error(exc) -> str:
    return f"Could not fetch OpenRouter models: {exc}"


def subscription_models_error(label: str, exc) -> str:
    return f"Could not list your {label} plan's models: {exc}"


def plan_use_sentence(label: str, account: str) -> str:
    return f"✓ Using your {label}: {account}"


def _fetch_openrouter_models() -> list[tuple[str, str]]:
    """Fetch available models from the OpenRouter API."""
    try:
        return _fetch_openrouter_models_or_raise()
    except Exception as e:
        console.print(f"\n[yellow]{openrouter_fetch_error(e)}[/yellow]")
        return []


def _subscription_label(provider: str) -> str:
    """Return the concise label used in plan prompts and diagnostics."""
    return get_subscription(provider).display_name.split(" (", 1)[0]



def subscription_menu_label() -> str:
    """The provider-menu row that opens signed-in plans."""
    if tuple(SIGNED_IN_PROVIDERS) == ("chatgpt",):
        return CHATGPT_PLAN_MENU
    return SIGNED_IN_ACCOUNT_MENU


def model_picker_title(provider: str, mode: str) -> str:
    """The questionary title for one provider and quick/deep tier."""
    key = (provider or "").lower()
    label = mode.title() if mode in {"quick", "deep"} else "Quick"
    if key == "chatgpt":
        return f"Select Your [{label}-Thinking] ChatGPT Plan Model:"
    if key in SUBSCRIPTIONS:
        return f"Select Your [{label}-Thinking] {_subscription_label(key)} Model:"
    if key == "openrouter":
        return f"Select Your [{label}-Thinking] OpenRouter Model (latest available):"
    if key == "azure":
        return f"Enter Azure deployment name ({mode}-thinking):"
    return f"Select Your [{label}-Thinking LLM Engine]:"


def _subscription_auth_label(provider: str) -> str:
    """Return the label used when announcing an authenticated subscription."""
    spec = get_subscription(provider)
    return (
        f"{_subscription_label(provider)} plan"
        if provider.lower() == "chatgpt"
        else spec.display_name
    )


def select_subscription_model(provider: str, mode: str, default=None) -> str:
    """Select a model listed by a signed-in subscription, or enter an ID.

    ``list_models`` belongs to the subscription's auth adapter rather than the
    inference client. This lets ChatGPT and Grok use their own credentials and
    discovery endpoints while the CLI keeps one model-selection flow.
    """
    spec = get_subscription(provider)
    try:
        models = spec.auth.list_models()
    except Exception as e:
        label = _subscription_label(provider)
        console.print(f"\n[yellow]{escape(subscription_models_error(label, e))}[/yellow]")
        models = []
    # Preserve the ChatGPT wording; other plans use their subscription label.
    title = model_picker_title(provider, mode)
    return _select_listed_model(title, models, default, _prompt_custom_model_id)


def select_chatgpt_model(mode: str, default=None) -> str:
    """Compatibility wrapper for the ChatGPT subscription model picker."""
    return select_subscription_model("chatgpt", mode, default)


def _select_listed_model(title: str, models, default, custom) -> str:
    """Select from ``models`` ((name, model ID) pairs, listed live) or enter one with ``custom``."""
    choices = [questionary.Choice(name, value=mid) for name, mid in models]
    choices.append(questionary.Choice(CUSTOM_MODEL_CHOICE, value="custom"))
    choice = questionary.select(
        title,
        choices=choices,
        default=default if any(c.value == default for c in choices) else None,
        instruction=MENU_NAVIGATION_INSTRUCTION,
        style=_MENU_STYLE,
    ).ask()
    if choice is None:
        console.print(f"\n[red]{NO_MODEL_EXIT}[/red]")
        exit(1)
    return custom() if choice == "custom" else choice


def _require_text(message: str, hint: str) -> str:
    """Prompt for a required value; exit cleanly if the user cancels.

    ``questionary.text(...).ask()`` returns None on Ctrl-C/Esc; mirror the
    exit-on-cancel behavior of the other required selections so a cancelled
    prompt never returns an empty model/deployment that would fail downstream.
    """
    response = questionary.text(
        message,
        validate=lambda x: len(x.strip()) > 0 or hint,
    ).ask()
    if response is None:
        console.print(f"\n[red]{CANCELLED_EXIT}[/red]")
        exit(1)
    return response.strip()


def select_openrouter_model(mode: str, default=None) -> str:
    """Select an OpenRouter model from the newest available, or enter a custom ID.

    ``mode`` ("quick"/"deep") labels the prompt so the two consecutive
    OpenRouter selections are distinguishable, like the other providers (#1000).
    """
    models = _fetch_openrouter_models()  # newest first
    # Prefer the newest from mainstream providers so the shortlist isn't crowded
    # out by niche/experimental releases; fall back to all if none match.
    mainstream = [
        (name, mid) for name, mid in models
        if not mid.startswith("~")  # skip variant/alias duplicate routes
        and mid.split("/", 1)[0] in _OPENROUTER_MAINSTREAM
    ]
    top = (mainstream or models)[:5]
    return _select_listed_model(
        model_picker_title("openrouter", mode), top, default,
        lambda: _require_text(OPENROUTER_ID_PROMPT, MODEL_ID_REQUIRED))


# Providers whose models are listed live, with the menu that offers them.
# Keep the named ChatGPT wrapper for backwards compatibility and bind every
# other subscription through the generic picker. ``LISTED_LIVE`` is imported
# by cli.prefs, so it remains a plain mapping rather than a computed property.
LISTED_LIVE = {"openrouter": select_openrouter_model, "chatgpt": select_chatgpt_model}
for _subscription_key in SUBSCRIPTIONS:
    if _subscription_key == "chatgpt":
        continue
    LISTED_LIVE[_subscription_key] = lambda mode, default=None, _key=_subscription_key: (
        select_subscription_model(_key, mode, default)
    )


def _prompt_custom_model_id() -> str:
    """Prompt user to type a custom model ID."""
    return _require_text(MODEL_ID_PROMPT, MODEL_ID_REQUIRED)


def _select_model(provider: str, mode: str, default=None) -> str:
    """Select a model for the given provider and mode (quick/deep)."""
    listed = LISTED_LIVE.get(provider.lower())
    if listed is not None:
        return listed(mode, default)
    # Accommodate a subscription added to the registry after this module was
    # imported (for example by an embedding application) without requiring a
    # second hard-coded picker entry.
    if provider.lower() in SUBSCRIPTIONS:
        return select_subscription_model(provider.lower(), mode, default)

    if provider.lower() == "azure":
        return _require_text(
            model_picker_title("azure", mode),
            AZURE_DEPLOYMENT_REQUIRED,
        )

    choice = questionary.select(
        model_picker_title(provider, mode),
        choices=[
            questionary.Choice(display, value=value)
            for display, value in get_model_options(provider, mode)
        ],
        default=_matching_choice(get_model_options(provider, mode), default),
        instruction=MENU_NAVIGATION_INSTRUCTION,
        style=_MENU_STYLE,
    ).ask()

    if choice is None:
        console.print(f"\n[red]{no_thinking_engine_exit(mode)}[/red]")
        exit(1)

    if choice == "custom":
        return _prompt_custom_model_id()

    return choice


def select_shallow_thinking_agent(provider, default=None) -> str:
    """Select shallow thinking llm engine using an interactive selection."""
    return _select_model(provider, "quick", default)


def select_deep_thinking_agent(provider, default=None) -> str:
    """Select deep thinking llm engine using an interactive selection."""
    return _select_model(provider, "deep", default)


def _llm_provider_table() -> list[tuple[str, str, str | None]]:
    """(display_name, provider_key, base_url) for every supported provider.

    Shared by the interactive picker and by env-driven configuration so an
    env-set provider resolves to the same default endpoint the menu uses.
    Ollama users can point at a remote ollama-serve via OLLAMA_BASE_URL
    (convention from the broader Ollama ecosystem); falls back to the
    localhost default when unset.
    """
    ollama_url = os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434/v1"
    # Subscription rows are data, not CLI conditionals: auth adapters can be
    # added to the registry without editing this provider table.
    subscription_rows = [
        (spec.display_name, key, spec.base_url)
        for key, spec in SUBSCRIPTIONS.items()
    ]
    return [
        ("OpenAI", "openai", "https://api.openai.com/v1"),
        ("Google", "google", None),
        ("Anthropic", "anthropic", "https://api.anthropic.com/"),
        *subscription_rows,
        ("xAI", "xai", "https://api.x.ai/v1"),
        ("DeepSeek", "deepseek", "https://api.deepseek.com"),
        ("Qwen", "qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
        # Z.AI international, the platform ZHIPU_API_KEY belongs to; the CN
        # platform is the separate glm-cn key, chosen in the region prompt.
        ("GLM", "glm", "https://api.z.ai/api/paas/v4/"),
        ("MiniMax", "minimax", "https://api.minimax.io/v1"),
        ("OpenRouter", "openrouter", "https://openrouter.ai/api/v1"),
        ("Mistral", "mistral", "https://api.mistral.ai/v1"),
        ("Kimi (Moonshot)", "kimi", "https://api.moonshot.ai/v1"),
        ("Groq", "groq", "https://api.groq.com/openai/v1"),
        ("NVIDIA NIM", "nvidia", "https://integrate.api.nvidia.com/v1"),
        ("Azure OpenAI", "azure", None),
        ("Amazon Bedrock", "bedrock", None),
        ("Ollama", "ollama", ollama_url),
        ("OpenAI-compatible (vLLM, LM Studio, llama.cpp, custom relay)", "openai_compatible", None),
    ]


def provider_default_url(provider_key: str) -> str | None:
    """Return the default backend URL for a provider key, or None if unknown."""
    key = provider_key.lower()
    for _, pk, url in _llm_provider_table():
        if pk == key:
            return url
    return None


def resolve_backend_url(
    provider: str, menu_url: str | None = None, env_url: str | None = None
) -> str | None:
    """Resolve the backend URL with the correct precedence.

    An explicit env override (``env_url``, from ``TRADINGAGENTS_LLM_BACKEND_URL``
    via ``DEFAULT_CONFIG['backend_url']``) is honored regardless of how the
    provider was chosen — interactively or from the environment (#978).
    Otherwise the menu/region URL, then the provider's default.
    """
    return env_url or menu_url or provider_default_url(provider)


def prompt_openai_compatible_url(default=None) -> str:
    """Prompt for a custom OpenAI-compatible endpoint base URL."""
    url = questionary.text(
        OPENAI_COMPATIBLE_URL_PROMPT,
        default=default or "",
        validate=lambda x: x.strip().startswith(("http://", "https://"))
        or URL_SCHEME_REQUIRED,
    ).ask()
    if not url:
        console.print(f"\n[red]{NO_ENDPOINT_EXIT}[/red]")
        exit(1)
    return url.strip()


# The menu entry that groups the providers used through a personal plan.
_SUBSCRIPTION = ("subscription", None)


def select_llm_provider(default=None) -> tuple[str, str | None]:
    """Select the LLM provider and its API endpoint."""
    PROVIDERS = _llm_provider_table()
    # A region-specific key (qwen-cn) is chosen in a later prompt; the menu
    # lists the base provider.
    base = (default or "").split("-cn")[0]
    choices = []
    for display, provider_key, url in PROVIDERS:
        if provider_key in SIGNED_IN_PROVIDERS:
            continue
        choices.append(questionary.Choice(display, value=(provider_key, url)))
        if provider_key == "anthropic":
            subscription_title = subscription_menu_label()
            choices.append(questionary.Choice(subscription_title, value=_SUBSCRIPTION))
    preselected = _SUBSCRIPTION if base in SIGNED_IN_PROVIDERS else next(
        ((key, url) for _, key, url in PROVIDERS if key == base), None
    )

    choice = questionary.select(
        LLM_PROVIDER_PROMPT,
        choices=choices,
        default=preselected,
        instruction=MENU_NAVIGATION_INSTRUCTION,
        style=_MENU_STYLE,
    ).ask()

    if choice is None:
        console.print(f"\n[red]{NO_PROVIDER_EXIT}[/red]")
        exit(1)

    if choice == _SUBSCRIPTION:
        choice = questionary.select(
            SUBSCRIPTION_SELECT_PROMPT,
            choices=[questionary.Choice(display, value=(key, url))
                     for display, key, url in PROVIDERS if key in SIGNED_IN_PROVIDERS],
            style=_MENU_STYLE,
        ).ask()
        if choice is None:
            console.print(f"\n[red]{NO_SUBSCRIPTION_EXIT}[/red]")
            exit(1)

    provider, url = choice
    return provider, url


def ask_openai_reasoning_effort() -> str:
    """Ask for OpenAI reasoning effort level."""
    return questionary.select(
        REASONING_EFFORT_PROMPT,
        choices=[questionary.Choice(label, value) for label, value in OPENAI_EFFORT_CHOICES],
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def ask_anthropic_effort() -> str | None:
    """Ask for Anthropic effort level.

    Controls token usage and response thoroughness on Claude 4.5 / 4.6 / 4.7
    models. The API also accepts "max"; we expose low/medium/high as the
    common selection range.
    """
    return questionary.select(
        EFFORT_LEVEL_PROMPT,
        choices=[questionary.Choice(label, value) for label, value in ANTHROPIC_EFFORT_CHOICES],
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def ask_gemini_thinking_config() -> str | None:
    """Ask for Gemini thinking configuration.

    Returns thinking_level: "high" or "minimal".
    Client maps to appropriate API param based on model series.
    """
    return questionary.select(
        THINKING_MODE_PROMPT,
        choices=[questionary.Choice(label, value) for label, value in GEMINI_THINKING_CHOICES],
        style=questionary.Style([
            ("selected", "fg:green noinherit"),
            ("highlighted", "fg:green noinherit"),
            ("pointer", "fg:green noinherit"),
        ]),
    ).ask()


GLM_REGION_PROMPT = "Select GLM platform:"
GLM_REGION_CHOICES = (
    ("Z.AI — api.z.ai (international, uses ZHIPU_API_KEY)", ("glm", "https://api.z.ai/api/paas/v4/")),
    ("BigModel — open.bigmodel.cn (China, uses ZHIPU_CN_API_KEY)", ("glm-cn", "https://open.bigmodel.cn/api/paas/v4/")),
)
QWEN_REGION_PROMPT = "Select Qwen region:"
QWEN_REGION_CHOICES = (
    ("International — dashscope-intl.aliyuncs.com (uses DASHSCOPE_API_KEY)", ("qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")),
    ("China — dashscope.aliyuncs.com (uses DASHSCOPE_CN_API_KEY)", ("qwen-cn", "https://dashscope.aliyuncs.com/compatible-mode/v1")),
)
MINIMAX_REGION_PROMPT = "Select MiniMax region:"
MINIMAX_REGION_CHOICES = (
    ("Global — api.minimax.io (uses MINIMAX_API_KEY)", ("minimax", "https://api.minimax.io/v1")),
    ("China — api.minimaxi.com (uses MINIMAX_CN_API_KEY)", ("minimax-cn", "https://api.minimaxi.com/v1")),
)


def ask_glm_region() -> tuple[str, str]:
    """Ask which GLM platform (Z.AI international vs BigModel China) to use.

    Zhipu serves the same GLM models under two brands with separate
    accounts; keys aren't interchangeable. Returns (provider_key, backend_url).
    """
    return questionary.select(
        GLM_REGION_PROMPT,
        choices=[questionary.Choice(label, value=value) for label, value in GLM_REGION_CHOICES],
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def ask_qwen_region() -> tuple[str, str]:
    """Ask which Qwen region (international vs China) to use.

    Alibaba DashScope exposes two endpoints with separate accounts —
    a key from one region does NOT authenticate against the other
    (fixes #758). Returns (provider_key, backend_url).
    """
    return questionary.select(
        QWEN_REGION_PROMPT,
        choices=[questionary.Choice(label, value=value) for label, value in QWEN_REGION_CHOICES],
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def ask_minimax_region() -> tuple[str, str]:
    """Ask which MiniMax region (global vs China) to use.

    MiniMax exposes two endpoints with separate accounts — a key from
    one region does NOT authenticate against the other. Returns
    (provider_key, backend_url).
    """
    return questionary.select(
        MINIMAX_REGION_PROMPT,
        choices=[questionary.Choice(label, value=value) for label, value in MINIMAX_REGION_CHOICES],
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()



def unavailable_analyst_sentence(names, asset_type: str) -> str:
    return f"{', '.join(names)} is not available for {asset_type}"


def ollama_endpoint_lines(url: str, from_env: str | None = None) -> list[str]:
    """Visible Ollama endpoint lines, without Rich markup."""
    origin = " (from OLLAMA_BASE_URL)" if from_env and from_env == url else ""
    lines = [f"✓ Using Ollama at {url}{origin}"]
    if not url.startswith(("http://", "https://")):
        lines.append(
            f"Note: {url!r} is missing a scheme. "
            "Ollama-serve typically expects a URL like http://<host>:11434/v1."
        )
    elif ":11434" not in url and "://localhost" not in url and "://127.0.0.1" not in url:
        lines.append(
            f"Note: {url!r} doesn't include port 11434. "
            "Make sure your remote ollama-serve listens on the port shown above."
        )
    return lines


def confirm_ollama_endpoint(url: str) -> None:
    """Show the resolved Ollama endpoint after provider selection.

    Surfaces three things the user benefits from seeing before model
    selection: which URL we'll actually hit, where it came from
    (`OLLAMA_BASE_URL` vs default), and a soft warning if the URL is
    missing the scheme/port that ollama-serve expects. The warning is
    advisory only — we don't reject malformed input, since the user may
    be doing something deliberately unusual (e.g. a reverse-proxy path).
    """
    lines = ollama_endpoint_lines(url, os.environ.get("OLLAMA_BASE_URL"))
    console.print(f"[green]{lines[0]}[/green]")
    for note in lines[1:]:
        console.print(f"[yellow]{note}[/yellow]")


def ensure_signed_in(provider: str) -> None:
    """Make sure a plan provider's sign-in works before the run, offering to sign in now.

    Signing in runs as its own process (``tradingagents login chatgpt``), so its
    read of a pasted address ends with it and never takes a later answer."""
    key = provider.lower()
    if key not in SIGNED_IN_PROVIDERS:
        return
    spec = get_subscription(key)

    try:
        spec.auth.access_token()
    except spec.unreachable_error as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    except spec.auth_error as exc:
        if not sys.stdin.isatty():
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(code=1) from None
        console.print(f"\n[yellow]{escape(str(exc))}[/yellow]")
        # -P keeps a cli/ package in the working directory from shadowing ours.
        login = [sys.executable, "-P", "-m", "cli.main", "login", key]
        if subprocess.run(login).returncode != 0:
            raise typer.Exit(code=1) from None
    console.print(
        f"[green]{escape(plan_use_sentence(_subscription_auth_label(key), spec.auth.signed_in_as() or ''))}[/green]"
    )


def paste_key_prompt(env_var: str) -> str:
    """The password prompt shown when an API key is missing."""
    return f"Paste your {env_var} (will be saved to .env):"


def unset_key_sentence(env_var: str) -> str:
    return f"{env_var} is not set in your environment."


def missing_key_sentence(env_var: str) -> str:
    return f"{env_var} is not set; there is no terminal to ask for it."


def skipped_key_sentence(env_var: str) -> str:
    return f"Skipped. API calls will fail until {env_var} is set."


def saved_key_sentence(env_var: str, env_path: str) -> str:
    return f"Saved {env_var} to {env_path}"


def ensure_api_key(provider: str) -> str | None:
    """Make sure the API key for `provider` is available in the environment.

    If the env var is already set, returns its value untouched. Otherwise
    interactively prompts the user, persists the value to the project's
    .env file via python-dotenv's set_key (creating .env if needed), and
    exports it into os.environ so the current process picks it up.

    Returns None for providers that do not require a key (e.g. ollama)
    and for providers not found in the canonical mapping.
    """
    env_var = get_api_key_env(provider)
    if env_var is None:
        return None  # ollama / unknown — no key check possible

    # Key-optional providers (generic OpenAI-compatible / local servers) read the
    # key when present but must never force an interactive prompt.
    spec = OPENAI_COMPATIBLE_PROVIDERS.get(provider.lower())
    if spec is not None and spec.key_optional:
        return os.environ.get(env_var)

    existing = os.environ.get(env_var)
    if existing:
        return existing

    if not sys.stdin.isatty():
        console.print(f"[red]{missing_key_sentence(env_var)}[/red]")
        raise typer.Exit(code=1)

    console.print(
        f"\n[yellow]{unset_key_sentence(env_var)}[/yellow]"
    )
    key = questionary.password(
        paste_key_prompt(env_var),
        style=questionary.Style([
            ("text", "fg:cyan"),
            ("highlighted", "noinherit"),
        ]),
    ).ask()
    if not key:
        console.print(
            f"[red]{skipped_key_sentence(env_var)}[/red]"
        )
        return None

    env_path = find_dotenv(usecwd=True) or str(Path.cwd() / ".env")
    # The file holds credentials, so make it owner-only before writing: create
    # it 0600 when absent, and tighten an existing one (set_key keeps the mode).
    if not os.path.exists(env_path):
        os.close(os.open(env_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    os.chmod(env_path, 0o600)
    set_key(env_path, env_var, key)
    os.environ[env_var] = key
    console.print(f"[green]{saved_key_sentence(env_var, env_path)}[/green]")
    return key


def ask_output_language(default=None) -> str:
    """Ask for report output language.

    ``default`` is offered only when it is one of the listed languages: a custom
    one entered last time is free text, which the menu cannot preselect.
    """
    choices = [questionary.Choice(label, value) for label, value in OUTPUT_LANGUAGE_CHOICES]
    choice = questionary.select(
        "Select Output Language:",
        choices=choices,
        default=_matching_choice([(c.title, c.value) for c in choices], default),
        style=questionary.Style([
            ("selected", "fg:yellow noinherit"),
            ("highlighted", "fg:yellow noinherit"),
            ("pointer", "fg:yellow noinherit"),
        ]),
    ).ask()

    # Output language has a sensible default, so a cancel falls back to English
    # rather than exiting the run (unlike the required model/provider prompts).
    if choice is None:
        return "English"
    if choice == "custom":
        return (questionary.text(
            CUSTOM_LANGUAGE_PROMPT,
            validate=lambda x: len(x.strip()) > 0 or CUSTOM_LANGUAGE_INVALID,
        ).ask() or "").strip() or "English"

    return choice
