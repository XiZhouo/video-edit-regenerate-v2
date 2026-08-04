from __future__ import annotations

import sys
from pathlib import Path

import pytest

_AUTH_DIR = Path(__file__).resolve().parent
_ENTITY_DIR = _AUTH_DIR.parent.parent / "01-data-structure" / "02-entity"
for _path in (_AUTH_DIR, _ENTITY_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from entity import ZeError
from media_ticket import MediaTicketSigner


SHA_A = "a" * 64
SHA_B = "b" * 64


def _signer(**kwargs) -> MediaTicketSigner:
    return MediaTicketSigner(tool_id="tool-a", master_key=b"k" * 32, **kwargs)


def test_ticket_round_trip_is_opaque_and_bound_to_every_authority_axis():
    signer = _signer(ttl_sec=60)
    assert signer.expires_at(now=1000.9) == 1060
    ticket, expires_at = signer.issue(
        principal_id="alice@example.test", project_id="project-a",
        generation=7, sha256=SHA_A, now=1000,
    )
    assert expires_at == 1060
    assert "alice@example.test" not in ticket
    claims = signer.verify(
        ticket, project_id="project-a", generation=7, sha256=SHA_A, now=1001,
    )
    assert claims.subject == signer.subject_for("alice@example.test")

    for binding in (
        {"project_id": "project-b", "generation": 7, "sha256": SHA_A},
        {"project_id": "project-a", "generation": 8, "sha256": SHA_A},
        {"project_id": "project-a", "generation": 7, "sha256": SHA_B},
    ):
        with pytest.raises(ZeError) as captured:
            signer.verify(ticket, now=1001, **binding)
        assert captured.value.code == "media_ticket_invalid"


def test_ticket_tamper_expiry_and_wrong_deployment_fail_closed():
    signer = _signer(ttl_sec=10)
    ticket, _ = signer.issue(
        principal_id="alice", project_id="project-a", generation=1,
        sha256=SHA_A, now=100,
    )
    tampered = ticket[:-1] + ("A" if ticket[-1] != "A" else "B")
    for candidate, now in ((tampered, 101), (ticket, 110)):
        with pytest.raises(ZeError) as captured:
            signer.verify(
                candidate, project_id="project-a", generation=1,
                sha256=SHA_A, now=now,
            )
        assert captured.value.code == "media_ticket_invalid"

    other = MediaTicketSigner(tool_id="tool-b", master_key=b"k" * 32)
    with pytest.raises(ZeError) as captured:
        other.verify(ticket, project_id="project-a", generation=1, sha256=SHA_A, now=101)
    assert captured.value.code == "media_ticket_invalid"


def test_file_backed_key_is_shared_across_signer_instances_and_bad_key_fails(tmp_path):
    key_path = tmp_path / "credentials.key"
    first = MediaTicketSigner(tool_id="tool-a", key_path=key_path)
    ticket, _ = first.issue(
        principal_id="alice", project_id="project-a", generation=1,
        sha256=SHA_A, now=100,
    )
    second = MediaTicketSigner(tool_id="tool-a", key_path=key_path)
    second.verify(ticket, project_id="project-a", generation=1, sha256=SHA_A, now=101)
    assert key_path.stat().st_mode & 0o777 == 0o600

    bad = tmp_path / "bad.key"
    bad.write_bytes(b"short")
    bad.chmod(0o600)
    with pytest.raises(ZeError) as captured:
        MediaTicketSigner(tool_id="tool-a", key_path=bad)
    assert captured.value.code == "credential_key_invalid"
