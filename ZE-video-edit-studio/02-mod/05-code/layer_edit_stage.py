"""layer_edit_stage — 「把这一段发给 AI 改」的慢 stage。

## 这一步做什么

时间线是一摞图层。人圈了一块、定了一段,点发送,这里做四件事:

1. **合成**:按 `layer_compose.compose_plan` 把「可见图层 × 选定区间」拼成一段
   (每一刻取最上面那个可见图层;隐藏的行不参与)
2. **烧标注**:把框画进画面,颜色与提示词里说的一致
3. **发**:按所选模型的协议分流字段，交给 Omni 或 Seedance
4. **回来插最上层**:结果作为一个**新图层**落在时间线上

## 为什么没有「候选 / 采纳」

结果本身就是一个新图层,盖在原片上面。要就留着,不要就删掉那一行,
想看原片把上面那层的眼睛关掉 —— 图层栈自己就是候选制,不需要再造一套。

纪律与域层一致:客户端抛、stage 分类。缺 ffmpeg / 服务不可达 → `requires_runtime`;
服务明确失败 → `failed`。绝不伪造结果。
"""
from __future__ import annotations

import os
import tempfile
import time
import urllib.error
import uuid
from pathlib import Path
from typing import Any, Callable

from _domain import ffmpeg_ops, render, videogen_client
from _domain.errors import DomainError, RequiresRuntime
import generation_contract
import guide_render

_TRANSPORT_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, OSError)

RUNTIME_HINT = ("需要本地 ffmpeg 合成这一段 + 视频服务做编辑;"
                "确认 ffmpeg 可用与 ZE_VIDEOGEN_BASE_URL 指向可用服务")


def _write_artifact(args: dict[str, Any], text: str) -> None:
    """把这一趟的收据写进 WorkItem 声明的产物文件。

    **不写就等于没跑完**:框架在 stage 返回后核对「声明的产物是否真的落地」,
    缺了就判 `pipeline_completion_repair_required`(artifact_output_missing)——
    结果视频明明拿回来了、图层也落了,任务却报红。这一步是审计链的一环,不是可选装饰。
    """
    artifacts = args.get("_artifacts") or ()
    if not artifacts:
        return
    path = Path(artifacts[0])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _result_name(annotations: list, prompt: str) -> str:
    """结果图层叫什么:优先用标注的文字(人写的那句),多条就串起来。"""
    labels = [str(a.get("label") or "").strip() for a in annotations]
    labels = [x for x in labels if x]
    if labels:
        return _brief("、".join(labels), 28)
    return _brief(prompt.splitlines()[-1] if prompt else "AI 改过", 28)


def _brief(text: str, limit: int = 280) -> str:
    t = " ".join((text or "").split())
    return t[:limit] + ("…" if len(t) > limit else "")


def _requires_runtime(args: dict[str, Any], msg: str) -> dict[str, Any]:
    _write_artifact(args, f"requires_runtime: {_brief(msg)}\n")
    return {"patches": {"status": "requires_runtime",
                        "lastError": f"{_brief(msg)} — {RUNTIME_HINT}"}}


def _failed(args: dict[str, Any], msg: str) -> dict[str, Any]:
    _write_artifact(args, f"failed: {_brief(msg)}\n")
    return {"patches": {"status": "failed", "lastError": _brief(msg) or "编辑失败"}}


def compose_segment(asset_store, plan: dict[str, Any], work: str, *,
                    canvas: tuple[int, int] = (0, 0), fps: float = 30.0,
                    clips_by_id: dict[str, Any] | None = None) -> str:
    """把这一段拼出来 —— 直接用域层渲染器,与**导出走的是同一条链**。

    合成逻辑(逐刀截 → 按该层几何摆进画布 → 空档补黑帧 → 拼接)住在
    ``ze_video_domain.render.compose_span``;这里只负责把 AssetStore 的取字节
    能力接进去。两边同源,就不会出现「发出去的和导出的不是同一套几何」。
    """
    from sdk import AssetRef

    return render.compose_span(
        plan, work,
        fetch_bytes=lambda ref: asset_store.get(AssetRef.from_dict(ref)),
        clips_by_id=clips_by_id or {},
        canvas=(int(canvas[0] or 0), int(canvas[1] or 0)), fps=fps)


