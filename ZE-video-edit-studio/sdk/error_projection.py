"""Safe transport projection for framework errors.

Internal exceptions deliberately keep paths and repair detail in ``context`` for
operators.  HTTP/MCP are user-facing trust boundaries and must not echo those
details through ``str(error)``.
"""
from __future__ import annotations

import copy
from typing import Any

from . import _paths  # noqa: F401  (side-effect: protocol path injection)
from history_authority import HistoryAuthorityError  # noqa: E402
from pipeline_authority_commit import PipelineAuthorityCommitError  # noqa: E402


_PUBLIC_MESSAGES = {
    "config_version_conflict": "configuration changed; reload and retry",
    "config_write_failed": "configuration could not be saved",
    "config_durability_unknown": "configuration may have been saved; reload before retrying",
    "config_load_invalid": "stored configuration is invalid and requires repair",
    "credential_store_corrupt": "credential storage is invalid and requires repair",
    "credential_store_unavailable": "credential storage is unavailable",
    "credential_store_permissions_invalid": "credential storage permissions require repair",
    "credential_store_write_failed": "credential could not be saved",
    "credential_store_durability_unknown": "credential may have been saved; reload before retrying",
    "credential_key_unavailable": "credential encryption key is unavailable",
    "credential_key_invalid": "credential encryption key requires repair",
    "credential_key_permissions_invalid": "credential key permissions require repair",
    "credential_key_write_failed": "credential encryption key could not be created",
    "credential_key_durability_unknown": "credential key creation has an uncertain outcome",
    "credential_cli_auth_write_failed": "CLI account file could not be saved",
    "credential_cli_auth_durability_unknown": "CLI account file may have been saved; reload before retrying",
    "idempotency_store_corrupt": "request ledger is invalid and requires repair",
    "idempotency_key_required": "this write requires an idempotency key",
    "mutation_rollback_failed": "the write outcome requires operator repair",
    "commit_repair_required": "the pipeline outcome requires repair before replay",
    "confirmation_repair_required": "this confirmation requires operator repair",
    "confirmation_execution_interrupted": "confirmation outcome requires operator repair",
    "confirmation_human_required": "this action must be approved by a human in the web interface",
    "project_topology_conflict": "the target project already belongs to another operation",
    "project_busy": "the project is currently changing; retry with the same intent",
    "project_generation_stale": "the project changed; reload before editing again",
    "project_recovery_required": "the project requires topology repair before it can be changed",
    "history_authority_mismatch": "canonical history changed; reload the project before retrying",
    "pipeline_authority_commit_invalid": "pipeline authority commit is invalid and was not stored",
    "pipeline_authority_root_invalid": "pipeline authority requires the active registered business command",
    "pipeline_authority_seal_required": "pipeline authority must be issued by the framework",
    "pipeline_authority_seal_invalid": "pipeline authority changed after framework issuance",
    "pipeline_start_missing": "pipeline start authority is missing",
    "pipeline_start_ambiguous": "pipeline start authority is ambiguous and requires repair",
    "pipeline_start_prefix_incomplete": "pipeline start history is incomplete and requires repair",
    "pipeline_start_identity_mismatch": "pipeline start identity does not match the committed work",
    "pipeline_start_digest_mismatch": "pipeline start history failed integrity verification",
    "pipeline_control_invalid": "pipeline control does not match the current durable state",
    "pipeline_authority_terminal": "pipeline authority is already terminal",
    "pipeline_authority_stale": "pipeline authority is stale; reload before continuing",
    "pipeline_authority_unavailable": "pipeline authority cannot be verified safely",
    "pipeline_reconcile_required": "pipeline state requires explicit reconciliation before continuing",
    "project_topology_write_failed": "the project change was not completed",
    "project_topology_durability_unknown": "the project may have changed; reconcile this intent before retrying",
    "project_topology_unavailable": "safe project lifecycle coordination is unavailable",
    "guest_session_required": "guest session established; retry the same intent",
}


_RECOVERY_REASON_MESSAGES = {
    # 冻结原因 → 人话(自愈已在启动对账兜过一轮;走到这里=修不了,说清为什么+怎么办)
    "adopt-mismatch": (
        "project identity records disagree and could not be self-healed; "
        "run the topology doctor or restore the project via export/import"
    ),
    "adopt-invalid-document": (
        "the project folder name and its document identity disagree "
        "(often caused by copying/renaming project folders by hand); "
        "run the topology doctor to reconcile, or restore via export/import"
    ),
    "adopt-invalid": "the project folder has no document.json; restore it from a backup",
    "adopt-invalid-tree": "the project folder contains unexpected file types; restore from a backup",
    "adopt-invalid-root": "the project entry is not a real directory; restore from a backup",
}


def public_error_message(error: Any) -> str:
    """Return a stable, path-free message suitable for untrusted clients."""
    code = str(getattr(error, "code", "") or "")
    context = dict(getattr(error, "context", {}) or {})
    if code == "project_recovery_required":
        reason_text = _RECOVERY_REASON_MESSAGES.get(str(context.get("reason") or ""))
        if reason_text:
            return reason_text
    if code == "history_authority_mismatch" and context.get("outcome_unknown") is True:
        return (
            "canonical history outcome is unknown; do not retry this action; "
            "reconcile or repair the project"
        )
    if (
        is_pipeline_authority_commit_error(error) and error.outcome_unknown
    ) or (
        code.startswith("pipeline_")
        and context.get("outcome_unknown") is True
        and context.get("durability_phase") in {
            "commit_outcome",
            "post_write_verification",
        }
    ):
        return (
            "pipeline authority was committed but fresh verification failed; "
            "do not retry this action; reconcile or repair the project"
        )
    if code in _PUBLIC_MESSAGES:
        return _PUBLIC_MESSAGES[code]
    message = getattr(error, "message", None)
    return str(message or code or "request failed")


def is_history_authority_error(error: BaseException) -> bool:
    """Recognize the neutral protocol error without matching code or text."""
    return isinstance(error, HistoryAuthorityError)


def history_outcome_unknown(error: BaseException) -> bool:
    """Return the protocol's typed post-write durability classification."""
    return is_history_authority_error(error) and error.outcome_unknown


def is_pipeline_authority_commit_error(error: BaseException) -> bool:
    """Recognize the private Pipeline durable-authority boundary by type."""
    return isinstance(error, PipelineAuthorityCommitError)


def is_commit_authority_error(error: BaseException) -> bool:
    """Recognize typed canonical-history or Pipeline commit-authority failures."""
    return is_history_authority_error(error) or is_pipeline_authority_commit_error(error)


def commit_outcome_unknown(error: BaseException) -> bool:
    """Classify only typed post-write authority failures as replay-unsafe."""
    return is_commit_authority_error(error) and error.outcome_unknown


def public_commit_authority_context(error: BaseException) -> dict[str, Any]:
    """Return the bounded machine-readable replay facts for CLI/MCP/HTTP."""
    if not is_commit_authority_error(error):
        return {}
    context = dict(getattr(error, "context", {}) or {})
    projected = {
        key: copy.deepcopy(context[key])
        for key in (
            "reason",
            "outcome_unknown",
            "durability_phase",
            "commit_facts",
        )
        if key in context
    }
    return projected


__all__ = [
    "commit_outcome_unknown",
    "history_outcome_unknown",
    "is_commit_authority_error",
    "is_history_authority_error",
    "is_pipeline_authority_commit_error",
    "public_commit_authority_context",
    "public_error_message",
]
