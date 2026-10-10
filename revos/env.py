"""The app reads its own .env. Nothing here prints a value, and tests never call it."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env_file(path: Path | None = None) -> int:
    """Copy KEY=VALUE lines from .env into the environment, without overriding anything already set.

    Returns how many variables were set (a number, never a name or a value). A no-op when
    REVOS_NO_DOTENV is set, which the test suite does so it never sees real credentials."""
    if os.environ.get("REVOS_NO_DOTENV"):
        return 0
    target = path or ROOT / ".env"
    if not target.is_file():
        return 0
    loaded = 0
    for raw in target.read_text().splitlines():
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        value = value.strip("'\"")
        if name and value and name not in os.environ:
            os.environ[name] = value
            loaded += 1
    return loaded
