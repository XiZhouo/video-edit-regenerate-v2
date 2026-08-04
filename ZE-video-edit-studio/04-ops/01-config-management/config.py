#!/usr/bin/env python3
"""config — 按 config_schema 把 env 读成**类型化 + 已校验**的值(A 轨 reader)。

定位:**单一、带校验的 env 读取处**。打错值(如 ZE_RB_GPU_VRAM_MB=abc)按 kind 报
`ConfigError`,而不是各处 `os.environ.get` 静默回落默认。

注意(加性·不动 core):本 reader **不**强行接管 core 现有 7 处 `os.environ.get` —— 那 7 行
按已锁定决策保持不动,由 `ops_contract_check.py` 交叉校验「它们读的 env 都在 schema 声明过」。
本 reader 供新代码 / 框架旋钮只读展示(read_framework_effective)/ B 轨 seed 使用。

不 import core(ops 不依赖 core,方向相反会破坏分层):用本地 ConfigError,不复用 entity.ZeError。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from config_schema import (  # 同目录;调用方需把本目录加入 sys.path(registry/bootstrap 负责)
    KIND_BOOL, KIND_ENDPOINT, KIND_ENUM, KIND_FLOAT, KIND_INT, KIND_MODEL_REF,
    KIND_PATH, KIND_SECRET, KIND_TEXT, KnobSpec, FRAMEWORK_KNOBS, all_specs,
)


class ConfigError(ValueError):
    """配置值非法(类型不符 / 不在 choices 内)。"""


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _spec_by_key(key: str) -> KnobSpec:
    for s in all_specs():
        if s.key == key:
            return s
    raise ConfigError(f"未声明的配置键: {key}")


def coerce(spec: KnobSpec, raw: Optional[str]) -> Any:
    """字符串原值 → 按 kind 的类型化值;raw 为 None 返回 spec.default。"""
    if raw is None:
        return spec.default
    raw = raw.strip()
    if raw == "":
        return spec.default
    k = spec.kind
    try:
        if k == KIND_INT:
            return int(raw)
        if k == KIND_FLOAT:
            return float(raw)
        if k == KIND_BOOL:
            low = raw.lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            raise ConfigError(f"{spec.env_var}: 期望 bool(1/0/true/false),得到 {raw!r}")
        if k == KIND_PATH:
            return Path(raw).expanduser()
        if k in (KIND_ENUM, KIND_MODEL_REF):
            if spec.choices and raw not in spec.choices:
                raise ConfigError(f"{spec.env_var}: {raw!r} 不在允许集 {list(spec.choices)}")
            return raw
        if k == KIND_ENDPOINT:
            try:
                parsed = urlsplit(raw)
                # Accessing .port validates malformed/non-numeric ports.
                _ = parsed.port
            except ValueError as exc:
                raise ConfigError(f"{spec.env_var}: endpoint 端口非法") from exc
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                raise ConfigError(
                    f"{spec.env_var}: endpoint 必须是带 host 的 http(s) URL",
                )
            if parsed.username is not None or parsed.password is not None:
                raise ConfigError(f"{spec.env_var}: endpoint 禁止 URL userinfo")
            if parsed.fragment:
                raise ConfigError(f"{spec.env_var}: endpoint 禁止 fragment")
            return raw
        # text / secret → 原字符串
        return raw
    except (ValueError, TypeError) as e:
        if isinstance(e, ConfigError):
            raise
        raise ConfigError(f"{spec.env_var}: 期望 {k},得到 {raw!r}") from e


def read(key: str, env: Optional[Mapping[str, str]] = None) -> Any:
    """读一个声明过的配置键 → 类型化值(env 缺失则 default)。"""
    spec = _spec_by_key(key)
    src = env if env is not None else os.environ
    return coerce(spec, src.get(spec.env_var))


def read_framework_effective(env: Optional[Mapping[str, str]] = None) -> list:
    """框架旋钮的当前生效值(供设置面板**只读展示**)。

    返回 [{key,label,desc,kind,env_var,value,scope,secret}],secret 类脱敏为 '***'(若有值)。
    """
    src = env if env is not None else os.environ
    out = []
    for s in FRAMEWORK_KNOBS:
        val = coerce(s, src.get(s.env_var))
        shown: Any
        if s.secret and val not in (None, ""):
            shown = "***"
        elif isinstance(val, Path):
            shown = str(val)
        else:
            shown = val
        out.append({
            "key": s.key, "label": s.label, "desc": s.desc, "kind": s.kind,
            "env_var": s.env_var, "value": shown, "scope": s.scope, "secret": s.secret,
        })
    return out
