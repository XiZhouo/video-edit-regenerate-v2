"""片段几何 —— 一层素材怎么摆进画布。

## 为什么要有这一层

画布(project.width/height)是**输出规格**:标注 bbox 的坐标系、发给 AI 的段的规格、
导出成品的规格,三者必须是同一个数。素材尺寸通常不等于画布尺寸,所以每一层需要说清
"你怎么摆进去" —— 这就是几何。

从前这件事被硬写在 ``ffmpeg_ops.build_normalize_argv`` 里(等比缩放 + 居中黑边),
也就是把"适配方式"焊死成一种,人摆不了:16:9 素材放进 9:16 画布只能上下大黑边,
没法裁满、没法挪、没法放大。

## 合成顺序(固定,不可调换)

    源帧 → crop(先裁掉不要的) → fitMode 基础缩放 → scale 手动倍率 → x/y 平移
         → 落在画布规格的画面上

**烧标注永远发生在这之后**,面对的是画布规格的帧。所以标注 bbox 直接按画布占比落位,
无论片段怎么变换都不会画错地方 —— 这是「框画在哪儿就改哪儿」成立的前提(规划 INV-5)。

## 纯函数

本模块只出 ffmpeg 滤镜字符串与数值,不碰进程、不碰文件。滤镜由 ffmpeg_ops 执行。
"""
from __future__ import annotations

from typing import Any, Mapping

#: fitMode 闭集。contain = 等比装进去(留边);cover = 等比裁满(溢出裁掉);
#: stretch = 拉满(变形);none = 原始像素(不缩放)。
FIT_MODES = ("contain", "cover", "stretch", "none")
DEFAULT_FIT = "contain"


def normalize_geometry(clip: Mapping[str, Any]) -> dict[str, Any]:
    """把 clip 上零散的几何字段收成一个规整的字典,缺省即老行为。

    老数据没有任何几何字段 → fitMode=contain、不裁、不缩放、不平移,
    与 ``build_normalize_argv`` 的 scale+pad 完全等价,所以升级不改变已有项目的画面。
    """
    fit = str(clip.get("fitMode") or DEFAULT_FIT)
    if fit not in FIT_MODES:
        fit = DEFAULT_FIT
    return {
        "fitMode": fit,
        "x": float(clip.get("x") or 0.0),
        "y": float(clip.get("y") or 0.0),
        "scale": max(0.01, float(clip.get("scale") if clip.get("scale") is not None else 1.0)),
        "cropL": max(0.0, float(clip.get("cropL") or 0.0)),
        "cropT": max(0.0, float(clip.get("cropT") or 0.0)),
        "cropR": max(0.0, float(clip.get("cropR") or 0.0)),
        "cropB": max(0.0, float(clip.get("cropB") or 0.0)),
    }


def is_identity(clip: Mapping[str, Any]) -> bool:
    """这一层是不是「默认摆法」—— 是的话滤镜链可以走最省的那条。"""
    g = normalize_geometry(clip)
    return (g["fitMode"] == DEFAULT_FIT and g["x"] == 0.0 and g["y"] == 0.0
            and g["scale"] == 1.0
            and g["cropL"] == g["cropT"] == g["cropR"] == g["cropB"] == 0.0)


def _crop_expr(g: Mapping[str, Any]) -> str | None:
    """裁切滤镜。用 iw/ih 表达 —— 不需要事先知道素材真实尺寸。

    四个值是**每边裁掉的比例**(0..1),不是像素:同一套几何套在不同分辨率的素材上
    含义一致,项目换素材、换画布都不用重算。
    """
    l, t, r, b = g["cropL"], g["cropT"], g["cropR"], g["cropB"]
    if l == t == r == b == 0.0:
        return None
    # 夹住:两边加起来不能吃掉整幅画面,至少留 1%
    if l + r > 0.99:
        l, r = l / (l + r) * 0.99, r / (l + r) * 0.99
    if t + b > 0.99:
        t, b = t / (t + b) * 0.99, b / (t + b) * 0.99
    return (f"crop=w=iw*{1 - l - r:.6f}:h=ih*{1 - t - b:.6f}"
            f":x=iw*{l:.6f}:y=ih*{t:.6f}")


