from __future__ import annotations

from types import SimpleNamespace

import server

from pipeline_authority_commit import PipelineAuthorityCommitError
from sdk.error_projection import (
    commit_outcome_unknown,
    is_commit_authority_error,
    public_commit_authority_context,
    public_error_message,
)


def _error(code: str, message: str):
    return SimpleNamespace(code=code, message=message, context={"path": "/srv/private/key"})


def test_sensitive_error_message_never_exposes_internal_path():
    error = _error("credential_store_corrupt", "broken /srv/private/credentials.json")
    message = public_error_message(error)
    assert "/srv/private" not in message
    assert "repair" in message


def test_http_error_projection_filters_message_and_context_path():
    response = server._error_response(
        _error("config_durability_unknown", "failed /srv/private/config.json"),
    )
    body = response.body.decode("utf-8")
    assert response.status_code == 500
    assert "/srv/private" not in body


def test_mutation_rollback_failure_is_server_error():
    response = server._error_response(_error("mutation_rollback_failed", "rollback failed"))
    assert response.status_code == 500


def test_pipeline_post_durable_error_is_replay_unsafe_and_path_free():
    error = PipelineAuthorityCommitError(
        "pipeline_authority_unavailable",
        "pipeline_authority_fresh_scan_failed",
        "failed to read /srv/private/oplog.ndjson",
        outcome_unknown=True,
        durability_phase="post_write_verification",
    ).with_commit_facts({"committed": True, "document_durable": True})

    assert is_commit_authority_error(error) is True
    assert commit_outcome_unknown(error) is True
    assert "/srv/private" not in public_error_message(error)
    assert "do not retry" in public_error_message(error)
    assert public_commit_authority_context(error) == {
        "reason": "pipeline_authority_fresh_scan_failed",
        "outcome_unknown": True,
        "durability_phase": "post_write_verification",
        "commit_facts": {"committed": True, "document_durable": True},
    }

    projected = server._project_pipeline_authority_error(error)
    response = server._error_response(projected)
    body = response.body.decode("utf-8")
    assert response.status_code == 409
    assert "/srv/private" not in body
    assert "do not retry" in body
    assert '"outcome_unknown":true' in body
    assert '"document_durable":true' in body


def test_pipeline_preflight_error_does_not_poison_idempotency():
    error = PipelineAuthorityCommitError(
        "pipeline_start_identity_mismatch",
        "pipeline_start_document_mismatch",
        "private /srv/project identity detail",
    )
    assert is_commit_authority_error(error) is True
    assert commit_outcome_unknown(error) is False
    assert public_error_message(error) == (
        "pipeline start identity does not match the committed work"
    )

    unavailable = PipelineAuthorityCommitError(
        "pipeline_authority_unavailable",
        "pipeline_authority_fresh_scan_failed",
        "fresh scan unavailable before storage",
    )
    response = server._error_response(
        server._project_pipeline_authority_error(unavailable),
    )
    assert response.status_code == 503


def test_credential_metadata_blocks_server_owned_fields_and_ssrf_targets():
    """服务端自有字段 + 不安全端点一律拒。

    注意边界在 ProviderHub §P5 之后变了:**公网 https 端点是可以登记的** —— 用户自绑个人
    账号本来就要能填自己的网关。安全性不再靠「只准官方 host」,而靠
    is_safe_registerable_endpoint(https/无 userinfo/无 query/非私网环回链路本地)
    加运行时 guard_outbound_host(防 DNS rebinding)。所以这里钉的是**护栏本身**,
    不是「除官方外全拒」那条已经作废的旧契约。
    """
    for metadata in (
        {"is_default": True},                                   # 服务端自有
        {"home_dir": "/tmp/escape"},                            # 服务端自有
        {"bin": "/tmp/attacker"},                               # 服务端自有
        {"base_url": "http://127.0.0.1:8080/internal"},         # 环回
        {"base_url": "https://192.168.1.5/v1"},                 # 私网
        {"base_url": "https://[::1]/v1"},                       # IPv6 环回
        {"base_url": "https://169.254.169.254/latest/meta"},    # 链路本地(云元数据)
        {"base_url": "http://evil.example/v1"},                 # 非 https
        {"base_url": "https://user:pw@evil.example/v1"},        # 带 userinfo
        {"base_url": "https://evil.example/v1?x=1"},            # 带 query
    ):
        try:
            server._credential_metadata_from_request(
                server.PROVIDER_HUPO_GATEWAY, metadata,
            )
        except Exception as exc:
            assert getattr(exc, "code", "") == "validation_failed"
        else:
            raise AssertionError(f"unsafe credential metadata accepted: {metadata}")


def test_public_https_endpoint_is_registerable_after_p5():
    """P5 契约的正面:干净的公网 https 端点可登记(用户自绑自己的网关)。

    这条与上一条成对 —— 上面钉住护栏拦什么,这里钉住护栏**不该**把正常用法也拦掉。
    """
    out = server._credential_metadata_from_request(
        server.PROVIDER_HUPO_GATEWAY, {"base_url": "https://api.example.com/v1"},
    )
    assert out["base_url"] == "https://api.example.com/v1"

    assert server._credential_metadata_from_request(
        server.PROVIDER_HUPO_GATEWAY,
        {"base_url": "https://api.ai.hupo.games/v1"},
    ) == {"base_url": "https://api.ai.hupo.games/v1"}
