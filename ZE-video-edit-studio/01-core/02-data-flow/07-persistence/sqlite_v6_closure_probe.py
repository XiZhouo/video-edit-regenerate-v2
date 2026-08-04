"""Private real-SQL evidence for the schema-v6 effect-closure boundary.

This is an audit fixture, not a writer, reader, migration adapter, or public
SDK surface.  It creates one in-memory repository from the immutable schema
registry, seeds the smallest closed baseline commit, and exercises the final
head barrier and post-finalization membership guards.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Callable, NamedTuple, Tuple

import sqlite_schema as schema


_NOW = "2026-07-12T00:00:00.000Z"


class _ClosureProbeResult(NamedTuple):
    missing_groups: Tuple[str, str, str]
    late_appends: Tuple[str, str, str]
    membership_updates: Tuple[str, str, str]
    membership_deletes: Tuple[str, str, str]


class _ClosureProbeError(RuntimeError):
    pass


def _expect_integrity(
    call: Callable[[], object],
    expected: str,
) -> str:
    try:
        call()
    except sqlite3.IntegrityError as exc:
        if expected not in str(exc):
            raise _ClosureProbeError(
                "expected %r, received %r" % (expected, str(exc))
            ) from None
        return expected
    raise _ClosureProbeError("statement unexpectedly succeeded: " + expected)


def _apply_schema(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        for migration in schema.MIGRATIONS:
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) "
                "VALUES (?,?,?,?)",
                (migration.version, migration.name, migration.checksum, _NOW),
            )
            if migration.version == 1:
                connection.execute(
                    "INSERT INTO kernel_repository(singleton,authority,created_at) "
                    "VALUES (1,'legacy',?)",
                    (_NOW,),
                )
            connection.execute("PRAGMA user_version=%d" % migration.version)
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _insert_action(
    connection: sqlite3.Connection,
    commit_id: str,
    *,
    action_id: str = "2" * 32,
    key: str = "closure-action",
    ordinal: int = 0,
) -> None:
    payload = b'{"action":"closure"}'
    connection.execute(
        """
        INSERT INTO deferred_actions(
            action_id,idempotency_scope,idempotency_key,project_id,generation,
            document_id,origin_commit_id,commit_action_count,ordinal,
            action_kind,action_bytes,action_sha256,payload_schema,codec_id,
            codec_version,replay_policy,idempotency_policy,status,state_revision,
            attempt_count,max_attempts,next_attempt_at,created_at,updated_at
        ) VALUES (
            ?,'closure',?,'project-v6',1,'document-v6',?,1,?,'notify',?,?,
            'action/v1','json',1,'provider_idempotent',
            'required_external_key','queued',1,0,3,?,?,?
        )
        """,
        (
            action_id,
            key,
            commit_id,
            ordinal,
            payload,
            hashlib.sha256(payload).digest(),
            _NOW,
            _NOW,
            _NOW,
        ),
    )


def _insert_pipeline_transition(
    connection: sqlite3.Connection,
    commit_id: str,
    *,
    intent_id: str = "3" * 32,
    key: str = "closure-pipeline-create",
    ordinal: int = 0,
    intent_kind: str = "create",
    before_revision: int | None = None,
    after_revision: int = 1,
    before_status: str | None = None,
    after_status: str = "queued",
) -> None:
    payload = b'{"pipeline":"closure"}'
    connection.execute(
        """
        INSERT INTO pipeline_job_transitions(
            intent_id,origin_commit_id,commit_pipeline_intent_count,ordinal,
            job_id,project_id,generation,document_id,intent_type,
            idempotency_key,intent_kind,before_state_revision,
            after_state_revision,before_status,after_status,intent_bytes,
            intent_sha256,payload_schema,codec_id,codec_version,replay_policy,
            max_attempts,committed_at
        ) VALUES (
            ?,?,1,?,?,'project-v6',1,'document-v6','closure.intent',?,?,?,?,
            ?,?,?,?,'pipeline/v1','json',1,'exact_cas_retry',3,?
        )
        """,
        (
            intent_id,
            commit_id,
            ordinal,
            "3" * 32,
            key,
            intent_kind,
            before_revision,
            after_revision,
            before_status,
            after_status,
            payload,
            hashlib.sha256(payload).digest(),
            _NOW,
        ),
    )


def _insert_asset_ref(
    connection: sqlite3.Connection,
    commit_id: str,
    asset_sha256: bytes,
    *,
    ordinal: int = 0,
) -> None:
    connection.execute(
        """
        INSERT INTO document_asset_refs(
            project_id,generation,document_id,document_version,commit_id,
            commit_asset_reference_count,ordinal,asset_sha256,entity_type,
            entity_id,entity_deleted,value_path,ref_mime,ref_size,ref_ext
        ) VALUES (
            'project-v6',1,'document-v6',1,?,1,?,?,'image','cover',0,
            '/fields/asset','image/png',13,'png'
        )
        """,
        (commit_id, ordinal, asset_sha256),
    )


def _insert_head(
    connection: sqlite3.Connection,
    commit_id: str,
    document_sha256: bytes,
    project_sha256: bytes,
) -> None:
    connection.execute(
        """
        INSERT INTO document_commit_heads(
            project_id,generation,document_id,document_version,commit_id,
            head_revision,status,current_snapshot_sha256,
            current_project_snapshot_sha256,business_root_sha256,
            operation_tail_sha256,asset_projection_sha256,
            asset_reference_count,entity_count,updated_at
        ) VALUES (
            'project-v6',1,'document-v6',1,?,1,'live',?,?,?,?,?,1,0,?
        )
        """,
        (
            commit_id,
            document_sha256,
            project_sha256,
            project_sha256,
            b"t" * 32,
            b"a" * 32,
            _NOW,
        ),
    )


def _seed_commit(
    connection: sqlite3.Connection,
) -> tuple[str, bytes, bytes, bytes]:
    repository_epoch = "e" * 64
    cutover_id = "f" * 64
    commit_id = "1" * 32
    project_payload = b"project-v6-baseline"
    project_sha256 = hashlib.sha256(project_payload).digest()
    document_payload = b"document-v6-baseline"
    document_sha256 = hashlib.sha256(document_payload).digest()
    asset_sha256 = hashlib.sha256(b"closure-asset").digest()

    connection.execute(
        "UPDATE kernel_repository SET authority='sqlite' WHERE singleton=1"
    )
    connection.execute(
        """
        INSERT INTO repository_runtime_control(
            authority,authority_state,repository_epoch,cutover_id,
            source_admission_revision,repository_revision,state_revision,
            runtime_generation,code_digest,schema_version,updated_at
        ) VALUES ('sqlite','active',?,?,7,1,1,'runtime-v6',?,6,?)
        """,
        (repository_epoch, cutover_id, b"c" * 32, _NOW),
    )
    connection.execute(
        """
        INSERT INTO projects(
            project_id,generation,codec_id,codec_version,
            snapshot_format_version,snapshot_bytes,snapshot_sha256
        ) VALUES ('project-v6',1,'json',1,1,?,?)
        """,
        (project_payload, project_sha256),
    )
    connection.execute(
        """
        INSERT INTO documents(
            project_id,document_id,document_version,schema_version,
            collaboration_mode,save_policy,codec_id,codec_version,
            snapshot_format_version,snapshot_bytes,snapshot_sha256
        ) VALUES (
            'project-v6','document-v6',1,'1.0.0','with_save','auto_commit',
            'json',1,1,?,?
        )
        """,
        (document_payload, document_sha256),
    )
    transaction_id = schema.BUSINESS_TRANSACTION_PREFIX + commit_id
    connection.execute(
        "INSERT INTO transaction_receipts(transaction_id,kind,committed_at) "
        "VALUES (?,?,?)",
        (transaction_id, schema.BUSINESS_TRANSACTION_KIND, _NOW),
    )
    manifest = b"{}"
    connection.execute(
        """
        INSERT INTO business_commits(
            commit_id,transaction_id,receipt_kind,project_id,generation,
            repository_revision,project_revision,head_revision,
            repository_epoch,cutover_id,
            source_admission_revision,document_id,document_version,
            previous_commit_id,commit_kind,save_policy,trace_id,
            operation_count,operation_target_count,outbox_event_count,
            durable_action_count,pipeline_intent_count,before_root_sha256,
            after_root_sha256,previous_operation_tail_sha256,
            operation_tail_sha256,asset_projection_sha256,
            asset_reference_count,entity_count,manifest_bytes,manifest_sha256,
            committed_at
        ) VALUES (
            ?,?,?,'project-v6',1,1,1,1,?,?,7,'document-v6',1,NULL,'baseline',
            'auto_commit','closure-probe',0,0,0,1,1,?,?,?,?,?,1,0,?,?,?
        )
        """,
        (
            commit_id,
            transaction_id,
            schema.BUSINESS_TRANSACTION_KIND,
            repository_epoch,
            cutover_id,
            b"0" * 32,
            project_sha256,
            b"0" * 32,
            b"t" * 32,
            b"a" * 32,
            manifest,
            hashlib.sha256(manifest).digest(),
            _NOW,
        ),
    )
    connection.execute(
        """
        INSERT INTO repository_projects(
            project_id,generation,project_revision,status,
            current_snapshot_sha256,created_at,updated_at
        ) VALUES ('project-v6',1,1,'live',?,?,?)
        """,
        (project_sha256, _NOW, _NOW),
    )
    connection.execute(
        """
        INSERT INTO asset_blobs(
            asset_sha256,content_key,media_type,extension,byte_length,status,
            state_revision,created_at,updated_at
        ) VALUES (?,?,'image/png','png',13,'available',1,?,?)
        """,
        (
            asset_sha256,
            "sha256/%s/%s" % (asset_sha256.hex()[:2], asset_sha256.hex()),
            _NOW,
            _NOW,
        ),
    )
    job_payload = b'{"job":"closure"}'
    connection.execute(
        """
        INSERT INTO pipeline_jobs(
            job_id,last_intent_id,idempotency_key,project_id,generation,
            document_id,pipeline_id,intent_type,intent_kind,input_commit_id,
            head_commit_id,job_bytes,job_sha256,payload_schema,codec_id,
            codec_version,replay_policy,status,state_revision,attempt_count,
            max_attempts,next_attempt_at,created_at,updated_at
        ) VALUES (
            ?,?,'closure-pipeline-create','project-v6',1,'document-v6',
            'closure','closure.create','create',?,?,?,?,'pipeline/v1','json',
            1,'exact_cas_retry','queued',1,0,3,?,?,?
        )
        """,
        (
            "3" * 32,
            "3" * 32,
            commit_id,
            commit_id,
            job_payload,
            hashlib.sha256(job_payload).digest(),
            _NOW,
            _NOW,
            _NOW,
        ),
    )
    return commit_id, project_sha256, document_sha256, asset_sha256


def _probe_missing_group(
    connection: sqlite3.Connection,
    label: str,
    expected: str,
    inserts: Tuple[Callable[[], object], ...],
    head: Callable[[], object],
) -> str:
    connection.execute("SAVEPOINT " + label)
    try:
        for insert in inserts:
            insert()
        return _expect_integrity(head, expected)
    finally:
        connection.execute("ROLLBACK TO " + label)
        connection.execute("RELEASE " + label)


def _run_sqlite_v6_closure_probe() -> _ClosureProbeResult:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        _apply_schema(connection)
        connection.execute("BEGIN IMMEDIATE")
        commit_id, project_hash, document_hash, asset_hash = _seed_commit(
            connection
        )
        action = lambda: _insert_action(connection, commit_id)
        pipeline = lambda: _insert_pipeline_transition(connection, commit_id)
        asset = lambda: _insert_asset_ref(connection, commit_id, asset_hash)
        head = lambda: _insert_head(
            connection, commit_id, document_hash, project_hash
        )

        missing = (
            _probe_missing_group(
                connection,
                "missing_action",
                "business_durable_action_count_mismatch",
                (pipeline, asset),
                head,
            ),
            _probe_missing_group(
                connection,
                "missing_pipeline",
                "business_pipeline_intent_count_mismatch",
                (action, asset),
                head,
            ),
            _probe_missing_group(
                connection,
                "missing_asset",
                "business_asset_reference_count_mismatch",
                (action, pipeline),
                head,
            ),
        )

        action()
        pipeline()
        asset()
        head()
        connection.execute(
            """
            UPDATE repository_runtime_control
            SET repository_revision=repository_revision,
                state_revision=2,
                updated_at=?
            WHERE singleton=1
            """,
            (_NOW,),
        )
        connection.execute("COMMIT")

        late = (
            _expect_integrity(
                lambda: _insert_action(
                    connection,
                    commit_id,
                    action_id="4" * 32,
                    key="late-action",
                    ordinal=1,
                ),
                "CHECK constraint failed",
            ),
            _expect_integrity(
                lambda: _insert_pipeline_transition(
                    connection,
                    commit_id,
                    intent_id="5" * 32,
                    key="late-pipeline",
                    ordinal=1,
                    intent_kind="transition",
                    before_revision=1,
                    after_revision=2,
                    before_status="queued",
                    after_status="paused",
                ),
                "CHECK constraint failed",
            ),
            _expect_integrity(
                lambda: _insert_asset_ref(
                    connection, commit_id, asset_hash, ordinal=1
                ),
                "CHECK constraint failed",
            ),
        )
        updates = (
            _expect_integrity(
                lambda: connection.execute(
                    "UPDATE deferred_actions SET action_kind='changed', "
                    "state_revision=2,updated_at=? "
                    "WHERE origin_commit_id=?",
                    (_NOW, commit_id),
                ),
                "business_identity_immutable",
            ),
            _expect_integrity(
                lambda: connection.execute(
                    "UPDATE pipeline_job_transitions SET intent_type='changed' "
                    "WHERE origin_commit_id=?",
                    (commit_id,),
                ),
                "business_history_append_only",
            ),
            _expect_integrity(
                lambda: connection.execute(
                    "UPDATE document_asset_refs SET entity_deleted=1 "
                    "WHERE commit_id=?",
                    (commit_id,),
                ),
                "business_history_append_only",
            ),
        )
        deletes = (
            _expect_integrity(
                lambda: connection.execute(
                    "DELETE FROM deferred_actions WHERE origin_commit_id=?",
                    (commit_id,),
                ),
                "business_state_no_delete",
            ),
            _expect_integrity(
                lambda: connection.execute(
                    "DELETE FROM pipeline_job_transitions "
                    "WHERE origin_commit_id=?",
                    (commit_id,),
                ),
                "business_history_append_only",
            ),
            _expect_integrity(
                lambda: connection.execute(
                    "DELETE FROM document_asset_refs WHERE commit_id=?",
                    (commit_id,),
                ),
                "business_history_append_only",
            ),
        )
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise _ClosureProbeError("closed fixture has foreign-key violations")
        return _ClosureProbeResult(missing, late, updates, deletes)
    finally:
        connection.close()


__all__: list[str] = []
