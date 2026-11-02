"""CLI live-view roles, with Chinese labels for the console.

The English agent names are the same strings the CLI progress panel uses.
"""

from __future__ import annotations

import re

from cli.run import ANALYSIS_COMPLETE, REVIEW_SENTENCE

TEAM_ZH = {
    "Analyst Team": "分析师团队",
    "Research Team": "研究团队",
    "Trading Team": "交易团队",
    "Risk Management": "风险管理",
    "Portfolio Management": "组合管理",
}
AGENT_ZH = {
    "Market Analyst": "市场分析师",
    "Sentiment Analyst": "情绪分析师",
    "News Analyst": "新闻分析师",
    "Fundamentals Analyst": "基本面分析师",
    "Bull Researcher": "多头研究员",
    "Bear Researcher": "空头研究员",
    "Research Manager": "研究经理",
    "Trader": "交易员",
    "Aggressive Analyst": "激进分析师",
    "Neutral Analyst": "中性分析师",
    "Conservative Analyst": "保守分析师",
    "Portfolio Manager": "组合经理",
}


def _teams():
    """English names come from the CLI progress panel, not a second list."""
    from cli.display import LIVE_PROGRESS_TEAMS

    rows = []
    for team, agents in LIVE_PROGRESS_TEAMS:
        if team not in TEAM_ZH:
            raise KeyError(team)
        built = []
        for agent, analyst in agents:
            if agent not in AGENT_ZH:
                raise KeyError(agent)
            built.append((agent, AGENT_ZH[agent], analyst))
        rows.append((team, TEAM_ZH[team], tuple(built)))
    return tuple(rows)


TEAMS = _teams()

STATUS_ZH = {
    "pending": "等待",
    "in_progress": "进行中",
    "completed": "完成",
    "error": "错误",
}


_BACKTEST_CELL = re.compile(r"^\[(\d+)/(\d+)\] (\S+) (\d{4}-\d{2}-\d{2})$")


def parse_backtest_lines(lines: list[str]) -> dict | None:
    """Read the same progress sentences the CLI prints during a backtest."""
    latest = None
    summary = []
    for line in lines:
        match = _BACKTEST_CELL.match(line.strip())
        if match:
            done, total, ticker, date = match.groups()
            latest = {
                "done": int(done),
                "total": int(total),
                "ticker": ticker,
                "date": date,
                "cli": line.strip(),
            }
            continue
        if line.startswith(("Ran ", "Continue or settle", "failed:", "unsettled:")):
            summary.append(line)
    if latest is None and not summary:
        return None
    return {"latest": latest, "summary": summary}


def _clock(value: str) -> bool:
    return len(value) == 8 and value[2] == ":" and value[5] == ":" and value.replace(":", "").isdigit()


def parse_message_lines(lines: list[str]) -> list[dict]:
    """Recent CLI message-panel rows, newest last."""
    found = []
    for line in lines:
        if not line.startswith("MESSAGE\t"):
            continue
        parts = line.split("\t")
        if len(parts) >= 4 and _clock(parts[-1]):
            found.append({"time": parts[-1], "type": parts[1], "content": "\t".join(parts[2:-1])})
        elif len(parts) >= 3:
            found.append({"type": parts[1], "content": "\t".join(parts[2:])})
    return found[-12:]


def message_panel_rows(lines: list[str]) -> list[dict]:
    """The CLI message panel: newest 12 of messages and tool rows, newest first."""
    rows = []
    for index, line in enumerate(lines):
        if line.startswith("MESSAGE\t"):
            parts = line.split("\t")
            if len(parts) >= 4 and _clock(parts[-1]):
                rows.append({"order": index, "time": parts[-1], "type": parts[1], "content": "\t".join(parts[2:-1])})
            elif len(parts) >= 3:
                rows.append({"order": index, "time": "", "type": parts[1], "content": "\t".join(parts[2:])})
        elif line.startswith("TOOL\t"):
            parts = line.split("\t")
            if len(parts) >= 4 and _clock(parts[-1]):
                name, args, stamp = parts[1], "\t".join(parts[2:-1]), parts[-1]
            elif len(parts) >= 3:
                name, args, stamp = parts[1], "\t".join(parts[2:]), ""
            else:
                continue
            rows.append({"order": index, "time": stamp, "type": "Tool", "content": f"{name}: {args}"})
    rows.sort(key=lambda item: item["order"], reverse=True)
    return [{key: value for key, value in item.items() if key != "order"} for item in rows[:12]]


REVIEW_NOTICE = REVIEW_SENTENCE


def review_notice(rating: str | None) -> str | None:
    """The CLI warning printed when a finished run has no tradeable rating."""
    from tradingagents.agents.rating import is_review

    if not rating or not is_review(str(rating)):
        return None
    return REVIEW_NOTICE


def parse_review_line(lines: list[str]) -> str | None:
    found = None
    prefix = "REVIEW" + "\t"
    for line in lines:
        if line.startswith(prefix):
            found = line.split("\t", 1)[1]
    return found


COMPLETION_SENTENCE = ANALYSIS_COMPLETE


def parse_completion(lines: list[str]) -> str | None:
    """The CLI completion line, only when that exact sentence was printed."""
    for line in lines:
        if line.strip() == COMPLETION_SENTENCE:
            return COMPLETION_SENTENCE
    return None


