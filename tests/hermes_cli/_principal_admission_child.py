"""Subprocess fixture for principal-admission tests; not production code."""
from __future__ import annotations

import json
import os
import subprocess
import sys

from hermes_cli.kanban_principal import (
    _LAUNCH_CAPABILITY_ENV,
    admit_captured_launch,
    capture_startup_capability,
    current_principal,
)


def main() -> int:
    captured = capture_startup_capability()
    principal = admit_captured_launch(captured)
    late = subprocess.run(
        [sys.executable, "-c", f"import os; print(int({repr(_LAUNCH_CAPABILITY_ENV)} in os.environ))"],
        check=True,
        capture_output=True,
        text=True,
    )
    current = current_principal()
    print(json.dumps({
        "admitted": principal is not None,
        "current": current is principal and principal is not None,
        "env_removed": _LAUNCH_CAPABILITY_ENV not in os.environ,
        "late_child_has_secret": late.stdout.strip() == "1",
        "profile": current.profile if current is not None else None,
        "task_id": current.task_id if current is not None else None,
        "run_id": current.run_id if current is not None else None,
        "has_raw_attribute": hasattr(current, "capability") if current is not None else False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
