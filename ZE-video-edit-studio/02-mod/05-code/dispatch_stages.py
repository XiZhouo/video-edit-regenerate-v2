"""dispatch_stages — the send.dispatch slow stage (video service call).

Runs in the supervisor worker thread: eats a payload, never touches ``doc``,
returns ``{patches, created_entities, name}`` for the supervisor to commit
through the command channel. Follows gen-timeline's gen_stages discipline:

- Dependencies (client / asset_store / sleeper / clock) are parameter-injected
  so the classification logic is unit-tested with fakes — zero framework
  monkeypatching, no live service.
- Video service unreachable / transport error → **requires_runtime** patches
  (retryable/config problem). Service says failed → **failed** patches. The
  client raises; this stage classifies (client-raises / stage-classifies).
- The first successful result on an empty target auto-adopts (confirmation #11):
  the candidate is created AND the target clip's videoRef is set in one commit.
"""

from __future__ import annotations

import os
import tempfile
import time
from typing import Any, Callable

from _domain import candidate_flow, ffmpeg_ops, videogen_client
from _domain.errors import DomainError, RequiresRuntime

_TRANSPORT_ERRORS = (TimeoutError, ConnectionError, OSError)

RUNTIME_HINT = "视频生成服务不可达或未配置;确认 env ZE_VIDEOGEN_BASE_URL 指向可用网关"


def _brief(text: str, limit: int = 280) -> str:
    t = " ".join((text or "").split())
    return t[:limit] + ("…" if len(t) > limit else "")


def _requires_runtime(exc: Exception) -> dict[str, Any]:
    msg = _brief(str(exc)) or exc.__class__.__name__
    return {"patches": {"status": "requires_runtime", "lastError": f"{msg} — {RUNTIME_HINT}"}}


def _failed(message: str) -> dict[str, Any]:
    return {"patches": {"status": "failed", "lastError": _brief(message) or "生成失败"}}


def render_ai_surface(args: dict[str, Any], asset_store) -> dict[str, Any] | None:
    """做出模式一真正要发的那段视频:按区间截段 + 把标注烧进画面,产物**入库**。

    入库(而不是发完就扔)是为了「人审的就是真发的」—— 你能点开看到我们究竟
    把什么画面送了出去。缺原视频或缺标注返回 ``None``,由调用方说清原因。

    从前这一步根本不存在:``send_compiler`` 只在文件清单里放了一个占位名
    ``__ai_surface_segment__``,而派发时解析文件又会把 ``__`` 开头的名字跳过 ——
    于是模式一发出去永远是「一段提示词 + 零文件」。
    """
    source = args.get("source_video_ref")
    annotations = args.get("annotations") or []
    if not (isinstance(source, dict) and source.get("sha256")) or not annotations:
        return None
    if asset_store is None:
        raise RequiresRuntime("AssetStore 未注入,无法读原视频/登记标注面")

    interval = args.get("send_interval") or {}
    start = float(interval.get("start") or 0.0)
    duration = interval.get("duration")

    with tempfile.TemporaryDirectory(prefix="ze-ves-surface-") as work:
        src = os.path.join(work, "source.mp4")
        with open(src, "wb") as fh:
            fh.write(asset_store.get(source["sha256"]))
        stage_in = src
        if duration:
            cut = os.path.join(work, "segment.mp4")
            ffmpeg_ops.cut_segment(src, cut, start, float(duration))
            stage_in = cut
        burned = os.path.join(work, "ai_surface.mp4")
        # 标注区间是素材坐标系的时间;烧到截好的段上要减掉截段起点。
        ffmpeg_ops.burn_annotations(stage_in, burned, annotations,
                                    time_offset=start if duration else 0.0)
        with open(burned, "rb") as fh:
            return asset_store.put(fh.read(), mime="video/mp4", ext=".mp4").to_dict()


def _submit_payload(client, asset_store, *, model: str, mode: str, prompt: str,
                    ratio: str, duration: Any, refs: list) -> dict[str, Any]:
    """提交。有真文件就走 multipart(``file[]``),没有就走纯 JSON。

    服务收 URL 或 data URI;我们手上是资产库里的二进制,所以落成临时文件走
    multipart —— 从前这里把 sha256 字符串直接塞进 JSON 的 ``files``,
    服务根本解析不了(只是因为清单一直是空的,才没暴露)。
    """
    materialised = [r for r in refs if isinstance(r, dict) and r.get("sha256")]
    if not materialised:
        return client.submit(model=model, mode=mode, prompt=prompt, ratio=ratio,
                             duration=duration, file_refs=None)
    with tempfile.TemporaryDirectory(prefix="ze-ves-send-") as work:
        paths = []
        for idx, ref in enumerate(materialised):
            path = os.path.join(work, f"input_{idx}.bin")
            with open(path, "wb") as fh:
                fh.write(asset_store.get(ref["sha256"]))
            paths.append(path)
        return client.submit_files(model=model, mode=mode, prompt=prompt,
                                   file_paths=paths, ratio=ratio, duration=duration)


