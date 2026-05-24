"""Static AST audit of event publisher call sites.

Walks the entire in-tree source (``abides-core/``, ``abides-markets/``,
``abides-gym/``) and inspects every call to ``logEvent``,
``publish_event``, ``publish_metric`` and ``publish_book_snapshot``.

Two strict invariants are enforced:

1. **Registered event types** — when the first positional argument is a
   string literal, the literal must be a key of
   :data:`abides_core.event_payloads.EVENT_TYPE_SCHEMA`. Dynamic
   expressions (``BinOp``, ``Name``, ``Attribute``, ``Call``) are
   skipped: dynamic event names exist (e.g. ``message.type()``, the
   ``<tag>_POST_ONLY`` order-book event, parameterised metric keys) and
   the registry already lists the values dynamic call sites can emit.

2. **No f-string / ``str(x)`` payload literals** — the second positional
   argument (the payload) must never be an :class:`ast.JoinedStr` (an
   f-string) or a literal ``str(x)`` call. Tuple/dict/Constant payloads
   and arbitrary expressions are allowed.

The audit is fatal: any violation fails the build with file/line
context so the offender can be fixed before merge.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from abides_core.telemetry.event_payloads import EVENT_TYPE_SCHEMA, EventType
from abides_core.utils import parse_logs_df

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = (
    REPO_ROOT / "abides-core" / "abides_core",
    REPO_ROOT / "abides-markets" / "abides_markets",
    REPO_ROOT / "abides-gym" / "abides_gym",
)

PUBLISHER_NAMES = frozenset(
    {
        "logEvent",
        "publish_event",
        "publish_metric",
        "publish_book_snapshot",
    }
)


def _iter_python_files() -> Iterator[Path]:
    for root in SOURCE_ROOTS:
        if not root.exists():
            continue
        yield from root.rglob("*.py")


def _collect_violations() -> tuple[list[str], list[str]]:
    """Return ``(unregistered_event_types, bad_payload_literals)``."""
    unregistered: list[str] = []
    bad_payloads: list[str] = []

    for py_file in _iter_python_files():
        try:
            source = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            else:
                continue
            if name not in PUBLISHER_NAMES:
                continue
            if not node.args:
                continue

            # ---- Invariant 1: literal event_type must be registered ----
            first = node.args[0]
            if (
                isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and first.value not in EVENT_TYPE_SCHEMA
            ):
                rel = py_file.relative_to(REPO_ROOT)
                unregistered.append(
                    f"{rel}:{first.lineno}: {name}({first.value!r}, ...) -- "
                    f"event_type not in EVENT_TYPE_SCHEMA"
                )

            # ---- Invariant 2: payload must not be f-string / str(x) ----
            if len(node.args) >= 2:
                payload = node.args[1]
                if isinstance(payload, ast.JoinedStr):
                    rel = py_file.relative_to(REPO_ROOT)
                    bad_payloads.append(
                        f"{rel}:{payload.lineno}: {name}(...) payload is an "
                        f"f-string; use a positional tuple matching the "
                        f"PayloadSchema instead"
                    )
                elif (
                    isinstance(payload, ast.Call)
                    and isinstance(payload.func, ast.Name)
                    and payload.func.id == "str"
                ):
                    rel = py_file.relative_to(REPO_ROOT)
                    bad_payloads.append(
                        f"{rel}:{payload.lineno}: {name}(...) payload is "
                        f"str(x); pass the structured value directly"
                    )

    return unregistered, bad_payloads


class TestEventPayloadSchemaAudit:
    """Fail the build on any structural drift between publishers and the registry."""

    def test_every_static_event_type_has_schema(self) -> None:
        missing = [
            event_type
            for event_type in EventType
            if event_type not in EVENT_TYPE_SCHEMA
        ]
        assert (
            not missing
        ), "EventType members missing EVENT_TYPE_SCHEMA entries: " + ", ".join(
            event_type.value for event_type in missing
        )

    def test_order_rejected_schema_matches_logged_payload(self) -> None:
        schema = EVENT_TYPE_SCHEMA[EventType.ORDER_REJECTED]
        assert schema.name == "ORDER_REJECTION"
        assert schema.fields == ("order_id", "reason")

    def test_parse_logs_df_expands_order_rejected_payload(self) -> None:
        agent = SimpleNamespace(
            id=1,
            type="TestAgent",
            log=[(1000, EventType.ORDER_REJECTED, (42, "INVALID_PRICE"))],
        )

        logs = parse_logs_df([agent])

        assert logs.loc[0, "EventType"] == EventType.ORDER_REJECTED
        assert logs.loc[0, "order_id"] == 42
        assert logs.loc[0, "reason"] == "INVALID_PRICE"

    @pytest.fixture(scope="class")
    def violations(self) -> tuple[list[str], list[str]]:
        return _collect_violations()

    def test_every_literal_event_type_is_registered(
        self, violations: tuple[list[str], list[str]]
    ) -> None:
        unregistered, _ = violations
        assert not unregistered, (
            "Unregistered event_type literals found at publisher call sites. "
            "Add them to EVENT_TYPE_SCHEMA in "
            "abides_core/event_payloads.py:\n  " + "\n  ".join(unregistered)
        )

    def test_no_fstring_or_str_payload_literals(
        self, violations: tuple[list[str], list[str]]
    ) -> None:
        _, bad_payloads = violations
        assert not bad_payloads, (
            "Publisher call sites must not pass f-string or str(x) payloads; "
            "use a positional tuple matching the registered PayloadSchema:\n  "
            + "\n  ".join(bad_payloads)
        )
