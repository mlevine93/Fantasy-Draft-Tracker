"""§1.5 enforcement: there is exactly one path to an order.

A convention that lives in a design document is not an invariant. This test walks the
AST of the whole package and fails the build if anything outside the risk engine can
manufacture a `ProposedOrder`, or if anything outside the execution router calls a
venue's order-placement methods.

It is written to bite *before* the venue clients exist, so the constraint is in place
when Phase 2 adds them rather than being retrofitted after the first violation.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "pmx"

#: The only module permitted to construct an order.
ORDER_FACTORY = "pmx/risk/engine.py"

#: The only module permitted to call a venue's order endpoints, once one exists.
ORDER_SUBMITTER = "pmx/execution/router.py"

#: Method names that place, modify, or cancel orders at a venue.
ORDER_METHODS = frozenset(
    {
        "place_order",
        "post_order",
        "submit_order",
        "create_order",
        "cancel_order",
        "amend_order",
        "replace_order",
    }
)


def python_files() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def relative(path: Path) -> str:
    return path.relative_to(PACKAGE.parent).as_posix()


def test_only_the_risk_engine_constructs_a_proposed_order() -> None:
    offenders: list[str] = []
    for path in python_files():
        if relative(path) == ORDER_FACTORY:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ProposedOrder"
            ):
                offenders.append(f"{relative(path)}:{node.lineno}")
    assert not offenders, (
        "ProposedOrder may only be constructed by the risk engine; found in: "
        + ", ".join(offenders)
    )


def test_only_the_router_calls_venue_order_methods() -> None:
    offenders: list[str] = []
    for path in python_files():
        if relative(path) == ORDER_SUBMITTER:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ORDER_METHODS
            ):
                offenders.append(f"{relative(path)}:{node.lineno} calls .{node.func.attr}()")
    assert not offenders, (
        "venue order methods may only be called from the execution router; found: "
        + ", ".join(offenders)
    )


def test_no_bare_except_in_risk_or_execution() -> None:
    """§1.7: never `except: pass`, and never a swallowed exception in the paths that
    can lose money."""
    offenders: list[str] = []
    for path in python_files():
        rel = relative(path)
        if not (rel.startswith("pmx/risk/") or rel.startswith("pmx/execution/")):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    offenders.append(f"{rel}:{node.lineno} bare except")
                elif all(isinstance(stmt, ast.Pass) for stmt in node.body):
                    offenders.append(f"{rel}:{node.lineno} except: pass")
    assert not offenders, "swallowed exceptions in risk/execution: " + ", ".join(offenders)


def test_no_float_literals_in_money_paths() -> None:
    """A float literal in risk or venue code is how binary rounding gets into money.

    Four modules are exempt, each for a stated reason:
      - `pmx/core/money.py` is the module that rejects floats.
      - `pmx/risk/limits.py` converts YAML floats to Decimal via str.
      - `pmx/venues/transport.py` and `pmx/venues/rate_limits.py` deal only in seconds
        and token counts. They are quarantined into their own modules precisely so this
        rule can stay strict everywhere a price is parsed.
    """
    exempt = {
        "pmx/core/money.py",
        "pmx/risk/limits.py",
        "pmx/venues/transport.py",
        "pmx/venues/rate_limits.py",
    }
    offenders: list[str] = []
    for path in python_files():
        rel = relative(path)
        if rel in exempt:
            continue
        if not (rel.startswith(("pmx/risk/", "pmx/venues/", "pmx/execution/", "pmx/core/"))):
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                offenders.append(f"{rel}:{node.lineno} float literal {node.value!r}")
    assert not offenders, "float literals in money paths: " + ", ".join(offenders)


def test_production_code_never_uses_model_copy() -> None:
    """`model_copy(update=...)` skips pydantic validators.

    Every invariant on `Signal`, `Quote`, and `ProposedOrder` — crossed books, negative
    edge, a quote that describes a different market, max_cost below notional — lives in
    a validator. `model_copy` would let production code construct an object that
    violates all of them and looks completely well-formed. Tests may use it to build
    fixtures; nothing under `pmx/` may.
    """
    offenders: list[str] = []
    for path in python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "model_copy"
            ):
                offenders.append(f"{relative(path)}:{node.lineno}")
    assert not offenders, (
        "model_copy bypasses validators and is forbidden in production code; found in: "
        + ", ".join(offenders)
        + " — construct a new object instead."
    )


def test_risk_engine_never_imports_a_venue_client() -> None:
    """Strategies and the engine must not be able to reach credentials or endpoints."""
    engine = PACKAGE / "risk" / "engine.py"
    tree = ast.parse(engine.read_text(), filename=str(engine))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    forbidden = [
        name
        for name in imported
        if name.startswith("pmx.venues") and not name.endswith("fees")
    ]
    assert not forbidden, f"risk engine imports venue clients: {forbidden}"
