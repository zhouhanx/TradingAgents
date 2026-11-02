import sys
from enum import StrEnum
from typing import Annotated

import typer
from rich.markup import escape

from cli.display import console
from cli.models import AnalystType, AssetType
from cli.prompts import filter_analysts_for_asset_type, parse_analysts
from cli.run import run_analysis
from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.openai_client import SIGNED_IN_PROVIDERS
from tradingagents.llm_clients.subscriptions import get_subscription
from tradingagents.portfolio import load_portfolio

# prompt_toolkit's win32 output module is importable only on Windows (it asserts
# the platform at import time), so gate on the platform rather than catching the
# failure — that way a genuinely broken prompt_toolkit on Windows still surfaces
# instead of silently disabling the handler below. Off Windows this stays an
# empty tuple, which `except` accepts and never matches (#1138).
if sys.platform == "win32":  # pragma: no cover - platform dependent
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError

    _NO_CONSOLE_ERRORS: tuple[type[BaseException], ...] = (NoConsoleScreenBufferError,)
else:
    _NO_CONSOLE_ERRORS = ()

app = typer.Typer(
    name="TradingAgents",
    help="TradingAgents CLI: Multi-Agents LLM Financial Trading Framework",
    add_completion=True,  # Enable shell completion
)


@app.callback(invoke_without_command=True)
def analyze(
    ctx: typer.Context,
    checkpoint: bool | None = typer.Option(
        None,
        "--checkpoint/--no-checkpoint",
        help="Enable/disable checkpoint-resume (save state after each node so a "
        "crashed run can resume). Omit to honor TRADINGAGENTS_CHECKPOINT_ENABLED.",
    ),
    clear_checkpoints: bool = typer.Option(
        False,
        "--clear-checkpoints",
        help="Delete all saved checkpoints before running (force fresh start).",
    ),
    portfolio: str = typer.Option(
        None,
        "--portfolio",
        help="JSON file with current holdings and cash, so the trader, risk and "
        "portfolio agents size against your actual position.",
    ),
    ticker: str = typer.Option(None, "--ticker", help="Ticker to analyze, e.g. NVDA or 0700.HK; skips the prompt"),
    date: str = typer.Option(None, "--date", help="Analysis date, YYYY-MM-DD; skips the prompt"),
    analysts: str = typer.Option(
        None, "--analysts", help="Comma-separated analysts, e.g. market,news; skips the prompt"
    ),
    save: bool | None = typer.Option(
        None, "--save/--no-save", help="Save the report under results_dir without asking"
    ),
    show: bool | None = typer.Option(
        None, "--show/--no-show", help="Show the full report at the end without asking"
    ),
    html: bool | None = typer.Option(
        None, "--html/--no-html",
        help="Save the report as one HTML page too, complete_report.html, without asking (default: yes)",
    ),
):
    """Run an analysis. This is what a bare `tradingagents` does.

    Flags answer their questions; with provider, models, depth and language also
    set through TRADINGAGENTS_* variables, the run asks nothing.
    """
    if ctx.invoked_subcommand is not None:
        return
    if clear_checkpoints:
        from tradingagents.graph.checkpointer import clear_all_checkpoints
        n = clear_all_checkpoints(DEFAULT_CONFIG["data_cache_dir"])
        console.print(f"[yellow]{checkpoint_cleared_sentence(n)}[/yellow]")
    portfolio_context = None
    if portfolio:
        try:
            portfolio_context = load_portfolio(portfolio)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None

    try:
        flags = {"ticker": ticker, "date": date, "analysts": analysts, "save": save, "show": show, "html": html}
        run_analysis(checkpoint=checkpoint, portfolio=portfolio_context, flags=flags)
    except _NO_CONSOLE_ERRORS:
        # A terminal with no console buffer cannot host the interactive prompts.
        # Emit one actionable line on stderr instead of a prompt_toolkit
        # traceback; plain text, since rich may not render here either (#1138).
        typer.echo(
            "Error: no Windows console available. The interactive CLI needs a real "
            "console buffer — run it from Windows Terminal, PowerShell, or cmd.exe "
            "rather than a piped or embedded terminal.",
            err=True,
        )
        raise typer.Exit(code=1) from None


