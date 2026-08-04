"""Pure declaration and projection helpers for runtime rehydration.

This middle layer snapshots registry declarations, hands off verified restored
business heads, captures exact business/runtime inventories, and validates
declaration authority installation.  It depends only on the state layer and
flat core model types; publication and durability transitions stay in the
facade coordinator.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable

from . import _paths  # noqa: F401  (install flat core imports first)
from ._runtime_rehydration_state import (
    DetachedBusinessHead,
    ReadOnlyDeclarationSnapshot,
    RegistryDeclarationSnapshot,
    _BucketInventory,
    _DeclarationRecord,
    _DetachedRecord,
    _EntityInventory,
    _LEDGER,
    _RuntimeInventory,
    _SourceInventory,
    _decode_json,
    _encode_json,
    _fail,
)

from document import (  # noqa: E402
    Document,
    DocumentRegistry,
    ReadOnlySourceSpec,
    validate_harvest_rule,
)
from entity import Entity, _clone_json_value  # noqa: E402
from field_types import canonicalize_entity_schema  # noqa: E402


def _declaration_digest(
    *,
    schema_version: str,
    runtime_generation: str,
    schema_bytes: bytes,
    mutation_policy_bytes: bytes,
    collaboration_mode: str,
    save_policy: str | None,
    collaboration_units: tuple[tuple[str, bool], ...],
    harvest_bytes: bytes,
    read_only_sources: tuple[tuple[str, str, str], ...],
    migrations: tuple[Callable[[Document], bool], ...],
) -> bytes:
    migration_labels = []
    for fn in migrations:
        migration_labels.append({
            "module": getattr(fn, "__module__", ""),
            "qualname": getattr(fn, "__qualname__", type(fn).__qualname__),
        })
    payload = {
        "schema_version": schema_version,
        "runtime_generation": runtime_generation,
        "schema": _decode_json(schema_bytes),
        "mutation_policies": _decode_json(mutation_policy_bytes),
        "collaboration_mode": collaboration_mode,
        "save_policy": save_policy,
        "collaboration_units": [list(item) for item in collaboration_units],
        "harvest_rules": _decode_json(harvest_bytes),
        "read_only_sources": [list(item) for item in read_only_sources],
        # Diagnostic only: durable exactly-once migration identity belongs in
        # the future adapter's migration ledger, not in this process digest.
        "migration_labels": migration_labels,
    }
    return hashlib.sha256(_encode_json(payload)).digest()


def snapshot_registry_declarations(
    registry: DocumentRegistry,
    *,
    schema_version: str,
    runtime_generation: str,
) -> RegistryDeclarationSnapshot:
    """Detach every runtime-relevant declaration from an exact registry."""
    if type(registry) is not DocumentRegistry:
        _fail(
            "runtime_declaration_invalid",
            "runtime_registry_type_invalid",
            "runtime declarations require an exact DocumentRegistry",
        )
    if type(schema_version) is not str or not schema_version:
        _fail(
            "runtime_declaration_invalid",
            "runtime_schema_version_invalid",
            "runtime declarations require a target schema version",
        )
    if (
        type(runtime_generation) is not str
        or not runtime_generation
        or len(runtime_generation) > 256
    ):
        _fail(
            "runtime_declaration_invalid",
            "runtime_generation_invalid",
            "runtime declarations require an exact deployment generation",
        )
    try:
        runtime_generation.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _fail(
            "runtime_declaration_invalid",
            "runtime_generation_invalid",
            "runtime deployment generation must be strict UTF-8",
        )
    state = object.__getattribute__(registry, "__dict__")
    if type(state) is not dict or set(state) != {
        "_entity_types", "_mutation_policies", "_collaboration_mode", "_save_policy",
        "_read_only_sources", "_open_migrations", "_harvest_rules",
        "_demo", "_demo_templates",
    }:
        _fail(
            "runtime_declaration_invalid",
            "runtime_registry_state_invalid",
            "runtime registry state is invalid or has unknown declarations",
        )

    raw_entity_types = state.get("_entity_types")
    if type(raw_entity_types) is not dict or not raw_entity_types:
        _fail(
            "runtime_declaration_invalid",
            "runtime_entity_declarations_invalid",
            "runtime registry must declare at least one entity type",
        )
    schema: dict[str, Any] = {}
    units: list[tuple[str, bool]] = []
    for entity_type, declaration in raw_entity_types.items():
        if (
            type(entity_type) is not str
            or not entity_type
            or type(declaration) is not dict
            or set(declaration) != {"schema", "collaboration_unit"}
            or type(declaration.get("schema")) is not dict
            or type(declaration.get("collaboration_unit")) is not bool
        ):
            _fail(
                "runtime_declaration_invalid",
                "runtime_entity_declarations_invalid",
                "runtime entity declaration has an invalid shape",
            )
        schema[entity_type] = canonicalize_entity_schema(declaration["schema"])
        units.append((entity_type, declaration["collaboration_unit"]))

    collaboration_mode = state.get("_collaboration_mode")
    if type(collaboration_mode) is not str:
        _fail(
            "runtime_declaration_invalid",
            "runtime_collaboration_mode_missing",
            "runtime registry must declare collaboration mode",
        )
    save_policy = state.get("_save_policy")
    if save_policy is not None and type(save_policy) is not str:
        _fail(
            "runtime_declaration_invalid",
            "runtime_save_policy_invalid",
            "runtime save policy declaration is invalid",
        )

    raw_harvest = state.get("_harvest_rules")
    if type(raw_harvest) is not list or any(type(rule) is not dict for rule in raw_harvest):
        _fail(
            "runtime_declaration_invalid",
            "runtime_harvest_declarations_invalid",
            "runtime harvest declarations are invalid",
        )
    harvest_rules = _clone_json_value(raw_harvest)
    for rule in harvest_rules:
        validate_harvest_rule(rule, schema)

    raw_sources = state.get("_read_only_sources")
    if type(raw_sources) is not list:
        _fail(
            "runtime_declaration_invalid",
            "runtime_read_only_declarations_invalid",
            "runtime read-only declarations are invalid",
        )
    sources: list[tuple[str, str, str]] = []
    source_names: set[str] = set()
    for raw in raw_sources:
        if type(raw) is not dict or set(raw) != {"name", "entity_type", "loader_path"}:
            _fail(
                "runtime_declaration_invalid",
                "runtime_read_only_declarations_invalid",
                "runtime read-only declaration has an invalid shape",
            )
        name = raw["name"]
        entity_type = raw["entity_type"]
        loader_path = raw["loader_path"]
        if any(type(value) is not str or not value for value in (name, entity_type, loader_path)):
            _fail(
                "runtime_declaration_invalid",
                "runtime_read_only_declarations_invalid",
                "runtime read-only declaration fields must be non-empty strings",
            )
        if name in source_names:
            _fail(
                "runtime_declaration_invalid",
                "runtime_read_only_declarations_conflict",
                "runtime read-only source names must be unique",
            )
        if entity_type not in schema:
            _fail(
                "runtime_declaration_invalid",
                "runtime_read_only_entity_type_undeclared",
                "runtime read-only sources must use the unified Document schema",
            )
        source_names.add(name)
        sources.append((name, entity_type, loader_path))

    raw_migrations = state.get("_open_migrations")
    if type(raw_migrations) is not list or any(not callable(fn) for fn in raw_migrations):
        _fail(
            "runtime_declaration_invalid",
            "runtime_migration_declarations_invalid",
            "runtime migration declarations are invalid",
        )

    raw_mutation_policies = state.get("_mutation_policies")
    if type(raw_mutation_policies) is not dict:
        _fail(
            "runtime_declaration_invalid",
            "runtime_mutation_policy_declarations_invalid",
            "runtime mutation policy declarations are invalid",
        )
    mutation_policies = _clone_json_value(raw_mutation_policies)

    # Exercise the Document constructor as the final declaration-domain check,
    # including policy/schema cross-validation.  This object is never
    # published; it merely proves the detached declaration can be installed.
    Document(
        schema_version=schema_version,
        collaboration_mode=collaboration_mode,
        schema=schema,
        mutation_policies=mutation_policies,
        harvest_rules=harvest_rules,
        save_policy=save_policy,
    )
    schema_bytes = _encode_json(schema)
    mutation_policy_bytes = _encode_json(mutation_policies)
    harvest_bytes = _encode_json(harvest_rules)
    source_rows = tuple(sources)
    unit_rows = tuple(units)
    migration_rows = tuple(raw_migrations)
    return _LEDGER.issue_declaration(_DeclarationRecord(
        schema_version=schema_version,
        runtime_generation=runtime_generation,
        schema_bytes=schema_bytes,
        mutation_policy_bytes=mutation_policy_bytes,
        collaboration_mode=collaboration_mode,
        save_policy=save_policy,
        collaboration_units=unit_rows,
        harvest_bytes=harvest_bytes,
        read_only_sources=source_rows,
        migrations=migration_rows,
        digest=_declaration_digest(
            schema_version=schema_version,
            runtime_generation=runtime_generation,
            schema_bytes=schema_bytes,
            mutation_policy_bytes=mutation_policy_bytes,
            collaboration_mode=collaboration_mode,
            save_policy=save_policy,
            collaboration_units=unit_rows,
            harvest_bytes=harvest_bytes,
            read_only_sources=source_rows,
            migrations=migration_rows,
        ),
    ))


def detach_restored_business_head(restored: Any) -> DetachedBusinessHead:
    """Consume a rooted restore carrier without exposing its mutable Document."""
    from business_snapshot import (  # noqa: E402
        BusinessSnapshotError,
        RestoredBusinessHead,
        _consume_restored_business_head,
    )

    if type(restored) is not RestoredBusinessHead:
        _fail(
            "runtime_head_invalid",
            "runtime_restored_head_invalid",
            "snapshot head is not a detached restored business head",
        )
    try:
        baseline = _consume_restored_business_head(restored)
    except BusinessSnapshotError as exc:
        _fail(
            "runtime_head_invalid",
            exc.code,
            "snapshot restored head changed or was not framework-issued",
        )
    except BaseException:
        _fail(
            "runtime_head_invalid",
            "runtime_restored_head_validation_failed",
            "snapshot restored head could not be validated",
        )
    return _LEDGER.issue_detached(_DetachedRecord(
        project_id=baseline.project.project_id,
        generation=baseline.project.generation,
        document=restored.document,
        origin="snapshot",
        baseline_snapshot=baseline,
    ))


def _issue_unpublished_document_for_test(
    document: Document,
    *,
    project_id: str,
    generation: int,
) -> DetachedBusinessHead:
    """Test-only issuer; production legacy heads require the future scanner."""
    if (
        type(document) is not Document
        or type(project_id) is not str
        or not project_id
        or type(generation) is not int
        or generation < 1
    ):
        _fail(
            "runtime_head_invalid",
            "runtime_detached_head_invalid",
            "unpublished test head metadata is invalid",
        )
    baseline = _capture_business_projection(document, project_id, generation)
    return _LEDGER.issue_detached(_DetachedRecord(
        project_id=project_id,
        generation=generation,
        document=document,
        origin="test_unpublished",
        baseline_snapshot=baseline,
    ))


def _capture_business_projection(
    document: Document,
    project_id: str,
    generation: int,
) -> Any:
    from business_snapshot import (  # noqa: E402
        BusinessSnapshotError,
        capture_business_snapshot,
    )

    try:
        return capture_business_snapshot(project_id, generation, document).snapshot
    except BusinessSnapshotError as exc:
        _fail(
            "runtime_business_snapshot_invalid",
            exc.code,
            "runtime business projection is invalid",
        )


def _business_root(snapshot: Any) -> bytes:
    root = snapshot.project.snapshot_sha256
    if type(root) is not bytes or len(root) != 32:
        _fail(
            "runtime_rehydration_failed",
            "runtime_business_root_invalid",
            "captured business root is invalid",
        )
    return root


def _capture_runtime_inventory(document: Document) -> _RuntimeInventory:
    buckets: list[_BucketInventory] = []
    for entity_type, bucket in document._collections.items():
        entities: list[_EntityInventory] = []
        for key, entity in bucket.items():
            admission = entity._document_admission_state
            if admission is None:
                admission_schema = None
                admission_validator = None
            else:
                admission_schema = _encode_json(admission[0])
                admission_validator = admission[1]
            entities.append(_EntityInventory(
                key=key,
                entity=entity,
                entity_type=entity.type,
                entity_id=entity.id,
                read_only=entity.read_only,
                schema_bytes=_encode_json(entity._schema),
                validator=entity._validator,
                owner=entity._document_owner,
                admission_schema_bytes=admission_schema,
                admission_validator=admission_validator,
                read_only_payload=(
                    _encode_json(entity.to_dict()) if entity.read_only else None
                ),
            ))
        buckets.append(_BucketInventory(entity_type, bucket, tuple(entities)))

    source_rows: list[_SourceInventory] = []
    for key, spec in document._read_only_sources.items():
        source_rows.append(_SourceInventory(
            key=key,
            spec=spec,
            name=spec.name,
            entity_type=spec.entity_type,
            schema_bytes=_encode_json(spec.schema),
            loader=spec.loader,
        ))
    return _RuntimeInventory(
        collections=document._collections,
        buckets=tuple(buckets),
        sources=document._read_only_sources,
        source_rows=tuple(source_rows),
        schema=document._schema,
        schema_bytes=_encode_json(document._schema),
        mutation_policies=document._mutation_policies,
        mutation_policy_bytes=_encode_json(document._mutation_policies),
        mutation_policy_sealed=document._mutation_policy_sealed,
        mutation_policy_registry_authority=(
            document._mutation_policy_registry_authority
        ),
        mutation_policy_registry_epoch=document._mutation_policy_registry_epoch,
        mutation_policy_registry_pid=document._mutation_policy_registry_pid,
        mutation_policy_command_authorities=(
            document._mutation_policy_command_authorities
        ),
        mutation_policy_command_items=tuple(
            document._mutation_policy_command_authorities.items()
        ),
        history_op_log_authority=document._history_op_log_authority,
        history_controller_authority=document._history_controller_authority,
        history_authority_epoch=document._history_authority_epoch,
        history_authority_pid=document._history_authority_pid,
        collaboration_mode=document._collaboration_mode,
        save_policy=document._save_policy,
        harvest_rules=document._harvest_rules,
        harvest_bytes=_encode_json(document._harvest_rules),
    )


def _runtime_inventory_matches(document: Document, expected: _RuntimeInventory) -> bool:
    if (
        document._collections is not expected.collections
        or document._read_only_sources is not expected.sources
        or document._schema is not expected.schema
        or document._mutation_policies is not expected.mutation_policies
        or document._mutation_policy_sealed is not expected.mutation_policy_sealed
        or document._mutation_policy_registry_authority
        is not expected.mutation_policy_registry_authority
        or document._mutation_policy_registry_epoch
        is not expected.mutation_policy_registry_epoch
        or document._mutation_policy_registry_pid
        != expected.mutation_policy_registry_pid
        or document._mutation_policy_command_authorities
        is not expected.mutation_policy_command_authorities
        or document._history_op_log_authority
        is not expected.history_op_log_authority
        or document._history_controller_authority
        is not expected.history_controller_authority
        or document._history_authority_epoch
        is not expected.history_authority_epoch
        or document._history_authority_pid != expected.history_authority_pid
        or document._harvest_rules is not expected.harvest_rules
        or document._collaboration_mode != expected.collaboration_mode
        or document._save_policy != expected.save_policy
    ):
        return False
    try:
        if (
            _encode_json(document._schema) != expected.schema_bytes
            or _encode_json(document._mutation_policies)
            != expected.mutation_policy_bytes
            or tuple(document._mutation_policy_command_authorities)
            != tuple(key for key, _value in expected.mutation_policy_command_items)
            or any(
                document._mutation_policy_command_authorities[key] is not binding
                for key, binding in expected.mutation_policy_command_items
            )
            or _encode_json(document._harvest_rules) != expected.harvest_bytes
            or tuple(document._collections.keys())
            != tuple(bucket.entity_type for bucket in expected.buckets)
            or tuple(document._read_only_sources.keys())
            != tuple(source.key for source in expected.source_rows)
        ):
            return False
        for bucket_expected in expected.buckets:
            bucket = document._collections.get(bucket_expected.entity_type)
            if bucket is not bucket_expected.bucket:
                return False
            if tuple(bucket.keys()) != tuple(item.key for item in bucket_expected.entities):
                return False
            for item in bucket_expected.entities:
                entity = bucket.get(item.key)
                if (
                    entity is not item.entity
                    or entity.type != item.entity_type
                    or entity.id != item.entity_id
                    or entity.read_only is not item.read_only
                    or entity._validator is not item.validator
                    or entity._document_owner is not item.owner
                    or _encode_json(entity._schema) != item.schema_bytes
                ):
                    return False
                admission = entity._document_admission_state
                if item.admission_schema_bytes is None:
                    if admission is not None:
                        return False
                elif (
                    type(admission) is not tuple
                    or len(admission) != 2
                    or _encode_json(admission[0]) != item.admission_schema_bytes
                    or admission[1] is not item.admission_validator
                ):
                    return False
                if item.read_only_payload is not None and (
                    _encode_json(entity.to_dict()) != item.read_only_payload
                ):
                    return False
        for source_expected in expected.source_rows:
            spec = document._read_only_sources.get(source_expected.key)
            if (
                spec is not source_expected.spec
                or spec.name != source_expected.name
                or spec.entity_type != source_expected.entity_type
                or spec.loader is not source_expected.loader
                or _encode_json(spec.schema) != source_expected.schema_bytes
            ):
                return False
    except BaseException:
        return False
    return True


def _runtime_inventory_preserved_after_source(
    document: Document,
    before: _RuntimeInventory,
    *,
    source_name: str,
    source_entity_type: str,
    loader: Any,
) -> bool:
    """Allow exactly one source append while preserving all prior runtime facts."""
    try:
        if (
            document._collections is not before.collections
            or document._read_only_sources is not before.sources
            or document._schema is not before.schema
            or document._mutation_policies is not before.mutation_policies
            or document._mutation_policy_sealed is not before.mutation_policy_sealed
            or document._mutation_policy_registry_authority
            is not before.mutation_policy_registry_authority
            or document._mutation_policy_registry_epoch
            is not before.mutation_policy_registry_epoch
            or document._mutation_policy_registry_pid
            != before.mutation_policy_registry_pid
            or document._mutation_policy_command_authorities
            is not before.mutation_policy_command_authorities
            or document._history_op_log_authority
            is not before.history_op_log_authority
            or document._history_controller_authority
            is not before.history_controller_authority
            or document._history_authority_epoch
            is not before.history_authority_epoch
            or document._history_authority_pid != before.history_authority_pid
            or tuple(document._mutation_policy_command_authorities)
            != tuple(key for key, _value in before.mutation_policy_command_items)
            or any(
                document._mutation_policy_command_authorities[key] is not binding
                for key, binding in before.mutation_policy_command_items
            )
            or document._harvest_rules is not before.harvest_rules
            or document._collaboration_mode != before.collaboration_mode
            or document._save_policy != before.save_policy
            or _encode_json(document._schema) != before.schema_bytes
            or _encode_json(document._mutation_policies)
            != before.mutation_policy_bytes
            or _encode_json(document._harvest_rules) != before.harvest_bytes
        ):
            return False

        before_buckets = {row.entity_type: row for row in before.buckets}
        for entity_type, expected_bucket in before_buckets.items():
            bucket = document._collections.get(entity_type)
            if bucket is not expected_bucket.bucket:
                return False
            expected_entities = {row.key: row for row in expected_bucket.entities}
            if tuple(bucket.keys())[:len(expected_entities)] != tuple(
                expected_entities.keys()
            ):
                return False
            for key, expected_entity in expected_entities.items():
                entity = bucket.get(key)
                if entity is not expected_entity.entity:
                    return False
                admission = entity._document_admission_state
                if (
                    entity.type != expected_entity.entity_type
                    or entity.id != expected_entity.entity_id
                    or entity.read_only is not expected_entity.read_only
                    or entity._validator is not expected_entity.validator
                    or entity._document_owner is not expected_entity.owner
                    or _encode_json(entity._schema) != expected_entity.schema_bytes
                ):
                    return False
                if expected_entity.admission_schema_bytes is None:
                    if admission is not None:
                        return False
                elif (
                    type(admission) is not tuple
                    or len(admission) != 2
                    or _encode_json(admission[0])
                    != expected_entity.admission_schema_bytes
                    or admission[1] is not expected_entity.admission_validator
                ):
                    return False
                if expected_entity.read_only_payload is not None and (
                    _encode_json(entity.to_dict())
                    != expected_entity.read_only_payload
                ):
                    return False
            for key, entity in bucket.items():
                if key not in expected_entities and (
                    entity_type != source_entity_type or not entity.read_only
                ):
                    return False
        for entity_type, bucket in document._collections.items():
            if entity_type in before_buckets:
                continue
            if entity_type != source_entity_type or any(
                not entity.read_only for entity in bucket.values()
            ):
                return False

        expected_source_keys = tuple(row.key for row in before.source_rows) + (
            source_name,
        )
        if tuple(document._read_only_sources.keys()) != expected_source_keys:
            return False
        for expected_source in before.source_rows:
            spec = document._read_only_sources.get(expected_source.key)
            if (
                spec is not expected_source.spec
                or spec.name != expected_source.name
                or spec.entity_type != expected_source.entity_type
                or spec.loader is not expected_source.loader
                or _encode_json(spec.schema) != expected_source.schema_bytes
            ):
                return False
        new_spec = document._read_only_sources.get(source_name)
        if (
            type(new_spec) is not ReadOnlySourceSpec
            or new_spec.name != source_name
            or new_spec.entity_type != source_entity_type
            or new_spec.loader is not loader
            or new_spec.schema != {}
        ):
            return False
    except BaseException:
        return False
    return True


def _runtime_sources_match_manifest(
    document: Document,
    declaration: _DeclarationRecord,
    loaders: tuple[Any, ...],
) -> bool:
    try:
        if len(loaders) != len(declaration.read_only_sources):
            return False
        if tuple(document._read_only_sources.keys()) != tuple(
            row[0] for row in declaration.read_only_sources
        ):
            return False
        for raw_source, loader in zip(declaration.read_only_sources, loaders):
            name, entity_type, _loader_path = raw_source
            spec = document._read_only_sources.get(name)
            if (
                type(spec) is not ReadOnlySourceSpec
                or spec.name != name
                or spec.entity_type != entity_type
                or spec.loader is not loader
                or spec.schema != {}
            ):
                return False
    except BaseException:
        return False
    return True


def _purge_runtime_overlays(document: Document) -> None:
    for entity_type, bucket in list(document._collections.items()):
        for entity in list(bucket.values()):
            if entity.read_only:
                document.hard_remove(entity_type, entity.id)
    for name in list(document.read_only_sources()):
        document.unregister_read_only_source(name)


def _install_declaration_authority(
    document: Document,
    declaration: _DeclarationRecord,
    *,
    validate_read_only: bool,
) -> None:
    schema = _decode_json(declaration.schema_bytes)
    mutation_policies = _decode_json(declaration.mutation_policy_bytes)
    harvest = _decode_json(declaration.harvest_bytes)
    if type(schema) is not dict:
        _fail(
            "runtime_declaration_invalid",
            "runtime_schema_authority_invalid",
            "runtime schema authority is not an exact object",
        )
    if type(mutation_policies) is not dict:
        _fail(
            "runtime_declaration_invalid",
            "runtime_mutation_policy_authority_invalid",
            "runtime mutation policy authority is not an exact object",
        )
    if type(harvest) is not list:
        _fail(
            "runtime_declaration_invalid",
            "runtime_harvest_authority_invalid",
            "runtime harvest authority is not an exact array",
        )
    document._schema_version = declaration.schema_version
    document._schema = schema
    document._install_mutation_policy_authority(mutation_policies)
    document._collaboration_mode = declaration.collaboration_mode
    document._save_policy = declaration.save_policy
    document._harvest_rules = harvest
    for rule in document._harvest_rules:
        validate_harvest_rule(rule, document._schema)
    for bucket in document._collections.values():
        for entity in bucket.values():
            if type(entity) is not Entity:
                _fail(
                    "runtime_rehydration_failed",
                    "runtime_entity_type_invalid",
                    "runtime collections contain a non-Entity value",
                )
            document._apply_validator_authority(
                entity,
                validate_current=validate_read_only and entity.read_only,
            )


def _declaration_authority_fingerprint(document: Document) -> tuple[Any, ...]:
    entity_authority = []
    for entity_type, bucket in document._collections.items():
        for key, entity in bucket.items():
            entity_authority.append((
                entity_type,
                key,
                entity,
                entity.read_only,
                _encode_json(entity._schema),
                entity._validator,
                entity._document_owner,
            ))
    source_authority = []
    for key, spec in document._read_only_sources.items():
        source_authority.append((
            key, spec, spec.name, spec.entity_type,
            _encode_json(spec.schema), spec.loader,
        ))
    return (
        document._schema,
        _encode_json(document._schema),
        document._mutation_policies,
        _encode_json(document._mutation_policies),
        document._collaboration_mode,
        document._save_policy,
        document._harvest_rules,
        _encode_json(document._harvest_rules),
        document._read_only_sources,
        tuple(source_authority),
        tuple(entity_authority),
        (
            document._mutation_policy_sealed,
            document._mutation_policy_registry_authority,
            document._mutation_policy_registry_epoch,
            document._mutation_policy_registry_pid,
            document._mutation_policy_command_authorities,
            tuple(document._mutation_policy_command_authorities.items()),
            document._history_op_log_authority,
            document._history_controller_authority,
            document._history_authority_epoch,
            document._history_authority_pid,
        ),
    )


def _authority_fingerprint_matches(
    document: Document,
    expected: tuple[Any, ...],
) -> bool:
    try:
        current = _declaration_authority_fingerprint(document)
        if len(current) != len(expected):
            return False
        # JSON/primitives compare by value; executable/mutable authority objects
        # compare by identity so equal-looking replacement cannot pass.
        if (
            current[0] is not expected[0]
            or current[1] != expected[1]
            or current[2] is not expected[2]
            or current[3:6] != expected[3:6]
            or current[6] is not expected[6]
            or current[7] != expected[7]
            or current[8] is not expected[8]
            or len(current[9]) != len(expected[9])
        ):
            return False
        for left, right in zip(current[9], expected[9]):
            if (
                left[0] != right[0]
                or left[1] is not right[1]
                or left[2:5] != right[2:5]
                or left[5] is not right[5]
            ):
                return False
        current_entities = {(row[0], row[1]): row for row in current[10]}
        for right in expected[10]:
            left = current_entities.get((right[0], right[1]))
            if left is None:
                if right[3]:
                    return False
                continue
            if left[2] is right[2] and (
                left[3] is not right[3]
                or left[4] != right[4]
                or left[5] is not right[5]
                or left[6] is not right[6]
            ):
                return False
        current_seal = current[11]
        expected_seal = expected[11]
        if (
            type(current_seal) is not tuple
            or type(expected_seal) is not tuple
            or len(current_seal) != 10
            or len(expected_seal) != 10
            or current_seal[0] is not expected_seal[0]
            or current_seal[1] is not expected_seal[1]
            or current_seal[2] is not expected_seal[2]
            or current_seal[3] != expected_seal[3]
            or current_seal[4] is not expected_seal[4]
            or len(current_seal[5]) != len(expected_seal[5])
            or current_seal[6] is not expected_seal[6]
            or current_seal[7] is not expected_seal[7]
            or current_seal[8] is not expected_seal[8]
            or current_seal[9] != expected_seal[9]
        ):
            return False
        for (left_id, left_binding), (right_id, right_binding) in zip(
            current_seal[5], expected_seal[5],
        ):
            if left_id != right_id or left_binding is not right_binding:
                return False
        if any(row[3] for row in current[10]):
            # Runtime overlays are installed only after canonical durability.
            return False
    except BaseException:
        return False
    return True


def _validate_changed_canonical_rows(
    document: Document,
    before: Any,
    after: Any,
) -> None:
    before_rows = {
        (row.entity_type, row.entity_id): row for row in before.entities
    }
    for row in after.entities:
        key = (row.entity_type, row.entity_id)
        if before_rows.get(key) == row:
            continue
        entity = document.get(row.entity_type, row.entity_id)
        if type(entity) is not Entity or entity.read_only:
            _fail(
                "runtime_migration_invalid",
                "runtime_migration_entity_projection_invalid",
                "migration changed a canonical row that is not present in memory",
            )
        document._apply_validator_authority(entity, validate_current=True)