def run_edit_stage(ctx, args: dict[str, Any], *, asset_store=None,
                   client=videogen_client,
                   sleeper: Callable[[float], None] = time.sleep,
                   poll_interval: float = 12.0,
                   poll_deadline: float = 1800.0,
                   now: Callable[[], float] = time.time) -> dict[str, Any]:
    """合成 → 烧标注 → 发 → 新图层。缺运行时就结构化降级,不编结果。"""
    plan = args.get("plan") or {}
    if not plan.get("valid"):
        return _failed(args, plan.get("reason") or "这一段合成不出来")
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return _failed(args, "没有提示词;先写一句要改成什么")
    if asset_store is None or ffmpeg_ops.ffmpeg_bin() is None:
        return _requires_runtime(args, "缺 ffmpeg 或 AssetStore")

    span = plan.get("span") or [0.0, 0.0]
    annotations = args.get("annotations") or []
    model = str(args.get("model") or "")
    provider = str(args.get("provider") or generation_contract.infer_provider(model))
    strategy = str(args.get("strategy") or generation_contract.ANNOTATED_VIDEO)
    try:
        generation_contract.validate_profile(provider, model, strategy)
    except ValueError as exc:
        return _failed(args, str(exc))
    span_duration = float(span[1]) - float(span[0])
    try:
        submission = generation_contract.compile_submission_request(
            provider=provider,
            model=model,
            strategy=strategy,
            ratio=str(args.get("ratio") or "16:9"),
            span_duration=span_duration,
        )
    except (TypeError, ValueError) as exc:
        return _failed(args, str(exc))

    with tempfile.TemporaryDirectory(prefix="ze-ves-layer-") as work:
        ctx.log("info", "合成这一段(可见图层)", progress_scope="indeterminate")
        canvas = args.get("canvas") or [0, 0]
        fps = float(args.get("fps") or 30.0)
        try:
            composed = compose_segment(asset_store, plan, work,
                                       canvas=(int(canvas[0] or 0), int(canvas[1] or 0)),
                                       fps=fps, clips_by_id=args.get("clips_by_id") or {})
        except RequiresRuntime as exc:
            return _requires_runtime(args, str(exc))
        except (DomainError, OSError, KeyError, ValueError) as exc:
            return _failed(args, f"合成失败: {exc}")

        files_to_send: list[str] = []
        reference_times = [float(v) for v in (args.get("reference_times") or [])]
        if strategy == generation_contract.ANNOTATED_VIDEO:
            ctx.log("info", "渲染带画面引导的视频", progress_scope="indeterminate")
            try:
                burned = os.path.join(work, "with_guides.mp4")
                # 旧项目只有矩形框时保留既有轻量 ffmpeg 路径；出现图片、文字或路径
                # 才进入通用透明引导渲染器。兼容旧数据，也避免无谓增加视频输入数。
                if (
                    all(str(item.get("kind") or "box") == "box" for item in annotations)
                    and not any(item.get("keyframes") for item in annotations)
                ):
                    ffmpeg_ops.burn_annotations(
                        composed, burned, annotations,
                        time_offset=float(span[0]), fps=fps,
                        canvas_w=float(canvas[0] or 0),
                        canvas_h=float(canvas[1] or 0),
                    )
                else:
                    guide_render.burn_guides(
                        composed,
                        burned,
                        annotations,
                        asset_store=asset_store,
                        work=work,
                        canvas=(int(canvas[0] or 0), int(canvas[1] or 0)),
                        time_offset=float(span[0]),
                        fps=fps,
                    )
                files_to_send = [burned]
            except (DomainError, RequiresRuntime, ValueError, OSError) as exc:
                return _failed(args, f"画面引导渲染失败: {exc}")
        else:
            if not reference_times:
                return _failed(args, "修改画面方式缺少参考画面时间点")
            ctx.log("info", f"渲染 {len(reference_times)} 张修改画面",
                    progress_scope="indeterminate")
            files_to_send = [composed]
            try:
                for idx, at in enumerate(reference_times):
                    frame = os.path.join(work, f"modified_frame_{idx:03d}.png")
                    guide_render.render_modified_frame(
                        composed,
                        frame,
                        at_local=max(0.0, at - float(span[0])),
                        guides=annotations,
                        global_at=at,
                        asset_store=asset_store,
                        work=work,
                        canvas=(int(canvas[0] or 0), int(canvas[1] or 0)),
                        fps=fps,
                    )
                    files_to_send.append(frame)
            except (DomainError, RequiresRuntime, ValueError, OSError) as exc:
                return _failed(args, f"修改画面渲染失败: {exc}")

        ctx.log("info", "发给 AI 编辑", progress_scope="indeterminate")
        submitted_reference_times = [
            max(0.0, at - float(span[0])) for at in reference_times
        ]
        compiled_prompt = generation_contract.decorate_prompt(
            prompt,
            provider=provider,
            strategy=strategy,
            frame_times=submitted_reference_times,
        )
        try:
            resp = client.submit_files(
                model=model,
                prompt=compiled_prompt,
                file_paths=files_to_send,
                **submission,
            )
        except videogen_client.SubmissionValidationError as exc:
            return _failed(args, str(exc))
        except _TRANSPORT_ERRORS as exc:
            return _requires_runtime(args, str(exc))

        task_id = client.task_id_of(resp)
        if str(resp.get("status") or "") == "failed":
            return _failed(args, client.error_message_of(resp) or "服务拒绝了这次编辑")
        url = client.result_url_of(resp)
        if not task_id and not url:
            return _failed(args, f"服务未返回 task_id: {resp}")

        if task_id and not url:
            deadline = now() + poll_deadline
            while True:
                ctx.checkpoint()
                sleeper(poll_interval)
                try:
                    resp = client.poll(task_id)
                except _TRANSPORT_ERRORS as exc:
                    return _requires_runtime(args, str(exc))
                status = str(resp.get("status") or "")
                ctx.log("info", f"生成状态: {status or '未知'}",
                        progress_scope="provider_generation")
                if status == "failed":
                    return _failed(args, client.error_message_of(resp) or "编辑失败")
                if status == "completed":
                    break
                if now() > deadline:
                    return _failed(args, f"编辑超时(>{int(poll_deadline)}s): {task_id}")
            url = client.result_url_of(resp)
        if not url:
            return _failed(args, f"completed 但没有产物 URL: {resp}")

        ctx.log("info", "下载并落成新图层", progress_scope="indeterminate")
        try:
            data = client.download(url)
        except _TRANSPORT_ERRORS as exc:
            return _failed(args, f"产物下载失败(可重试): {exc}")

    video_ref = asset_store.put(data, mime="video/mp4", ext=".mp4").to_dict()
    _write_artifact(args, "\n".join([
        f"span: {span[0]}–{span[1]}s",
        f"provider: {provider}",
        f"strategy: {strategy}",
        f"model: {model}",
        f"guides: {len(annotations)}",
        f"reference_frames: {len(args.get('reference_times') or [])}",
        f"result_sha256: {video_ref.get('sha256', '')}",
        f"new_layer: {args.get('new_layer')}",
        "prompt:", prompt, "",
    ]))
    # 结果 = 一个新图层,插在最上面。播放时自动看到它,因为它盖住了下面。
    return {
        "patches": {"status": "succeeded", "lastError": ""},
        "created_entities": [{
            "type": "clip",
            "id": f"clip-{uuid.uuid4().hex[:10]}",
            # 名字用标注自己的文字,不用提示词首行 —— 提示词开头是自动拼的
            # 「1. 画面中的红色矩形框:…」,拿它当图层名又长又是机器话。
            "name": _result_name(annotations, prompt),
            "data": {
                "layer": int(args.get("new_layer") or 0),
                "visible": True,
                "start": float(span[0]),
                "duration": round(float(span[1]) - float(span[0]), 2),
                "sourceIn": 0.0,
                "xKind": "gen",
                "videoRef": video_ref,
            },
        }],
    }