@app.command()
def backtest(
    tickers: str = typer.Argument(..., help="Comma-separated tickers, e.g. NVDA,AAPL"),
    start: str = typer.Option(..., "--start", help="First analysis date, YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="Last analysis date, YYYY-MM-DD"),
    every: int = typer.Option(7, "--every", help="Days between analysis dates"),
    analysts: str = typer.Option(
        None, "--analysts", help="Comma-separated analysts to run: market, sentiment, news, fundamentals; omit for all the asset type allows"
    ),
    asset_type: str = typer.Option("stock", "--asset-type", help="stock or crypto"),
    portfolio: str = typer.Option(
        None, "--portfolio", help="JSON file with holdings and cash, held constant across the grid"
    ),
    run_id: str = typer.Option(
        None, "--run-id", help="Continue an earlier sweep: its cells are skipped and its log reused"
    ),
):
    """Score past decisions over a grid of tickers and dates."""

    try:
        dates = iter_grid(start, end, every)
        book = load_portfolio(portfolio) if portfolio else None
        kind = AssetType(asset_type.strip().lower())
        # The analysts are named and checked as for an analysis; without a
        # choice, every analyst the asset type allows runs.
        chosen = (parse_analysts(analysts, kind) if analysts
                  else filter_analysts_for_asset_type(list(AnalystType), kind))
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    names = [t.strip() for t in tickers.split(",") if t.strip()]
    if not names:
        console.print(f"[red]{NO_TICKER_SENTENCE}[/red]")
        raise typer.Exit(code=1)

    def show_progress(done, total, ticker, date):
        console.print(f"[dim]{backtest_progress_line(done, total, ticker, date)}[/dim]")

    kwargs = {"asset_type": kind.value, "portfolio": book, "run_id": run_id, "progress": show_progress,
              "selected_analysts": [a.value for a in chosen]}

    try:
        result = run_backtest(names, dates, DEFAULT_CONFIG, **kwargs)
    except Exception as exc:  # a missing key or an unknown analyst is a setup error
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    print_backtest_completion(result)


# The plans `login`, `logout` and `auth-status` take: the providers signed in
# to, not keyed. `SIGNED_IN_PROVIDERS` is generated by the provider registry.
Plan = StrEnum("Plan", {name: name for name in SIGNED_IN_PROVIDERS})




def every_type_error(raw) -> str:
    """The typer message for a non-integer --every. The option itself still uses int."""
    return f"Invalid value for '--every': {raw!r} is not a valid int."


NO_TICKER_SENTENCE = "No ticker to analyze; pass them comma-separated, e.g. NVDA,AAPL"
FAILED_PREFIX = "failed:"
UNSETTLED_PREFIX = "unsettled:"
CHATGPT_SIGNOUT_UNCONFIRMED = (
    "Signed out here, but OpenAI could not be reached to revoke the sign-in; "
    "it stays valid at OpenAI until it expires."
)


def checkpoint_cleared_sentence(count: int) -> str:
    return f"Cleared {count} checkpoint(s)."


def backtest_progress_line(done, total, ticker, date) -> str:
    return f"[{done}/{total}] {ticker} {date}"


def ran_cells_sentence(cells_run, skipped, log_path) -> str:
    return f"Ran {cells_run} cells, skipped {skipped}. Log: {log_path}"


def continue_sweep_sentence(run_id) -> str:
    return f"Continue or settle this sweep: --run-id {run_id}"


def failed_cell_sentence(ticker, date, reason) -> str:
    return f"{FAILED_PREFIX} {ticker} {date}: {reason}"


def unsettled_cell_sentence(ticker, reason) -> str:
    return f"{UNSETTLED_PREFIX} {ticker}: {reason}"


def backtest_followup_lines(result) -> list[str]:
    """Visible lines printed after summarize().render()."""
    lines = [
        ran_cells_sentence(result.cells_run, result.skipped, result.log_path),
        continue_sweep_sentence(result.run_id),
    ]
    lines.extend(failed_cell_sentence(ticker, date, reason) for ticker, date, reason in result.failures)
    lines.extend(unsettled_cell_sentence(ticker, reason) for ticker, reason in result.settlement_failures)
    return lines


def backtest_completion_text(result) -> str:
    """Plain text of the summary, the blank line, and the follow-up lines."""
    parts = [summarize(result).render(), ""]
    parts.extend(backtest_followup_lines(result))
    return "\n".join(parts) + "\n"


def print_backtest_completion(result) -> None:
    """Print the completion the same way, keeping the failed and unsettled colors."""
    console.print(summarize(result).render())
    console.print()
    for line in backtest_followup_lines(result):
        if line.startswith(FAILED_PREFIX):
            console.print(f"[yellow]{FAILED_PREFIX}[/yellow]{line[len(FAILED_PREFIX):]}")
        elif line.startswith(UNSETTLED_PREFIX):
            console.print(f"[yellow]{UNSETTLED_PREFIX}[/yellow]{line[len(UNSETTLED_PREFIX):]}")
        else:
            console.print(line)


def signed_in_sentence(label: str, email: str) -> str:
    return f"Signed in to {label} as {email}."


def signed_out_sentence(label: str) -> str:
    return f"Signed out of {label}."


def local_signout_unconfirmed(label: str) -> str:
    return f"Signed out of {label} locally; server revocation was not confirmed."


def not_signed_in_sentence(label: str) -> str:
    return f"Not signed in to {label}."


def auth_status_error_sentence(label: str, error: str) -> str:
    return (
        f"{label}: could not read local sign-in status: {error} "
        "(local status only; no live validation)."
    )


def auth_status_signed_in_sentence(label: str, account: str) -> str:
    return (
        f"{label}: signed in as {account} "
        "(local status only; sign-in was not validated live)."
    )


def auth_status_absent_sentence(label: str) -> str:
    return f"{label}: not signed in (local status only; no live validation)."


def _subscription_label(plan: str, *, include_plan: bool = False) -> str:
    """Return the concise user-facing name for a subscription.

    Registry display names are suitable for the provider picker (for example,
    ``ChatGPT (Plus or Pro plan)``), while existing ChatGPT CLI messages use
    the shorter ``ChatGPT`` label.
    """
    spec = get_subscription(plan)
    name = spec.display_name.split(" (", 1)[0]
    if include_plan:
        # ChatGPT's established wording says "plan". Other subscriptions may
        # not represent the same plan entitlement, so keep their registry
        # label (for Grok this is currently "Grok (subscription)").
        return f"{name} plan" if plan.lower() == "chatgpt" else spec.display_name
    return name


@app.command()
def login(plan: Annotated[Plan, typer.Argument(help="The plan to sign in with")]):
    """Sign in to a subscription provider on this machine."""
    spec = get_subscription(plan.value)

    try:
        # One unwrapped line, so the sign-in address can be copied whole.
        email = spec.auth.sign_in(
            say=lambda line: console.print(line, soft_wrap=True, markup=False, highlight=False))
    except spec.auth_error as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[green]{escape(signed_in_sentence(_subscription_label(plan.value), str(email)))}[/green]")


@app.command()
def logout(plan: Annotated[Plan, typer.Argument(help="The plan to sign out of")]):
    """Sign out of a subscription provider on this machine."""
    spec = get_subscription(plan.value)

    try:
        outcome = spec.auth.sign_out()
    except spec.auth_error as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from None
    label = _subscription_label(plan.value)
    if outcome == "revoked":
        console.print(signed_out_sentence(label))
    elif outcome == "forgotten":
        if plan.value == "chatgpt":
            # Keep the established ChatGPT wording for existing scripts/users.
            console.print(f"[yellow]{CHATGPT_SIGNOUT_UNCONFIRMED}[/yellow]")
        else:
            console.print(f"[yellow]{local_signout_unconfirmed(label)}[/yellow]")
    else:
        console.print(not_signed_in_sentence(label))


def local_sign_in(spec) -> tuple[str | None, str | None]:
    """(account, read problem) from the local sign-in file only, as both
    ``auth-status`` and the WebUI report it (CLI-001 / QA W5).

    The file's shape is checked first with the provider's ``auth_file_problem``
    (the shared schema validator), so a damaged or ill-shaped file, including
    NaN/inf token timestamps, is a read failure, never "not signed in" or
    "signed in". Nothing is validated online.
    """
    probe = getattr(spec.auth, "auth_file_problem", None)
    if probe is not None:
        try:
            problem = probe()
        except Exception as exc:  # a status probe must not produce a traceback
            problem = type(exc).__name__
        if problem:
            return None, str(problem)
    try:
        account = spec.auth.signed_in_as()
    except Exception as exc:  # local status should never produce a traceback
        return None, type(exc).__name__
    return (str(account) if account else None), None


@app.command("auth-status")
def auth_status(
    plan: Annotated[Plan | None, typer.Argument(help="A subscription to inspect; omit for all")] = None,
):
    """Show local sign-in status without validating credentials live.

    This deliberately calls only ``signed_in_as``. A positive result means
    local sign-in material exists, not that the provider is reachable now.
    """
    plans = (plan.value,) if plan is not None else tuple(SIGNED_IN_PROVIDERS)
    unreadable = False
    for key in plans:
        spec = get_subscription(key)
        label = _subscription_label(key)
        account, problem = local_sign_in(spec)
        if problem:
            console.print(escape(auth_status_error_sentence(label, problem)))
            unreadable = True
            continue
        if account:
            console.print(escape(auth_status_signed_in_sentence(label, str(account))))
        else:
            console.print(auth_status_absent_sentence(label))
    if unreadable:
        # A sign-in file that exists but cannot be used is not a clean status (W5).
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
