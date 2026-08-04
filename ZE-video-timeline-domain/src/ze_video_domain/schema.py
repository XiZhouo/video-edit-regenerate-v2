"""ze.video_project.v1 — unified video-project document schema.

This is the single source of truth for the entity shapes shared by the video
quartet (① edit-studio, ② gen-timeline, ③ timeline-editor, ④ batch-foundry).
It is framework-agnostic *domain* data: pure dicts + immutable factory/update
helpers. Each build-v3 skill's ``document_register`` adapts these shapes into
framework entity types — the domain layer never imports the framework
(editor-architecture rule 11: Document ≠ Storage; rule 13: framework vs mod).

Discipline enforced here:
- Immutability: factories return fresh dicts; every mutation goes through
  ``update_entity`` / ``bump_version``, which return NEW objects and never
  mutate in place (coding-style: never mutate).
- Normalized by id: a Document keeps flat ``dict[id -> entity]`` maps per type
  (editor-architecture rule 5).
- Concurrency: every entity carries an integer ``version`` counter, bumped on
  every change (editor-architecture rule 12).

No randomness / clock calls live here so the module is deterministic and
testable; ids and timestamps are injected by callers.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

SCHEMA_ID = "ze.video_project.v1"

# --- controlled vocabularies -------------------------------------------------

ASSET_TYPES = ("video", "image", "audio")
TRACK_KINDS = ("video", "overlay", "audio")
CLIP_KINDS = ("source", "gen", "bridge")  # clip.x_kind
SEND_MODES = (1, 2, 3)  # 1 标注入视频 / 2 拆参考图 / 3 直发参照
REPASTE_STRATEGIES = ("full_segment", "mask_merge", "export_only")  # 整段候选/时空蒙版/仅导出
CANDIDATE_SCOPES = ("region", "clip", "span")  # unifies clip_candidate + editor.candidate
SEND_JOB_STATUSES = ("draft", "compiled", "dispatched", "succeeded", "failed", "cancelled")
VERIFY_VERDICTS = ("green", "yellow", "red")

ENTITY_TYPES = (
    "project",
    "asset",
    "track",
    "clip",
    "annotation",
    "send_profile",
    "send_job",
    "candidate",
    "verify_report",
)

# Which flat map on the Document holds each entity type.
_COLLECTION = {
    "asset": "assets",
    "track": "tracks",
    "clip": "clips",
    "annotation": "annotations",
    "send_profile": "send_profiles",
    "send_job": "send_jobs",
    "candidate": "candidates",
    "verify_report": "verify_reports",
}


# --- validation helpers ------------------------------------------------------

def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ValueError(msg)


def _str(value: Any, field: str) -> str:
    _require(isinstance(value, str) and value != "", f"{field} must be a non-empty string")
    return value


def _one_of(value: Any, allowed: Iterable[Any], field: str) -> Any:
    _require(value in allowed, f"{field} must be one of {tuple(allowed)}, got {value!r}")
    return value


def _num(value: Any, field: str, *, allow_none: bool = False) -> Any:
    if value is None and allow_none:
        return None
    _require(isinstance(value, (int, float)) and not isinstance(value, bool),
             f"{field} must be a number")
    return value


def _interval(value: Any, field: str) -> list[float]:
    _require(
        isinstance(value, (list, tuple)) and len(value) == 2
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value),
        f"{field} must be [t0, t1] numbers",
    )
    t0, t1 = float(value[0]), float(value[1])
    _require(t1 >= t0, f"{field} must have t1 >= t0, got [{t0}, {t1}]")
    return [t0, t1]


def _bbox(value: Any, field: str) -> dict[str, float]:
    """bbox in the asset coordinate system: {x, y, w, h}."""
    _require(isinstance(value, Mapping), f"{field} must be a mapping {{x,y,w,h}}")
    out = {}
    for k in ("x", "y", "w", "h"):
        _require(k in value, f"{field}.{k} missing")
        out[k] = float(_num(value[k], f"{field}.{k}"))
    _require(out["w"] >= 0 and out["h"] >= 0, f"{field} w/h must be >= 0")
    return out


# --- base entity -------------------------------------------------------------

def _base(entity_id: str, entity_type: str, name: str) -> dict[str, Any]:
    _one_of(entity_type, ENTITY_TYPES, "type")
    return {
        "id": _str(entity_id, "id"),
        "type": entity_type,
        "name": _str(name, "name"),
        "version": 1,
    }


def bump_version(entity: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``entity`` with ``version`` incremented (immutable)."""
    out = dict(entity)
    out["version"] = int(entity.get("version", 1)) + 1
    return out


