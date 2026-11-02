"""Run one console job in its own process so stop can signal that process."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def execute(job: dict) -> int:
    if job.get("kind") == "backtest":
        from webui.service import run_backtest_job
        return run_backtest_job(job)
    from webui.frameworks import job_framework, runner_for
    return runner_for(job_framework(job))(job)  # W66: the job's framework, never a fixed graph


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m webui.worker JOB.json", file=sys.stderr)
        return 2
    job = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    try:
        return execute(job)
    except Exception as exc:  # noqa: BLE001 - the run is over; say why, redacted
        return report_failure(job, exc)


def report_failure(job: dict, exc: BaseException) -> int:
    """Write ``<run>.failure.json`` and a redacted log line; exit non-zero.

    The status page reads the file to name the failed role, the kind of
    failure, the provider and model, the timeout and attempts (RUN-002).
    """
    from webui.run_failure import build_failure, failure_path, redacted_traceback, write_failure

    failure = build_failure(exc, job)
    if job.get("result_path"):
        write_failure(failure_path(job["result_path"]), failure)
    print(redacted_traceback(exc), file=sys.stderr, flush=True)
    print("FAILURE\t" + json.dumps(failure, ensure_ascii=False), flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
