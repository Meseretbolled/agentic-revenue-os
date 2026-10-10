"""Architecture test: the only road to a sent message is policy.gate.send_with_gate.

Static check over the source tree, so a new module that sends around the gate fails the build.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "revos"
GATE = "policy/gate.py"
OUTBOX = "runtime/outbox.py"
ROUTER = "adapters/router.py"     # a provider that forwards to other providers; only the outbox calls it


def sources():
    for path in sorted(ROOT.rglob("*.py")):
        yield path.relative_to(ROOT).as_posix(), path.read_text()


def test_provider_perform_is_called_only_by_the_outbox() -> None:
    callers = set()
    for name, text in sources():
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "perform":
                callers.add(name)
    assert callers == {OUTBOX, ROUTER}


def test_the_router_is_wired_only_by_composition_code() -> None:
    users = {name for name, text in sources()
             if _mentions(ast.parse(text), "ActionRouter") and name != ROUTER}
    assert users <= {"adapters/mock/__init__.py", "composition.py"}


def _mentions(tree: ast.AST, symbol: str) -> bool:
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id == symbol:
            return True
        if isinstance(n, ast.alias | ast.FunctionDef) and n.name == symbol:
            return True
    return False


def test_execute_action_is_used_only_by_the_gate_and_its_own_module() -> None:
    users = {name for name, text in sources() if _mentions(ast.parse(text), "execute_action")}
    assert users == {OUTBOX, GATE}


def test_only_the_outbox_marks_an_action_sent() -> None:
    writers = {name for name, text in sources() if re.search(r"\bSET\b[^\"]*status\s*=\s*'sent'", text)}
    assert writers == {OUTBOX}
