"""W03 commit-surface contract shared by every production write-path test.

W00 first recorded the eight surfaces and six known ordering gaps.  W03 has now
closed them. Seven rows execute the same coordinator boundary; the former MCP
confirmation write ingress is now a protected denial row because an AI transport
must never approve its own pending command.

The common commit boundary is:

    mutate -> save -> append -> publish -> return

``mutate`` means the command handler or history controller has applied the document
change.  ``save`` and ``append`` are the two currently separate durable writes.
``publish`` is the collaborator-visible event.  Idempotency completion is an HTTP
wrapper concern rather than a step shared by all eight surfaces, so W00 records it
separately in the accompanying baseline note instead of weakening this common order.
"""
from __future__ import annotations

from dataclasses import dataclass


TARGET_COMMIT_ORDER = ("mutate", "save", "append", "publish", "return")
HTTP_TARGET_COMMIT_ORDER = (
    "mutate",
    "save",
    "append",
    "publish",
    "action_return",
    "idempotency_success",
    "return",
)


@dataclass(frozen=True)
class CommitSurface:
    name: str
    test_node: str
    known_gap: str = ""
    target_order: tuple[str, ...] = TARGET_COMMIT_ORDER
    writes: bool = True


SURFACE_MATRIX = (
    CommitSurface(
        "http_command",
        "sdk/test_server_finalize_order.py::test_http_command_commits_before_publish",
        target_order=HTTP_TARGET_COMMIT_ORDER,
    ),
    CommitSurface(
        "http_confirm",
        "sdk/test_server_finalize_order.py::test_http_confirm_commits_before_publish",
        target_order=HTTP_TARGET_COMMIT_ORDER,
    ),
    CommitSurface(
        "mcp_command",
        "sdk/test_mcp_commit_order.py::test_mcp_command_commits_before_publish",
    ),
    CommitSurface(
        "mcp_confirm_denied",
        "sdk/test_mcp_commit_order.py::test_mcp_ai_cannot_self_approve_pending_command",
        target_order=(),
        writes=False,
    ),
    CommitSurface(
        "cli",
        "scripts/test_ze_command_finalize.py::test_cli_command_commits_before_publish",
    ),
    CommitSurface(
        "pipeline_worker",
        "01-core/03-commands-operations/03-pipeline/test_supervisor.py::test_pipeline_worker_commits_before_publish",
    ),
    CommitSurface(
        "undo",
        "sdk/test_server_finalize_order.py::test_undo_route_persists_before_appending_no_double_save",
    ),
    CommitSurface(
        "redo",
        "sdk/test_server_finalize_order.py::test_redo_route_persists_before_append_before_publish",
    ),
)

_BY_NAME = {surface.name: surface for surface in SURFACE_MATRIX}


def assert_commit_order(surface_name: str, events: list[str]) -> None:
    """Apply the single target contract and include the surface in failures."""
    if surface_name not in _BY_NAME:
        raise AssertionError(f"unregistered commit surface: {surface_name}")
    target = _BY_NAME[surface_name].target_order
    assert tuple(events) == target, (
        f"{surface_name} commit order was {tuple(events)!r}; "
        f"target is {target!r}"
    )


def test_commit_surface_matrix_covers_seven_writes_and_the_denied_ai_approval_ingress():
    assert tuple(surface.name for surface in SURFACE_MATRIX) == (
        "http_command",
        "http_confirm",
        "mcp_command",
        "mcp_confirm_denied",
        "cli",
        "pipeline_worker",
        "undo",
        "redo",
    )
    assert len({surface.test_node for surface in SURFACE_MATRIX}) == len(SURFACE_MATRIX)
    assert [surface.name for surface in SURFACE_MATRIX if not surface.writes] == [
        "mcp_confirm_denied"
    ]
    assert _BY_NAME["http_command"].target_order[-3:] == (
        "action_return", "idempotency_success", "return",
    )
    assert _BY_NAME["http_confirm"].target_order[-3:] == (
        "action_return", "idempotency_success", "return",
    )


def test_w03_has_closed_every_recorded_commit_surface_gap():
    assert [surface.name for surface in SURFACE_MATRIX if surface.known_gap] == []
