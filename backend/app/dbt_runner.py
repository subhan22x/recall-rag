from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from .config import Settings

ROOT = Path(__file__).resolve().parents[1]
DBT_DIR = ROOT / "dbt"


def run_dbt_build(settings: Settings) -> dict[str, object]:
    """Run the checked-in dbt project against the local development database."""
    parsed = urlparse(settings.database_url)
    default_port = 5433 if parsed.hostname in {None, "localhost", "127.0.0.1"} else 5432
    env = {
        **os.environ,
        "RECALLOPS_DB_HOST": parsed.hostname or "localhost",
        "RECALLOPS_DB_PORT": str(parsed.port or default_port),
        "RECALLOPS_DB_USER": parsed.username or "recallops",
        "RECALLOPS_DB_PASSWORD": parsed.password or "recallops",
        "RECALLOPS_DB_NAME": (parsed.path or "/recallops").lstrip("/"),
    }
    dbt_executable = Path(sys.executable).with_name("dbt")
    command = [str(dbt_executable) if dbt_executable.exists() else "dbt", "build", "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR)]
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=90,
    )
    return {
        "ok": result.returncode == 0,
        "return_code": result.returncode,
        "output": (result.stdout + result.stderr)[-6000:],
    }
