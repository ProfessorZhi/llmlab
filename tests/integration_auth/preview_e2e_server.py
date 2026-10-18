"""Subprocess harness: a real preview server for the browser E2E test.

Started by ``browser.preview.test.mjs`` as
``python -m tests.integration_auth.preview_e2e_server``. The whole
configuration arrives as **one JSON line on stdin** — including the generated
administrator password, which therefore never appears in a command line, a
process listing, a file, or this process's output.

It answers with one JSON line on stdout (``{"origin": ..., "port": ...}``) and
then serves until its stdin closes, which is how the test shuts it down.

The fixtures it creates are the ones a real operator would create out of band:
the local administrator (through the same interactive seam as the CLI) and, in
mock mode, the shadow user the offline Feishu mock will sign in as — optionally
with a project grant, so the same flow can be observed landing on the DEMO and
on the no-project refusal.
"""

from __future__ import annotations

import json
import pathlib
import signal
import sys
import threading
from typing import Any

from backend.auth import bootstrap, store

import dev.preview_auth as preview

from tests.integration_auth import support


def _read_config() -> dict[str, Any]:
    line = sys.stdin.readline()
    if not line.strip():
        raise SystemExit("preview_e2e_server: no configuration was supplied on stdin")
    return json.loads(line)


def _shutdown_when_stdin_closes(server) -> None:
    """The parent holding the pipe open *is* the lifetime of this server."""

    def watch() -> None:
        while sys.stdin.readline():
            pass
        server.shutdown()

    threading.Thread(target=watch, daemon=True).start()


def main() -> int:
    config = _read_config()
    state_dir = pathlib.Path(str(config["state_dir"]))
    mock_feishu = bool(config.get("mock_feishu"))

    app = preview.build_preview_application(
        state_dir, local_http_dev=True, mock_feishu=mock_feishu
    )
    # Same seam the CLI uses. There is no password preset anywhere in the product,
    # and this harness does not add one: it feeds the prompt.
    password = str(config["password"])
    bootstrap.initialize_admin_password(
        app.cfg, read_password=support.ScriptedReader(password, password)
    )

    db = app.auth.service.db
    if mock_feishu:
        # The identity the offline mock will sign in as, created ahead of time
        # exactly as an operator would, so the flow can be observed both with
        # and without a project grant.
        member = db.upsert_feishu_user(
            app_id=preview.MOCK_APP_ID,
            open_id=preview.MOCK_OPEN_ID,
            union_id=None,
            tenant_key=preview.MOCK_TENANT_KEY,
            display_name=preview.MOCK_DISPLAY_NAME,
        )
        if config.get("grant_mock_member"):
            db.grant_project(member.id, "synthetic-atlas", store.ROLE_MEMBER)

    server = preview.make_server(app, port=0)
    port = int(server.server_address[1])
    sys.stdout.write(
        json.dumps(
            {
                "origin": f"http://127.0.0.1:{port}",
                "port": port,
                "session_cookie": app.cfg.cookie_name,
            }
        )
        + "\n"
    )
    sys.stdout.flush()

    _shutdown_when_stdin_closes(server)
    try:
        signal.signal(
            signal.SIGTERM,
            lambda *_args: threading.Thread(target=server.shutdown, daemon=True).start(),
        )
    except (ValueError, OSError):  # pragma: no cover - platform dependent
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover - subprocess entry point
    raise SystemExit(main())