def parse_wall_time(lines: list[str]) -> str | None:
    found = None
    for line in lines:
        if line.startswith("Analyst wall time:"):
            found = line
    return found


def parse_stats_line(lines: list[str]) -> str | None:
    found = None
    for line in lines:
        if line.startswith("STATS\t"):
            found = line.split("\t", 1)[1]
    return found


def parse_tool_lines(lines: list[str]) -> list[dict]:
    """Recent tool calls, newest last, matching the CLI message panel's Tool rows."""
    found = []
    for line in lines:
        if not line.startswith("TOOL\t"):
            continue
        parts = line.split("\t")
        if len(parts) >= 4 and _clock(parts[-1]):
            found.append({"name": parts[1], "args": "\t".join(parts[2:-1]), "time": parts[-1]})
        elif len(parts) >= 3:
            found.append({"name": parts[1], "args": "\t".join(parts[2:])})
        else:
            continue
    return found[-12:]


def parse_status_lines(lines: list[str]) -> dict[str, str]:
    """The latest status per role. A line is ``STATUS<TAB>agent<TAB>status`` with an
    optional ``<TAB>HH:MM:SS`` stamp (older logs have none)."""
    return {event["agent"]: event["status"] for event in status_events(lines)}


def status_events(lines: list[str]) -> list[dict]:
    """Every role status change in log order, with its clock time when the line has one."""
    events = []
    for line in lines:
        if not line.startswith("STATUS\t"):
            continue
        parts = line.split("\t")
        if len(parts) == 4 and _clock(parts[3]):
            _, agent, status, stamp = parts
        elif len(parts) == 3:
            _, agent, status = parts
            stamp = ""
        else:
            continue
        if status in STATUS_ZH:
            events.append({"agent": agent, "status": status, "time": stamp})
    return events


def _seconds(stamp: str) -> int | None:
    if not stamp or not _clock(stamp):
        return None
    hours, minutes, seconds = (int(part) for part in stamp.split(":"))
    return hours * 3600 + minutes * 60 + seconds


def _span(start: str, end: str) -> str:
    """m:ss between two clock stamps; a run past midnight wraps once."""
    first, last = _seconds(start), _seconds(end)
    if first is None or last is None:
        return ""
    total = (last - first) % 86400
    return f"{total // 60}:{total % 60:02d}"


def progress_timeline(lines: list[str] | None = None, selected: list[str] | None = None, limit: int = 12) -> list[dict]:
    """The status page's recent activity: real role status changes, newest first.

    Pending is the starting state, so it is not news. Only roles shown in the
    pipeline are listed (an unselected analyst never appears).
    """
    names = {}
    chosen = set(selected or [])
    for _team, team_zh, agents in TEAMS:
        for agent, zh, analyst in agents:
            if analyst and chosen and analyst not in chosen:
                continue
            names[agent] = (zh, team_zh)
    rows = []
    for event in status_events(lines or []):
        if event["status"] == "pending" or event["agent"] not in names:
            continue
        zh, team_zh = names[event["agent"]]
        rows.append({**event, "zh": zh, "team_zh": team_zh, "status_zh": STATUS_ZH[event["status"]]})
    rows.reverse()
    return rows[:limit]


def progress_rows(lines: list[str] | None = None, selected: list[str] | None = None) -> list[dict]:
    events = status_events(lines or [])
    found = {event["agent"]: event["status"] for event in events}
    started: dict[str, str] = {}
    finished: dict[str, str] = {}
    for event in events:
        if event["status"] == "in_progress" and event["time"]:
            started.setdefault(event["agent"], event["time"])
        elif event["status"] in ("completed", "error") and event["time"]:
            finished[event["agent"]] = event["time"]
    chosen = set(selected or [])
    rows = []
    for team, team_zh, agents in TEAMS:
        for agent, zh, analyst in agents:
            if analyst and chosen and analyst not in chosen:
                continue
            status = found.get(agent, "pending")
            begin = started.get(agent, "")
            end = finished.get(agent, "") if status in ("completed", "error") else ""
            rows.append({
                "team": team,
                "team_zh": team_zh,
                "agent": agent,
                "zh": zh,
                "status": status,
                "status_zh": STATUS_ZH[status],
                "started": begin,
                "finished": end,
                "elapsed": _span(begin, end) if begin and end else "",
            })
    return rows

def format_report_line(text: str) -> str:
    """One log line carrying the CLI Current Report panel body."""
    escaped = text.replace('\\', '\\' * 2).replace('\n', '\\' + "n").replace("\t", '\\' + "t")
    return "REPORT" + "\t" + escaped


def parse_report_line(lines: list[str]) -> str | None:
    """The latest Current Report body, or None before the first section."""
    found = None
    prefix = "REPORT" + "\t"
    for line in lines:
        if line.startswith(prefix):
            found = line[len(prefix):]
    if found is None:
        return None
    out = []
    index = 0
    while index < len(found):
        if found[index] == '\\' and index + 1 < len(found):
            code = found[index + 1]
            out.append({"n": "\n", "t": "\t", '\\': '\\'}.get(code, code))
            index += 2
        else:
            out.append(found[index])
            index += 1
    return "".join(out)
