"""Architecture test: inbound text reaches a model only through UntrustedText.for_model()."""

import ast
from pathlib import Path

REVOS = Path(__file__).resolve().parents[2] / "revos"
PRIVATE = {"_raw", "_visible"}
ALLOWED = {"domain/untrusted.py"}


def test_private_text_of_untrusted_content_is_read_only_inside_its_own_module() -> None:
    offenders = []
    for path in sorted(REVOS.rglob("*.py")):
        rel = path.relative_to(REVOS).as_posix()
        if rel in ALLOWED:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr in PRIVATE:
                offenders.append(f"{rel}:{node.lineno} .{node.attr}")
    assert not offenders, offenders


def test_the_llm_port_takes_untrusted_text_not_strings_for_inbound_content() -> None:
    import inspect

    from revos.ports.llm import LLM
    for name in ("classify_reply", "extract_call_fields"):
        first = list(inspect.signature(getattr(LLM, name)).parameters.values())[1]
        assert first.annotation.__name__ == "UntrustedText" if hasattr(first.annotation, "__name__") \
            else "UntrustedText" in str(first.annotation)