def update_entity(entity: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
    """Return a NEW entity with ``changes`` applied and version bumped.

    Never mutates the input. ``id`` and ``type`` cannot be changed.
    """
    for locked in ("id", "type"):
        if locked in changes:
            raise ValueError(f"cannot change immutable field {locked!r}")
    out = dict(entity)
    out.update(changes)
    out["version"] = int(entity.get("version", 1)) + 1
    return out


# --- entity factories --------------------------------------------------------

def make_project(project_id: str, name: str, *, fps: float = 30.0,
                 width: int = 1920, height: int = 1080,
                 global_context: str = "") -> dict[str, Any]:
    """project — name / fps / resolution / globalContext (written by ①②③)."""
    ent = _base(project_id, "project", name)
    ent.update({
        "fps": float(_num(fps, "fps")),
        "width": int(_num(width, "width")),
        "height": int(_num(height, "height")),
        "globalContext": _str_or_empty(global_context, "globalContext"),
    })
    return ent


def make_asset(asset_id: str, name: str, asset_type: str, ref: str, *,
               duration: float | None = None, width: int | None = None,
               height: int | None = None, fps: float | None = None,
               seedance_asset_id: str | None = None,
               content_hash: str | None = None) -> dict[str, Any]:
    """asset — name is project-unique (serves ④ naming reference); type/ref/meta.

    Uniqueness of ``name`` is enforced at Document level (see ``add_entity``).
    ``content_hash`` supports the "生成结果一键归档转素材" dedup-by-hash flow.
    """
    ent = _base(asset_id, "asset", name)
    ent.update({
        "assetType": _one_of(asset_type, ASSET_TYPES, "assetType"),
        "ref": _str(ref, "ref"),
        "meta": {
            "duration": _num(duration, "duration", allow_none=True),
            "width": _num(width, "width", allow_none=True),
            "height": _num(height, "height", allow_none=True),
            "fps": _num(fps, "fps", allow_none=True),
        },
        "seedanceAssetId": seedance_asset_id,
        "contentHash": content_hash,
    })
    return ent


def make_track(track_id: str, name: str, *, order: int = 0, kind: str = "video",
               visible: bool = True, included_in_send: bool = True,
               locked: bool = False) -> dict[str, Any]:
    """track — order / visible / includedInSend / locked / kind.

    ``visible`` and ``includedInSend`` are the V2 double-switch: preview-visible
    is not the same as sent-to-model.
    """
    ent = _base(track_id, "track", name)
    ent.update({
        "order": int(_num(order, "order")),
        "kind": _one_of(kind, TRACK_KINDS, "kind"),
        "visible": bool(visible),
        "includedInSend": bool(included_in_send),
        "locked": bool(locked),
    })
    return ent


def make_clip(clip_id: str, name: str, track_id: str, asset_id: str | None, *,
              timeline_start: float, timeline_end: float,
              in_point: float = 0.0, out_point: float | None = None,
              x_kind: str = "source", gen: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """clip — trackId / assetId / timeline interval / in-out / gen extension ns.

    Gen-specific fields (x_mode, x_prompt, x_bridgeIn/Out, x_refAssetIds…) live
    under ``gen`` (the x_kind:gen|source|bridge extension namespace), preserved
    verbatim so ②③'s existing clip semantics carry over losslessly.
    """
    ent = _base(clip_id, "clip", name)
    tl = _interval([timeline_start, timeline_end], "timeline")
    ent.update({
        "trackId": _str(track_id, "trackId"),
        "assetId": asset_id,  # None for pure-gen placeholder clips
        "timeline": {"start": tl[0], "end": tl[1]},
        "inPoint": float(_num(in_point, "inPoint")),
        "outPoint": _num(out_point, "outPoint", allow_none=True),
        "xKind": _one_of(x_kind, CLIP_KINDS, "xKind"),
        "gen": dict(gen) if gen else {},
    })
    return ent


def make_annotation(annotation_id: str, name: str, *, bbox: Mapping[str, Any],
                    interval: Iterable[float], label: str, number: int,
                    color: str = "#ff3b30", target_clip_id: str | None = None,
                    target_span: Iterable[float] | None = None,
                    ref_image_asset_id: str | None = None) -> dict[str, Any]:
    """annotation — bbox(asset coords) / interval[t0,t1] / label / number / color.

    Written by ① (edit-studio), read by ② (gen-timeline). ``interval`` may be a
    degenerate single-frame [ta, ta] — still a valid interval, the send segment
    is stretched open by padding at compile time.
    """
    ent = _base(annotation_id, "annotation", name)
    ent.update({
        "bbox": _bbox(bbox, "bbox"),
        "interval": _interval(interval, "interval"),
        "label": _str(label, "label"),
        "number": int(_num(number, "number")),
        "color": _str(color, "color"),
        "targetClipId": target_clip_id,
        "targetSpan": _interval(target_span, "targetSpan") if target_span is not None else None,
        "refImageAssetId": ref_image_asset_id,
    })
    return ent


def make_send_profile(profile_id: str, name: str, *, mode: int, model: str,
                      duration: float | None = None, ratio: str = "16:9",
                      repaste_strategy: str = "full_segment",
                      padding_sec: float = 0.5,
                      strategy_output: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """send_profile — mode / model / duration / ratio / 回贴策略 / paddingSec.

    ``strategy_output`` holds the structured 5-tuple from the prompt-strategy
    layer (suggestedMode/assetRoles/riskChecks/promptPlan/optimizedPrompt).
    """
    ent = _base(profile_id, "send_profile", name)
    ent.update({
        "mode": _one_of(mode, SEND_MODES, "mode"),
        "model": _str(model, "model"),
        "duration": _num(duration, "duration", allow_none=True),
        "ratio": _str(ratio, "ratio"),
        "repasteStrategy": _one_of(repaste_strategy, REPASTE_STRATEGIES, "repasteStrategy"),
        "paddingSec": float(_num(padding_sec, "paddingSec")),
        "strategyOutput": dict(strategy_output) if strategy_output else None,
    })
    return ent


def make_send_job(job_id: str, name: str, *, profile_snapshot: Mapping[str, Any],
                  compiled_snapshot: Mapping[str, Any],
                  status: str = "draft", task_id: str | None = None,
                  elapsed_sec: float | None = None,
                  error: str | None = None) -> dict[str, Any]:
    """send_job — profile snapshot / compiled artifact snapshot / status / task_id.

    The auditable dispatch record: freezes the exact prompt, file order, render
    surfaces and model params that were sent, so a result can always be traced
    to what produced it (V7).
    """
    ent = _base(job_id, "send_job", name)
    ent.update({
        "profileSnapshot": dict(profile_snapshot),
        "compiledSnapshot": dict(compiled_snapshot),
        "status": _one_of(status, SEND_JOB_STATUSES, "status"),
        "taskId": task_id,
        "elapsedSec": _num(elapsed_sec, "elapsedSec", allow_none=True),
        "error": error,
    })
    return ent


def make_candidate(candidate_id: str, name: str, *, scope: str,
                   video_ref: str | None = None, source_send_job_id: str | None = None,
                   target_clip_id: str | None = None, target_span: Iterable[float] | None = None,
                   stale: bool = False, verify_report_id: str | None = None,
                   adopted: bool = False) -> dict[str, Any]:
    """candidate — scope(region|clip|span) / videoRef / source send_job / stale.

    Unifies gen-timeline.clip_candidate and editor.candidate into one shape.
    Written by the machine, adopted by a human. ``stale`` is set when the human
    edits the target after the job started (gen-timeline stale mechanism,
    generalised domain-wide).
    """
    ent = _base(candidate_id, "candidate", name)
    ent.update({
        "scope": _one_of(scope, CANDIDATE_SCOPES, "scope"),
        "videoRef": video_ref,
        "sourceSendJobId": source_send_job_id,
        "targetClipId": target_clip_id,
        "targetSpan": _interval(target_span, "targetSpan") if target_span is not None else None,
        "stale": bool(stale),
        "verifyReportId": verify_report_id,
        "adopted": bool(adopted),
    })
    return ent


def make_verify_report(report_id: str, name: str, *, candidate_id: str,
                       checks: Mapping[str, Any] | None = None,
                       verdict: str = "green") -> dict[str, Any]:
    """verify_report — OCR残留 / 框外帧差 / 接缝帧差 / 时长漂移 / 红黄绿结论.

    ``checks`` maps check-name -> {verdict, detail}. Overall ``verdict`` is the
    worst of the individual checks (red > yellow > green).
    """
    ent = _base(report_id, "verify_report", name)
    ent.update({
        "candidateId": _str(candidate_id, "candidateId"),
        "checks": dict(checks) if checks else {},
        "verdict": _one_of(verdict, VERIFY_VERDICTS, "verdict"),
    })
    return ent


# --- document (normalized-by-id container) -----------------------------------

def new_document(project: Mapping[str, Any]) -> dict[str, Any]:
    """Create an empty ze.video_project.v1 document around ``project``.

    One flat map per entity type (normalized by id).
    """
    _require(project.get("type") == "project", "new_document requires a project entity")
    return {
        "schema": SCHEMA_ID,
        "project": dict(project),
        "assets": {},
        "tracks": {},
        "clips": {},
        "annotations": {},
        "send_profiles": {},
        "send_jobs": {},
        "candidates": {},
        "verify_reports": {},
    }


def _collection_key(entity_type: str) -> str:
    _require(entity_type in _COLLECTION, f"{entity_type} is not stored in a collection")
    return _COLLECTION[entity_type]


def add_entity(document: Mapping[str, Any], entity: Mapping[str, Any]) -> dict[str, Any]:
    """Return a NEW document with ``entity`` added to its collection (immutable).

    Enforces project-unique asset ``name`` (the naming-reference invariant).
    """
    key = _collection_key(entity["type"])
    eid = entity["id"]
    _require(eid not in document[key], f"{entity['type']} id {eid!r} already exists")
    if entity["type"] == "asset":
        existing = {a["name"] for a in document[key].values()}
        _require(entity["name"] not in existing,
                 f"asset name {entity['name']!r} must be unique within the project")
    out = dict(document)
    out[key] = {**document[key], eid: dict(entity)}
    return out


def put_entity(document: Mapping[str, Any], entity: Mapping[str, Any]) -> dict[str, Any]:
    """Return a NEW document with ``entity`` replaced (expects it to exist)."""
    key = _collection_key(entity["type"])
    eid = entity["id"]
    _require(eid in document[key], f"{entity['type']} id {eid!r} does not exist")
    out = dict(document)
    out[key] = {**document[key], eid: dict(entity)}
    return out


def remove_entity(document: Mapping[str, Any], entity_type: str, entity_id: str) -> dict[str, Any]:
    """Return a NEW document with the entity removed."""
    key = _collection_key(entity_type)
    _require(entity_id in document[key], f"{entity_type} id {entity_id!r} does not exist")
    out = dict(document)
    coll = dict(document[key])
    del coll[entity_id]
    out[key] = coll
    return out


def get_entity(document: Mapping[str, Any], entity_type: str, entity_id: str) -> dict[str, Any] | None:
    key = _collection_key(entity_type)
    ent = document[key].get(entity_id)
    return dict(ent) if ent is not None else None


def new_id(prefix: str, existing_ids: Iterable[str]) -> str:
    """Deterministic id allocator: ``prefix`` + next free integer suffix.

    Avoids randomness so the domain stays deterministic/testable. The framework
    may supply its own ids instead; this is a convenience for CLI/tests.
    """
    used = set()
    for eid in existing_ids:
        if isinstance(eid, str) and eid.startswith(prefix + "-"):
            tail = eid[len(prefix) + 1:]
            if tail.isdigit():
                used.add(int(tail))
    n = 1
    while n in used:
        n += 1
    return f"{prefix}-{n:04d}"


def _str_or_empty(value: Any, field: str) -> str:
    _require(isinstance(value, str), f"{field} must be a string")
    return value