def run_dispatch_stage(ctx, args: dict[str, Any], *, client=videogen_client,
                       asset_store=None, sleeper: Callable[[float], None] = time.sleep,
                       poll_interval: float = 12.0, poll_deadline: float = 1800.0,
                       now: Callable[[], float] = time.time) -> dict[str, Any]:
    """Submit → poll → (download) → candidate, classifying failures structurally.

    ``args`` payload keys: prompt, model, mode, ratio, duration, file_refs,
    target_clip_id, target_has_video, scope, job_id. Returns the supervisor
    commit dict. Never raises for a gateway problem — those become structured
    patches; only genuine bugs propagate.
    """
    prompt = str(args.get("prompt") or "").strip()
    model = str(args.get("model") or "")
    mode = args.get("mode")
    if not prompt:
        return _failed("发送快照没有提示词;请先 send.compile")

    refs = list(args.get("file_refs") or [])
    surface_ref = None
    if int(mode or 0) == 1:
        ctx.log("info", "渲染标注面(截段 + 烧框)", progress_scope="indeterminate")
        try:
            surface_ref = render_ai_surface(args, asset_store)
        except RequiresRuntime as exc:
            return {"patches": {"status": "requires_runtime",
                                "lastError": f"{_brief(str(exc))} — 模式一要本地 ffmpeg 才能烧录标注"}}
        except (DomainError, ValueError, OSError) as exc:
            return _failed(f"渲染标注面失败: {exc}")
        if surface_ref is None:
            return _failed("模式一缺原视频或标注 —— 把标注挂到有素材的片段上再发")
        refs = [surface_ref]     # 模式一发的就是这一段,不带别的

    ctx.log("info", "submit", progress_scope="indeterminate")
    try:
        submit_resp = _submit_payload(client, asset_store, model=model,
                                      mode=_service_mode(mode), prompt=prompt,
                                      ratio=str(args.get("ratio") or "16:9"),
                                      duration=args.get("duration"), refs=refs)
    except _TRANSPORT_ERRORS as exc:
        return _requires_runtime(exc)

    task_id = client.task_id_of(submit_resp)
    if not task_id:
        status = client.status_of(submit_resp)
        if status == "failed":
            return _failed(client.error_message_of(submit_resp))
        return _failed("网关未返回 task_id")

    # poll loop with cancellation-respecting checkpoint
    deadline = now() + poll_deadline
    while now() < deadline:
        ctx.checkpoint()
        try:
            resp = client.poll(task_id)
        except _TRANSPORT_ERRORS as exc:
            return _requires_runtime(exc)
        status = client.status_of(resp)
        progress = _progress(resp)
        if progress is not None:
            ctx.set_progress(progress, label="生成中")
        if status in ("completed", "succeeded"):
            return _on_success(resp, args, client, asset_store, task_id)
        if status == "failed":
            return _failed(client.error_message_of(resp))
        _sleep_checkpoint(ctx, poll_interval, sleeper)
    return _failed("生成轮询超时")


def _on_success(resp, args, client, asset_store, task_id) -> dict[str, Any]:
    url = client.result_url_of(resp)
    if not url:
        return _failed("网关完成但未返回结果地址")
    # A candidate must carry a content-addressed asset_ref to be adoptable, so the
    # result is downloaded and stored. Without an asset store we cannot produce a
    # valid ref → requires_runtime (retry once the store is available).
    if asset_store is None:
        return {"patches": {"status": "requires_runtime",
                            "lastError": "AssetStore 未注入,无法落库候选视频"}}
    try:
        data = client.download(url)
        video_ref = asset_store.put(data, ext=".mp4").to_dict()
    except _TRANSPORT_ERRORS as exc:
        return _requires_runtime(exc)
    except Exception as exc:  # non-transport download problem is a real failure
        return _failed(f"下载失败:{exc}")

    auto = candidate_flow.should_auto_adopt(
        target_has_video=bool(args.get("target_has_video")),
        is_regeneration=bool(args.get("is_regeneration")),
        auto_adopt_enabled=bool(args.get("auto_adopt_enabled", True)))

    # 候选永不自动写正本:supervisor 的 stage 提交契约只消费 patches(打到本 WorkItem
    # 绑定的实体)/ created_entities / name / ai_provenance —— stage **无法**跨实体 patch
    # 目标 clip。所以「空段首结果自动采纳」在 stage 内不可能真写 clip.videoRef;这里只把
    # 建议落在候选上(autoAdoptSuggested),真正写正本仍走 candidate.adopt 一次调用
    # (人点/AI 调,都是同一命令通道)。这既守住候选制,也不假装自动采纳已生效。
    candidate_data = {k: v for k, v in {
        "scope": args.get("scope") or "region",
        "videoRef": video_ref,
        "sourceSendJobId": args.get("job_id"),
        "targetClipId": args.get("target_clip_id"),
        "stale": False,
        "adopted": False,
        "autoAdoptSuggested": bool(auto),
    }.items() if v is not None}
    patches: dict[str, Any] = {"status": "succeeded", "taskId": task_id, "lastError": ""}
    if auto and args.get("target_clip_id"):
        patches["pendingAdoptHint"] = "空段首结果:建议采纳(candidate.adopt 写入正片)"
    return {
        "patches": patches,
        "created_entities": [{"type": "candidate", "name": "生成候选", "data": candidate_data}],
        "name": "生成候选",
    }


def _service_mode(mode: Any) -> str:
    """Map edit-studio mode number → video-service mode string."""
    return {1: "edit", 2: "reference-to-video", 3: "reference-to-video"}.get(mode, "text-to-video")


def _progress(resp: dict) -> float | None:
    raw = resp.get("progress")
    if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    val = float(raw)
    if val != val or val in (float("inf"), float("-inf")):
        return None
    if val > 1.0:
        val = val / 100.0
    return max(0.0, min(1.0, val))


def _sleep_checkpoint(ctx, seconds: float, sleeper: Callable[[float], None]) -> None:
    remaining = seconds
    while remaining > 0:
        ctx.checkpoint()
        step = min(1.0, remaining)
        sleeper(step)
        remaining -= step
    ctx.checkpoint()
