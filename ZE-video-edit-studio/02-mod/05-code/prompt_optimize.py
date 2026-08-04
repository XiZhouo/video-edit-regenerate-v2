"""Video-edit prompt optimization stage.

Business facts and target-model adaptation live here; credentials and provider
mechanics stay in the shared SDK.  A failed optimization patches its request
entity and never overwrites the user's draft or silently spends another account.

Optimizer account/model stay in the WorkItem payload (execution audit for this
run) and never become shared project preferences.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from sdk import AssetRef, CancelledError, ZeError, user_llm_schema_call
from _domain import ffmpeg_ops, layer_compose, render
from _domain.strategy import runner
import cursor_credentials
import guide_render

MAX_REFERENCE_FRAMES = 4
# 这些优化器会收到「与最终提交同构图」的烧引导视频，自行用 ffprobe/ffmpeg 看轨。
_VIDEO_OPTIMIZER_PROVIDERS = frozenset({
    "codex_cli_profile", "claude_cli_profile", "cursor_api_key",
})
_ACTIVE_CANCEL: dict[int, threading.Event] = {}
_APP_ROOT = Path(__file__).resolve().parents[2]


def _brief(value: Any, limit: int = 600) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def select_frame_times(
    *,
    reference_times: list[float] | tuple[()] = (),
    guide_facts: list[dict[str, Any]] | tuple[()] = (),
    max_frames: int = MAX_REFERENCE_FRAMES,
) -> list[float]:
    """Pick up to ``max_frames`` representative timeline times.

    Priority: explicit reference_frame cues → guide keyframes → guide midpoints.
    """
    chosen: list[float] = []
    seen: set[float] = set()

    def add(raw: float) -> None:
        at = round(float(raw), 3)
        if at < 0 or at in seen or len(chosen) >= max_frames:
            return
        seen.add(at)
        chosen.append(at)

    for at in reference_times:
        add(at)
    for guide in guide_facts:
        windows = guide.get("windows") or ()

        def exposed(at: float) -> bool:
            return not windows or any(
                float(window[0]) <= at <= float(window[1])
                for window in windows
                if isinstance(window, (list, tuple)) and len(window) == 2
            )

        origin = float(guide.get("keyframe_origin")
                       or ((guide.get("interval") or [0])[0] if guide.get("interval") else 0)
                       or 0)
        for frame in guide.get("keyframes") or ():
            at = origin + float(frame.get("time") or 0)
            if exposed(at):
                add(at)
    if not chosen:
        for guide in guide_facts:
            windows = guide.get("windows") or ()
            interval = windows[0] if windows else (guide.get("interval") or [0, 0])
            start = float(interval[0] or 0)
            end = float(interval[1] if len(interval) > 1 else start)
            add((start + max(end, start)) / 2.0)
    return chosen


def thin_guide_facts(guides: list[dict[str, Any]] | tuple[()] = ()) -> list[dict[str, Any]]:
    """Prompt 里只保留引导薄契约；完整 keyframe/bbox 留给烧视频链路。"""
    thinned: list[dict[str, Any]] = []
    for guide in guides:
        item: dict[str, Any] = {
            "number": guide.get("number"),
            "kind": str(guide.get("kind") or "box"),
            "label": str(guide.get("label") or "").strip(),
        }
        intervals = guide.get("submitted_intervals")
        if intervals:
            item["submitted_intervals"] = intervals
        else:
            interval = guide.get("interval")
            if isinstance(interval, (list, tuple)) and len(interval) == 2:
                item["interval"] = [float(interval[0]), float(interval[1])]
        thinned.append(item)
    return thinned


def thin_video_facts(facts: list[dict[str, Any]] | tuple[()] = ()) -> list[dict[str, Any]]:
    """工作区视频索引：文件名 + 局部时间，不含画面复述。"""
    return [{
        "file": str(item.get("file") or ""),
        "window": item.get("window"),
        "duration": item.get("duration"),
        "localStart": item.get("localStart", 0.0),
        "guidesRendered": bool(item.get("guidesRendered", True)),
    } for item in facts]


def build_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert editor facts into the thin strategy template fields."""
    return {
        "targetModel": str(payload.get("target_model") or ""),
        "targetProvider": str(payload.get("target_provider") or ""),
        "generationStrategy": str(payload.get("generation_strategy") or ""),
        "ratio": str(payload.get("ratio") or ""),
        "duration": round(float(payload.get("duration") or 0), 3),
        "guideFacts": json.dumps(
            thin_guide_facts(list(payload.get("guide_facts") or [])),
            ensure_ascii=False,
        ),
        "videoFacts": json.dumps(
            thin_video_facts(list(payload.get("video_facts") or [])),
            ensure_ascii=False,
        ),
        "submissionWindows": json.dumps(
            payload.get("submission_windows") or [], ensure_ascii=False,
        ),
        "userRequest": str(payload.get("user_request") or "").strip() or "按画面引导完成编辑",
    }


