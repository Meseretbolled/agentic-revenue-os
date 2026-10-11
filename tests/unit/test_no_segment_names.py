"""Extensibility rule: segments are config packs, never code."""

import re
from pathlib import Path

import pytest

from tests.conftest import PACKS_DIR

ROOT = Path(__file__).resolve().parents[2]
PRODUCT_CODE = [ROOT / "revos", ROOT / "scripts"]        # every Python file that is not a test
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
    for path in sorted(p for root in PRODUCT_CODE for p in root.rglob("*.py")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert not hits, "segment names found in code:\n" + "\n".join(hits)


def test_the_guard_would_catch_a_violation(tmp_path) -> None:
    sneaky = tmp_path / "x.py"
    sneaky.write_text('if segment == "franchise_brands":\n    pass\n')
    pattern = re.compile("|".join(re.escape(w) for w in forbidden_words()), re.IGNORECASE)
    assert pattern.search(sneaky.read_text())


def test_every_pack_id_and_its_words_are_on_the_forbidden_list() -> None:
    words = forbidden_words()
    for pack in PACKS_DIR.iterdir():
        if pack.is_dir():
            assert pack.name in words and all(w in words for w in pack.name.split("_") if len(w) > 4)


def test_naming_a_segment_in_a_python_file_fails_the_real_scan() -> None:
    """Break the rule on purpose in a scratch module under revos/ and run the real scan: it must fail."""
    scratch = ROOT / "revos" / "_scratch_segment_name_probe.py"
    try:
        scratch.write_text('SEGMENT = "' + sorted(p.name for p in PACKS_DIR.iterdir() if p.is_dir())[0] + '"\n')
        with pytest.raises(AssertionError, match="segment names found in code"):
            test_no_python_file_names_a_segment()
    finally:
        scratch.unlink(missing_ok=True)
