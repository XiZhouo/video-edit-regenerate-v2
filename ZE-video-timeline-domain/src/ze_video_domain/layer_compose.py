"""图层合成 —— 时间线是一摞图层,不是一堆候选。

## 模型

时间线每一行是一个视频片段(图层)。每行可以隐藏。**没隐藏的行才参与合成。**
上层盖下层:任意时刻,画面上看到的是那一刻**最上面那个可见图层**。

编辑一段的做法:把「可见图层 × 选定区间」合成出一段 → 烧上标注 → 发给 AI →
**回来的结果作为一个新图层插到最上面**。播放时自动看到最新的,因为它盖在上面。

## 这个模型替掉了什么

原来那套「候选列表 + 采纳按钮 + 回贴策略 + 对比态」全部不需要:

- 候选制? 图层栈本身就是 —— 新图层盖在上面,原片一直在下面,没覆盖任何东西。
- 采纳? 它已经在时间线上了。要就留着,不要就删掉那一层。
- 对比? 把上面那层的眼睛关掉,看到的就是下面的原片。
- 回贴策略? 图层叠加就是回贴。

## 本模块只回答一件事

给定一摞图层和一个时间区间,**这一段该由哪些片段的哪些部分拼起来**。
纯函数,不碰 ffmpeg、不碰文件 —— 拼接由调用方拿这份计划去执行。
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import clip_math


def _span(clip: Mapping[str, Any]) -> tuple[float, float]:
    start = float(clip.get("start") or 0.0)
    return start, start + float(clip.get("duration") or 0.0)


def visible_layers(clips: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """参与合成的图层,**从下往上**排序(layer 小的在下)。

    隐藏的行不参与 —— 这是人控制"这次发什么"的唯一开关。
    """
    live = [dict(c) for c in clips
            if c.get("visible", True) and not c.get("deleted")
            and float(c.get("duration") or 0.0) > 0]
    return sorted(live, key=lambda c: (float(c.get("layer") or 0), str(c.get("id") or "")))


def topmost_at(clips: Sequence[Mapping[str, Any]], t: float) -> dict[str, Any] | None:
    """``t`` 时刻画面上看到的是哪一层 —— 最上面那个盖住这一刻的可见图层。"""
    hit = [c for c in visible_layers(clips) if _span(c)[0] <= t < _span(c)[1]]
    return hit[-1] if hit else None


def compose_plan(clips: Sequence[Mapping[str, Any]],
                 t0: float, t1: float) -> dict[str, Any]:
    """``[t0, t1)`` 这一段该怎么拼。

    在区间内按"最上层可见图层"切段:每当最上层换人,就切一刀。返回::

        {"valid": bool, "reason": str, "span": [t0, t1],
         "cuts": [{"clip_id", "asset_id", "video_ref",
                   "timeline_start", "timeline_end",     # 在时间线上的位置
                   "source_in", "source_out"}],          # 在该素材内部取哪一段
         "gaps": [[a, b], …]}                            # 没有任何可见图层盖住的空档

    空档不是错误(时间线本来可以有洞),但要如实报出来 —— 拼出来会短一截,
    调用方得知道,不能让人以为发出去的是连续的一段。
    """
    t0, t1 = float(t0), float(t1)
    if t1 - t0 <= 0.01:
        return {"valid": False, "reason": "选的这一段是空的;在时间线上拖出一段再发",
                "span": [t0, t1], "cuts": [], "gaps": []}

    layers = visible_layers(clips)
    if not layers:
        return {"valid": False, "reason": "没有可见图层 —— 至少要有一行没被隐藏",
                "span": [t0, t1], "cuts": [], "gaps": []}

    # 边界 = 区间两端 + 每个图层的起止(落在区间内的)。最上层只可能在这些点上换人。
    marks = {t0, t1}
    for clip in layers:
        for edge in _span(clip):
            if t0 < edge < t1:
                marks.add(edge)
    ordered = sorted(marks)

    cuts: list[dict[str, Any]] = []
    gaps: list[list[float]] = []
    for left, right in zip(ordered, ordered[1:]):
        if right - left <= 1e-6:
            continue
        top = topmost_at(layers, (left + right) / 2.0)
        if top is None:
            gaps.append([clip_math.round2(left), clip_math.round2(right)])
            continue
        clip_start = _span(top)[0]
        source_in = float(top.get("sourceIn") or top.get("source_in") or 0.0)
        # 相邻两刀若还是同一层,合并 —— 别切出一堆没必要的碎段。
        if cuts and cuts[-1]["clip_id"] == top.get("id") and \
                abs(cuts[-1]["timeline_end"] - left) < 1e-6:
            cuts[-1]["timeline_end"] = clip_math.round2(right)
            cuts[-1]["source_out"] = clip_math.round2(source_in + (right - clip_start))
            continue
        cuts.append({
            "clip_id": top.get("id"),
            "asset_id": top.get("assetId") or top.get("asset_id"),
            "video_ref": top.get("videoRef") or top.get("video_ref"),
            "timeline_start": clip_math.round2(left),
            "timeline_end": clip_math.round2(right),
            "source_in": clip_math.round2(source_in + (left - clip_start)),
            "source_out": clip_math.round2(source_in + (right - clip_start)),
        })

    if not cuts:
        return {"valid": False, "reason": "选的这一段上没有任何可见图层",
                "span": [t0, t1], "cuts": [], "gaps": gaps}
    return {"valid": True, "reason": "", "span": [clip_math.round2(t0), clip_math.round2(t1)],
            "cuts": cuts, "gaps": gaps}


def next_layer(clips: Sequence[Mapping[str, Any]]) -> int:
    """新图层该放第几层 —— 最上面。AI 的结果回来就插这儿,所以播放时自动看到最新的。"""
    if not clips:
        return 0
    return int(max(float(c.get("layer") or 0) for c in clips)) + 1


def exposed_windows(clips: Sequence[Mapping[str, Any]],
                    annotations: Sequence[Mapping[str, Any]],
                    t0: float, t1: float) -> dict[Any, list[list[float]]]:
    """每条标注在 ``[t0, t1)`` 里**真正露出来**的那些小段。

    一根数轴,两种不透明度:

    * **视频片段**满屏不透明 —— 盖住它下面的一切(下面的视频层、下面的标注框)
    * **标注**只是一个框 —— 不盖任何东西,所以标注之间互不遮挡

    于是某一刻画面 = 最上面那个可见视频层;画在它上面的框 = 层序比它大的可见标注。
    层序比它小的被盖住了 —— 画面上看不见,**发送时就不该存在**。
    这就是「上面看见的是什么,就发送什么」。

    返回 ``{annotation_id: [[a,b], ...]}``;完全被盖住的标注不出现在结果里。
    """
    t0, t1 = float(t0), float(t1)
    layers = visible_layers(clips)

    # 边界 = 区间两端 + 每个视频层的起止(落在区间内的)。最上层只在这些点上换人。
    marks = {t0, t1}
    for clip in layers:
        for edge in _span(clip):
            if t0 < edge < t1:
                marks.add(edge)
    ordered = sorted(marks)

    out: dict[Any, list[list[float]]] = {}
    for ann in annotations:
        if not ann.get("visible", True) or ann.get("deleted"):
            continue
        interval = ann.get("interval")
        if not (isinstance(interval, (list, tuple)) and len(interval) == 2):
            continue
        a0, a1 = float(interval[0]), float(interval[1])
        ann_layer = float(ann.get("layer") or 0)
        pieces: list[list[float]] = []
        for left, right in zip(ordered, ordered[1:]):
            if right - left <= 1e-6:
                continue
            top = topmost_at(layers, (left + right) / 2.0)
            # 这一小段上面压着的那层视频,层序比标注还高 → 标注被盖住,这段不算
            if top is not None and float(top.get("layer") or 0) > ann_layer:
                continue
            # 与标注自己的时间范围求交(单帧标注 a0==a1 也算命中它落在的那一小段)
            lo, hi = max(left, a0), min(right, a1)
            if a0 == a1 and left <= a0 <= right:
                lo, hi = a0, a0
            elif hi <= lo:
                continue
            if pieces and lo - pieces[-1][1] < 1e-6:
                pieces[-1][1] = max(pieces[-1][1], hi)
            else:
                pieces.append([clip_math.round2(lo), clip_math.round2(hi)])
        if pieces:
            out[ann.get("id")] = [[clip_math.round2(a), clip_math.round2(b)] for a, b in pieces]
    return out


def send_groups(annotations: Sequence[Mapping[str, Any]], *,
                padding_sec: float = 0.5,
                total_duration: float = 0.0,
                min_span: float = 0.5) -> list[dict[str, Any]]:
    """这些标注该分成**几次**发,每次发哪一段。

    规则:每条标注的区间各自先加 ``padding_sec`` 余量,**加完之后重叠或挨着的才并成
    一组**,每组发一次。

    为什么不是整体取并集:并集会把 [1,2] 和 [25,26] 吞成 [1,26] —— 隔了 23 秒不相干的
    画面一起发出去,费用和时长都按 26 秒算,模型还得在里面找两个框。而挨着的两条
    ([3,4] 与 [5,6],加余量后 [2.5,4.5] 与 [4.5,6.5])确实该并成一段,它们在同一处。

    余量是给模型的上下文:光看一秒画面判断不出运动方向和光照走势,前后各留半秒
    它才知道这块东西在怎么动。单帧标注 [ta,ta] 也正是靠它撑成一个可发段。

    返回按时间排好的 ``[{"start","end","numbers"}]``;``numbers`` 是这一组里
    参与的标注编号(升序),给界面摊开说「这一段是哪几条标注要的」。
    """
    padded: list[tuple[float, float, int]] = []
    for ann in annotations:
        # 有 windows 就按窗口逐段算 —— 一条标注被上面的视频层打断成两截露出来时,
        # 那是两处独立的活,该发两次。只看 interval(首尾包围)会把中间被盖住的
        # 时间也一起发出去,等于把「看见什么就发什么」这条规则又丢掉。
        spans = ann.get("windows")
        if not (isinstance(spans, (list, tuple)) and spans):
            interval = ann.get("interval")
            if not (isinstance(interval, (list, tuple)) and len(interval) == 2):
                continue
            spans = [interval]
        for span in spans:
            a0, a1 = float(span[0]), float(span[1])
            total = float(total_duration) if total_duration else a1 + padding_sec
            t0, t1 = clip_math.pad_interval((a0, a1), padding_sec, total)
            padded.append((t0, t1, int(ann.get("number") or 0)))
    if not padded:
        return []

    padded.sort()
    groups: list[dict[str, Any]] = []
    for t0, t1, number in padded:
        # 挨着也算重叠(t0 == 上一组的 end):它们是同一处,该发在一段里
        if groups and t0 <= groups[-1]["end"] + 1e-6:
            groups[-1]["end"] = max(groups[-1]["end"], t1)
            groups[-1]["numbers"].append(number)
        else:
            groups.append({"start": t0, "end": t1, "numbers": [number]})
    # 每段撑到至少 min_span:居中撑开再夹进时间线。
    # 太短的段发不出去 —— clip.duration 的 schema 下限是 0.2,而 0.17 秒的结果层
    # 会在整条链的最后一步被拒(合成、烧框、调服务全白做)。宁可发长一点。
    cap = float(total_duration) if total_duration else 0.0
    for g in groups:
        if g["end"] - g["start"] < min_span:
            mid = (g["start"] + g["end"]) / 2.0
            g["start"] = max(0.0, mid - min_span / 2.0)
            g["end"] = g["start"] + min_span
            if cap and g["end"] > cap:
                g["end"] = cap
                g["start"] = max(0.0, cap - min_span)
        g["start"] = clip_math.round2(g["start"])
        g["end"] = clip_math.round2(g["end"])
        g["numbers"] = sorted(set(g["numbers"]))
    return groups


def annotations_in_span(annotations: Sequence[Mapping[str, Any]],
                        t0: float, t1: float) -> list[dict[str, Any]]:
    """落在这一段里的标注,按编号排。区间与 [t0,t1) 有交叠就算数(单帧标注也算)。"""
    out = []
    for ann in annotations:
        interval = ann.get("interval")
        if not (isinstance(interval, (list, tuple)) and len(interval) == 2):
            continue
        a, b = float(interval[0]), float(interval[1])
        if b >= t0 and a <= t1:
            out.append(dict(ann))
    return sorted(out, key=lambda a: a.get("number") or 0)
