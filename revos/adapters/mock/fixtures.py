"""Fictional fixture data for the mock adapters. Data lives in YAML, not in code."""

from pathlib import Path
from typing import Any

import yaml

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict[str, Any]:
    return yaml.safe_load((FIXTURES / f"{name}.yaml").read_text())
