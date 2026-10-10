"""Extensibility rule: segments are config packs, never code."""

import re
from pathlib import Path

from tests.conftest import PACKS_DIR

REVOS = Path(__file__).resolve().parents[2] / "revos"
NAMED_SEGMENTS = ["franchise", "agencies", "agency", "publisher"]


def forbidden_words() -> list[str]:
    words = list(NAMED_SEGMENTS)
    for pack in PACKS_DIR.iterdir():
        if pack.is_dir():
            words.append(pack.name)                       # every pack id
            words += [w for w in pack.name.split("_") if len(w) > 4]
    return sorted(set(words))


def test_no_python_file_names_a_segment() -> None:
    pattern = re.compile("|".join(re.escape(w) for w in forbidden_words()), re.IGNORECASE)
    hits = []
    for path in sorted(REVOS.rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(REVOS.parent)}:{number}: {line.strip()}")
    assert not hits, "segment names found in code:\n" + "\n".join(hits)


def test_the_guard_would_catch_a_violation(tmp_path) -> None:
    sneaky = tmp_path / "x.py"
    sneaky.write_text('if segment == "franchise_brands":\n    pass\n')
    pattern = re.compile("|".join(re.escape(w) for w in forbidden_words()), re.IGNORECASE)
    assert pattern.search(sneaky.read_text())
