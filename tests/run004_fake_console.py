"""RUN-004 harness: the real console server with the fake worker in place of webui.worker.

``python -m tests.run004_fake_console --port 0`` runs ``webui.server.main``
(so the restart recovery and the queue dispatcher run exactly as in production);
only two seams change: runs are spawned as ``tests.run004_fake_worker`` and no
subscription sign-in is required, because nothing calls a model.
"""

from __future__ import annotations

import sys

from webui import server, service


def main(argv: list[str]) -> int:
    service.set_spawn_argv(lambda job: [sys.executable, "-m", "tests.run004_fake_worker", job["job_path"]])
    service._require_credential = lambda provider: None
    return server.main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