def build_scale_chain(clip: Mapping[str, Any], *, width: int, height: int) -> str:
    """裁切 + 缩放那一段(不含落位)。产出的帧可能比画布大、小或正好。"""
    w, h = int(width), int(height)
    g = normalize_geometry(clip)
    parts: list[str] = []

    crop = _crop_expr(g)
    if crop:
        parts.append(crop)

    fit, s = g["fitMode"], g["scale"]
    if fit == "stretch":
        parts.append(f"scale={w * s:.0f}:{h * s:.0f}")
    elif fit == "none":
        # 原始像素:只按手动倍率缩放,不做适配
        if s != 1.0:
            parts.append(f"scale=iw*{s:.6f}:ih*{s:.6f}")
    else:
        # contain: 等比装进去(可能留边);cover: 等比裁满(可能溢出)
        keep = "decrease" if fit == "contain" else "increase"
        parts.append(f"scale={w * s:.0f}:{h * s:.0f}:force_original_aspect_ratio={keep}")

    parts.append("setsar=1")
    return ",".join(parts)


def build_filtergraph(clip: Mapping[str, Any], *, width: int, height: int,
                      fps: float = 30.0, in_label: str = "0:v",
                      out_label: str = "v") -> str:
    """这一层的完整 filter_complex:源帧 → 落在画布上的一帧。

    产出永远是 ``width×height``、``fps``、SAR=1,所以多层拼接、烧框、导出面对的
    都是同一个坐标系(规划 INV-5)。

    **落位用 overlay,不用 crop+pad。** 后者表达不了「移出可用余量」的位移:
    画面与画布同宽时,pad 的落点被夹在 [0,0],人往右拖 48px 画面纹丝不动 ——
    而界面上的手柄已经移过去了,两边当场错位(交叉验证测试逮到的真 bug)。
    overlay 接受负坐标与溢出坐标,超出部分自然裁掉,正是「把一块画面摆在画布某处」
    这件事的原语。三种情形(正位移/负位移/画面大于画布)都在真 ffmpeg 上验过。
    """
    w, h = int(width), int(height)
    g = normalize_geometry(clip)
    dx, dy = g["x"] * w, g["y"] * h
    scale_chain = build_scale_chain(clip, width=w, height=h)
    return (
        f"color=c=black:s={w}x{h}:r={float(fps):g}[cgbg];"
        f"[{in_label}]{scale_chain}[cgfg];"
        f"[cgbg][cgfg]overlay="
        f"x='(W-w)/2+({dx:.0f})':y='(H-h)/2+({dy:.0f})':shortest=1[cgout];"
        f"[cgout]fps={float(fps):g},setsar=1[{out_label}]"
    )


def canvas_rect_of(clip: Mapping[str, Any], *, width: int, height: int,
                   src_w: float, src_h: float) -> dict[str, float]:
    """这一层的画面落在画布的哪个矩形上(像素)。

    给界面画变换手柄用 —— 手柄必须画在画面真正占的那块地方,不能画在整个画布上。
    与 :func:`build_filter_chain` 同一套算式;两者不一致的话,人拖的框和实际渲染
    会对不上,所以有测试钉着它们相等。
    """
    g = normalize_geometry(clip)
    sw = max(1.0, float(src_w) * (1 - g["cropL"] - g["cropR"]))
    sh = max(1.0, float(src_h) * (1 - g["cropT"] - g["cropB"]))
    w, h, s = float(width), float(height), g["scale"]

    if g["fitMode"] == "stretch":
        dw, dh = w, h
    elif g["fitMode"] == "none":
        dw, dh = sw * s, sh * s
    else:
        ratio = min(w * s / sw, h * s / sh) if g["fitMode"] == "contain" \
            else max(w * s / sw, h * s / sh)
        dw, dh = sw * ratio, sh * ratio

    return {
        "x": (w - dw) / 2 + g["x"] * w,
        "y": (h - dh) / 2 + g["y"] * h,
        "width": dw,
        "height": dh,
    }