def render_prompt(payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    strategy = runner.load_strategy("video-edit-v1")
    user = runner.render_user_template(strategy, build_fields(payload))
    return strategy, f"{strategy.get('systemPrompt', '')}\n\n{user}".strip()


def _asset_bytes(asset_store, ref: dict[str, Any]) -> bytes:
    try:
        return asset_store.get(ref["sha256"])
    except Exception:
        return asset_store.get(AssetRef.from_dict(ref))


def prepare_visible_context(
    *,
    clips: list[dict[str, Any]],
    guides: list[dict[str, Any]],
    assets_by_id: dict[str, dict[str, Any]],
    total_duration: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Resolve current video/image files and keep only guides visible on canvas.

    This is the same layer rule used by video submission: generated clips use
    their own ``videoRef``; source clips resolve through their asset; guides
    below a covering video layer do not participate; imported images prefer the
    background-removed file.
    """
    prepared_clips: list[dict[str, Any]] = []
    for clip in clips:
        shaped = dict(clip)
        direct = shaped.get("videoRef") or shaped.get("video_ref")
        asset_id = str(shaped.get("assetId") or shaped.get("asset_id") or "")
        asset = assets_by_id.get(asset_id) or {}
        ref = direct or asset.get("assetRef")
        if isinstance(ref, dict) and ref.get("sha256"):
            shaped["videoRef"] = ref
        prepared_clips.append(shaped)

    prepared_guides: list[dict[str, Any]] = []
    for guide in guides:
        shaped = dict(guide)
        asset_id = str(shaped.get("asset_id") or shaped.get("assetId") or "")
        asset = assets_by_id.get(asset_id) or {}
        if asset:
            shaped["asset_name"] = str(asset.get("name") or "")
            ref = asset.get("processedRef") or asset.get("assetRef")
            if isinstance(ref, dict) and ref.get("sha256"):
                shaped["image_ref"] = ref
        prepared_guides.append(shaped)

    total = float(total_duration) if total_duration is not None else max(
        (
            float(clip.get("start") or 0) + float(clip.get("duration") or 0)
            for clip in prepared_clips
        ),
        default=0.0,
    )
    exposure = layer_compose.exposed_windows(
        prepared_clips, prepared_guides, 0.0, max(total, 0.001),
    )
    exposed_guides: list[dict[str, Any]] = []
    for guide in prepared_guides:
        windows = exposure.get(guide.get("id"))
        if not windows:
            continue
        exposed_guides.append({
            **guide,
            "windows": windows,
            "interval": [windows[0][0], windows[-1][1]],
        })
    return prepared_clips, exposed_guides


def _managed_temp_parent() -> Path:
    """Tool-owned runtime temp root; never mix generated media into source files."""
    configured = Path(os.environ.get("ZE_DATA_DIR") or (_APP_ROOT / ".data"))
    root = configured if configured.is_absolute() else (_APP_ROOT / configured)
    target = (root / "tmp" / "prompt-optimization").resolve()
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        target.chmod(0o700)
    except OSError:
        pass
    return target


def prepare_optimizer_videos(
    *,
    submission_windows: list[dict[str, Any]],
    clips: list[dict[str, Any]],
    assets_by_id: dict[str, dict[str, Any]],
    guides: list[dict[str, Any]],
    asset_store,
    work: str,
    canvas: tuple[int, int],
    fps: float,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Render the exact visible timeline windows for optimizers to inspect."""
    if asset_store is None or ffmpeg_ops.ffmpeg_bin() is None:
        raise ZeError(
            "requires_runtime",
            "提示词优化自主分析视频需要本地 ffmpeg 与素材存储",
            context={},
        )
    prepared_clips, prepared_guides = prepare_visible_context(
        clips=clips,
        guides=guides,
        assets_by_id=assets_by_id,
    )
    clips_by_id = {
        str(item.get("id") or ""): item for item in prepared_clips
    }
    paths: list[str] = []
    facts: list[dict[str, Any]] = []
    for index, window in enumerate(submission_windows, start=1):
        start = float(window.get("start") or 0)
        end = float(window.get("end") or 0)
        plan = layer_compose.compose_plan(prepared_clips, start, end)
        if not plan.get("valid"):
            raise ZeError(
                "validation_failed",
                f"{start:.2f}–{end:.2f}s 无法合成供提示词优化分析的视频",
                context={"span": [start, end], "reason": plan.get("reason")},
            )
        segment_work = os.path.join(work, f"optimizer-video-{index}-work")
        os.makedirs(segment_work, exist_ok=True)
        output = os.path.join(work, f"optimizer-video-{index}.mp4")
        try:
            composed = render.compose_span(
                plan,
                segment_work,
                fetch_bytes=lambda ref: _asset_bytes(asset_store, dict(ref)),
                clips_by_id=clips_by_id,
                canvas=(int(canvas[0]), int(canvas[1])),
                fps=float(fps),
            )
            guide_render.burn_guides(
                composed,
                output,
                prepared_guides,
                asset_store=asset_store,
                work=segment_work,
                canvas=(int(canvas[0]), int(canvas[1])),
                time_offset=start,
                fps=float(fps),
            )
        except ZeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ZeError(
                "requires_runtime",
                f"准备提示词优化分析视频失败: {type(exc).__name__}",
                context={"span": [start, end]},
            ) from exc
        paths.append(output)
        facts.append({
            "file": Path(output).name,
            "window": index,
            "timelineStart": round(start, 3),
            "timelineEnd": round(end, 3),
            "duration": round(end - start, 3),
            "localStart": 0.0,
            "composite": True,
            "guidesRendered": True,
        })
    if not paths:
        raise ZeError("validation_failed", "没有可供提示词优化分析的视频区间", context={})
    return paths, facts


def add_submission_timing(
    *,
    guides: list[dict[str, Any]],
    submission_windows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add file-local timing without replacing authoritative timeline timing."""
    enriched: list[dict[str, Any]] = []
    for guide in guides:
        shaped = dict(guide)
        spans = guide.get("windows") or [guide.get("interval") or [0, 0]]
        submitted_intervals: list[dict[str, Any]] = []
        for index, window in enumerate(submission_windows, start=1):
            window_start = float(window.get("start") or 0)
            window_end = float(window.get("end") or 0)
            for span in spans:
                if not (isinstance(span, (list, tuple)) and len(span) == 2):
                    continue
                start = max(float(span[0]), window_start)
                end = min(float(span[1]), window_end)
                if end < start:
                    continue
                submitted_intervals.append({
                    "window": index,
                    "start": round(start - window_start, 3),
                    "end": round(end - window_start, 3),
                })

        origin = float(
            guide.get("keyframe_origin")
            if guide.get("keyframe_origin") is not None
            else (guide.get("interval") or [0])[0]
        )
        submitted_keyframes: list[dict[str, Any]] = []
        for frame in guide.get("keyframes") or ():
            absolute_at = origin + float(frame.get("time") or 0)
            for index, window in enumerate(submission_windows, start=1):
                start = float(window.get("start") or 0)
                end = float(window.get("end") or 0)
                if start <= absolute_at <= end:
                    submitted_keyframes.append({
                        "window": index,
                        "at": round(absolute_at - start, 3),
                    })
                    break
        shaped["submitted_intervals"] = submitted_intervals
        shaped["submitted_keyframes"] = submitted_keyframes
        enriched.append(shaped)
    return enriched


def extract_reference_frames(
    *,
    frame_times: list[float],
    clips: list[dict[str, Any]],
    assets_by_id: dict[str, dict[str, Any]],
    guides: list[dict[str, Any]],
    asset_store,
    work: str,
    canvas: tuple[int, int],
    fps: float,
    submission_windows: list[dict[str, Any]] | None = None,
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """Render selected times exactly as the current editor canvas displays them."""
    if not frame_times:
        return [], [], []
    if asset_store is None or ffmpeg_ops.ffmpeg_bin() is None:
        return [], [], ["缺 ffmpeg 或 AssetStore，代表帧未截取"]

    prepared_clips, prepared_guides = prepare_visible_context(
        clips=clips,
        guides=guides,
        assets_by_id=assets_by_id,
    )
    total = max(
        (
            float(clip.get("start") or 0) + float(clip.get("duration") or 0)
            for clip in layer_compose.visible_layers(prepared_clips)
        ),
        default=0.0,
    )
    frame_span = max(0.04, 1.0 / max(float(fps), 1.0))
    paths: list[str] = []
    facts: list[dict[str, Any]] = []
    warnings: list[str] = []
    for idx, at in enumerate(frame_times):
        requested_at = max(0.0, float(at))
        display_at = min(requested_at, max(0.0, total - frame_span / 2.0))
        clip = layer_compose.topmost_at(prepared_clips, display_at)
        if clip is None:
            warnings.append(f"{at}s 无可见图层，跳过代表帧")
            continue
        asset_id = str(clip.get("assetId") or clip.get("asset_id") or "")
        sample_start = display_at
        sample_end = min(total, sample_start + frame_span)
        if sample_end - sample_start <= 0.01:
            sample_start = max(0.0, display_at - frame_span)
            sample_end = min(total, max(display_at, sample_start + frame_span))
        plan = layer_compose.compose_plan(prepared_clips, sample_start, sample_end)
        if not plan.get("valid"):
            warnings.append(f"{at}s 当前画面无法合成，跳过代表帧")
            continue
        frame_work = os.path.join(work, f"frame-{idx}")
        os.makedirs(frame_work, exist_ok=True)
        out = os.path.join(work, f"reference-{idx}.png")
        try:
            composed = render.compose_span(
                plan,
                frame_work,
                fetch_bytes=lambda ref: _asset_bytes(asset_store, dict(ref)),
                clips_by_id={
                    str(item.get("id") or ""): item for item in prepared_clips
                },
                canvas=(int(canvas[0]), int(canvas[1])),
                fps=float(fps),
            )
            guide_render.render_modified_frame(
                composed,
                out,
                at_local=max(0.0, display_at - sample_start),
                guides=prepared_guides,
                global_at=requested_at,
                asset_store=asset_store,
                work=frame_work,
                canvas=(int(canvas[0]), int(canvas[1])),
                fps=float(fps),
            )
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{at}s 合成代表帧失败: {type(exc).__name__}")
            continue
        paths.append(out)
        facts.append({
            "at": round(requested_at, 3),
            "file": Path(out).name,
            "clipId": str(clip.get("id") or ""),
            "assetId": asset_id,
            "composite": True,
            **next(
                (
                    {
                        "submissionWindow": index,
                        "submittedAt": round(
                            requested_at - float(window.get("start") or 0), 3,
                        ),
                    }
                    for index, window in enumerate(submission_windows or [], start=1)
                    if float(window.get("start") or 0)
                    <= requested_at
                    <= float(window.get("end") or 0)
                ),
                {},
            ),
        })
    return paths, facts, warnings


def cancel_prompt_optimize(ctx) -> None:
    """Stage cancel_handler: signal the in-flight Cursor/CLI wait to abort."""
    event = _ACTIVE_CANCEL.get(id(ctx))
    if event is not None:
        event.set()


def run_prompt_optimize_stage(
    ctx, args: dict[str, Any], *, config=None, asset_store=None,
) -> dict[str, Any]:
    provider = str(args.get("optimizer_provider") or "").strip()
    model = str(args.get("optimizer_model") or "").strip()
    cancel_event = threading.Event()
    _ACTIVE_CANCEL[id(ctx)] = cancel_event
    try:
        ctx.checkpoint()
        wants_video = provider in _VIDEO_OPTIMIZER_PROVIDERS
        temp_parent = _managed_temp_parent() if wants_video else None
        with tempfile.TemporaryDirectory(
            prefix="opt-" if temp_parent else "ze-prompt-opt-",
            dir=str(temp_parent) if temp_parent else None,
        ) as work:
            video_paths: list[str] = []
            video_facts: list[dict[str, Any]] = []
            if wants_video:
                video_paths, video_facts = prepare_optimizer_videos(
                    submission_windows=list(args.get("submission_windows") or []),
                    clips=list(args.get("clips") or []),
                    assets_by_id=dict(args.get("assets_by_id") or {}),
                    guides=list(args.get("guide_facts") or []),
                    asset_store=asset_store,
                    work=work,
                    canvas=tuple(args.get("canvas") or [0, 0]),
                    fps=float(args.get("fps") or 30.0),
                )
            # 有合成视频后不再抽代表帧：视频优先，避免 DomainError 噪音。
            image_paths: list[str] = []
            frame_facts: list[dict[str, Any]] = []
            frame_warnings: list[str] = []
            if not video_paths:
                frame_times = select_frame_times(
                    reference_times=[float(v) for v in (args.get("frame_times") or [])],
                    guide_facts=list(args.get("guide_facts") or []),
                )
                image_paths, frame_facts, frame_warnings = extract_reference_frames(
                    frame_times=frame_times,
                    clips=list(args.get("clips") or []),
                    assets_by_id=dict(args.get("assets_by_id") or {}),
                    guides=list(args.get("guide_facts") or []),
                    asset_store=asset_store,
                    work=work,
                    canvas=tuple(args.get("canvas") or [0, 0]),
                    fps=float(args.get("fps") or 30.0),
                    submission_windows=list(args.get("submission_windows") or []),
                )
            payload = dict(args)
            payload["frame_facts"] = frame_facts
            payload["video_facts"] = video_facts
            strategy, prompt = render_prompt(payload)
            ctx.log(
                "info",
                f"整理薄契约、{len(video_paths)} 段视频"
                f"与 {len(image_paths)} 张代表帧",
                progress_scope="indeterminate",
            )
            ctx.checkpoint()
            # Cursor Key 只在本工具私有库;注入后不读 Hub context store。
            cursor_store = (
                cursor_credentials.cursor_credential_store()
                if cursor_credentials.is_cursor_provider(provider)
                else None
            )
            output = user_llm_schema_call(
                provider=provider,
                prompt=prompt,
                output_schema=runner.output_schema_for(strategy),
                config=config,
                model=model,
                image_paths=image_paths,
                video_paths=video_paths,
                timeout=300,
                cwd=Path(work) if video_paths else None,
                cancel_event=cancel_event,
                credential_store=cursor_store,
            )
            ctx.checkpoint()
            valid, errors = runner.validate_output(output, strategy)
            if not valid:
                raise ZeError(
                    "model_call_failed",
                    "提示词优化结果不符合结构化契约",
                    context={"errors": errors[:10], "provider": provider},
                )
            optimized = str(output["optimized_prompt"]).strip()
            warnings = [str(item) for item in output.get("warnings") or []] + frame_warnings
            facts_used = [str(item) for item in output.get("facts_used") or []]
            if frame_facts:
                facts_used.append(f"代表帧×{len(frame_facts)}")
            if video_facts:
                facts_used.append(f"自主分析视频×{len(video_facts)}")
            ctx.log(
                "info",
                "提示词优化完成",
                progress=1.0,
                optimizer_provider=provider,
                optimizer_model=model,
                frame_count=len(frame_facts),
                video_count=len(video_facts),
            )
            return {
                "patches": {
                    "status": "succeeded",
                    "optimizedPrompt": optimized,
                    "warningsJson": json.dumps(warnings, ensure_ascii=False),
                    "factsUsedJson": json.dumps(facts_used, ensure_ascii=False),
                    "lastError": "",
                }
            }
    except CancelledError as exc:
        cancel_event.set()
        return {
            "patches": {
                "status": "cancelled",
                "lastError": _brief(str(exc) or "已取消"),
                "warningsJson": "[]",
                "factsUsedJson": "[]",
            }
        }
    except ZeError as exc:
        code = getattr(exc, "code", "")
        status = "cancelled" if code == "cancelled" else "failed"
        return {
            "patches": {
                "status": status,
                "lastError": _brief(str(exc)),
                "warningsJson": "[]",
                "factsUsedJson": "[]",
            }
        }
    except Exception as exc:  # keep provider/library details out of persisted drafts
        return {
            "patches": {
                "status": "failed",
                "lastError": _brief(f"{type(exc).__name__}: {exc}"),
                "warningsJson": "[]",
                "factsUsedJson": "[]",
            }
        }
    finally:
        _ACTIVE_CANCEL.pop(id(ctx), None)
