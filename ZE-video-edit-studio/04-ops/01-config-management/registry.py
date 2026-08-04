#!/usr/bin/env python3
"""registry — ConfigRegistry(B 轨机制)+ seed_framework_config(框架自带配置组)。

定位:配置的运行时机制层,对位 core 的 DocumentRegistry/CommandRegistry —— 框架建空 registry →
`seed_framework_config` 装入框架旋钮(只读展示)+ 通用配置组(LLM 路线 / 图片服务)→ mod 的
`config_register.py` 加业务旋钮 → `.data/config.json` 覆盖默认值。经 sdk barrel re-export,bootstrap
装配(见 sdk/bootstrap.py)。

设计要点:
  - **kind/scope/secret** 三属性驱动面板渲染与存储后端(见 SPEC §2/§3)。
  - **密钥默认源链**:override(.data) > env(env_var) > default_source(ZE-hupo-api/.env) > default。
    复用 ZE-hupo-api/.env 这一全系列权威密钥源,不另造 silo;只读,绝不写。
  - **不 import core**(ops 不依赖 core):自注入本目录,只 import 同层 config_schema/config。
"""
from __future__ import annotations

import errno
import json
import os
import re
import sys
import tempfile
import threading
from dataclasses import replace
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

# 配置键格式:小写起头 + 点分命名(键会作 JSON property + 回显前端,守住形态)
_KEY_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config_schema as cs  # noqa: E402
from config import coerce  # noqa: E402

MASK = "***"


class ConfigRegistryError(RuntimeError):
    """ConfigRegistry 的稳定、可机器判断错误基类。"""

    code = "config_registry_error"

    def __init__(self, message: str, *, context: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.context = dict(context or {})


class ConfigLoadError(ConfigRegistryError):
    """已有配置文件不可读/损坏；启动必须 fail closed，不能静默退回默认值。"""

    code = "config_load_invalid"


class ConfigWriteError(ConfigRegistryError):
    """replace 前写入失败；旧内存与旧目标文件均未改变，可安全重试。"""

    code = "config_write_failed"


class ConfigDurabilityUnknown(ConfigRegistryError):
    """replace 已成功但父目录 fsync 失败；完整新文件已可见，掉电耐久性未知。"""

    code = "config_durability_unknown"


class ConfigVersionConflict(ConfigRegistryError):
    """进程内乐观锁版本不匹配。多进程 CAS 由 W06 的事务存储提供。"""

    code = "config_version_conflict"


_UNSUPPORTED_DIR_FSYNC = frozenset(filter(None, (
    errno.EINVAL,
    errno.EBADF,
    getattr(errno, "ENOTSUP", None),
    getattr(errno, "EOPNOTSUPP", None),
)))


def _json_payload(values: Mapping[str, Any]) -> str:
    """先完整序列化再碰磁盘；Path 是唯一允许的非 JSON 原生值。"""

    def _default(value: Any) -> Any:
        if isinstance(value, Path):
            return str(value)
        raise TypeError(f"不可序列化的配置值: {type(value).__name__}")

    return json.dumps(
        dict(values), ensure_ascii=False, indent=2, allow_nan=False, default=_default,
    ) + "\n"


def _write_stream(stream: Any, payload: str) -> None:
    """故障注入边界：完整写入。"""
    stream.write(payload)


def _flush_stream(stream: Any) -> None:
    """故障注入边界：把 Python 缓冲送进内核。"""
    stream.flush()


def _fsync_stream(stream: Any) -> None:
    """故障注入边界：replace 前确保临时文件数据已耐久。"""
    os.fsync(stream.fileno())


def _fsync_parent(path: Path) -> bool:
    """同步 rename 所在目录；不支持目录 fsync 的平台返回 False。"""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(str(path.parent), flags)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_DIR_FSYNC:
            return False
        raise
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno in _UNSUPPORTED_DIR_FSYNC:
                return False
            raise
    finally:
        os.close(fd)
    return True


def _atomic_replace(path: Path, payload: str) -> bool:
    """唯一临时文件写入 + 原子 replace；返回父目录是否完成 fsync。

    replace 前的任何错误都包装成 ``ConfigWriteError``，并清掉临时文件。replace
    成功后的目录 fsync 错误不能伪装成“旧文件仍在”，故单独报告 durability unknown。
    """
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001 — 所有普通 I/O 实现错误都要稳定分类
        raise ConfigWriteError(
            f"配置目录创建失败: {path.parent}",
            context={"stage": "mkdir", "path": str(path), "replaced": False},
        ) from exc

    fd: Optional[int] = None
    tmp_path: Optional[Path] = None
    replaced = False
    stage = "temp_create"
    try:
        fd, raw_tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp",
        )
        tmp_path = Path(raw_tmp)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            # 权限收窄在部分平台不可用；mkstemp 本身已按 0600 创建。
            pass
        stream = os.fdopen(fd, "w", encoding="utf-8")
        fd = None  # stream 从这里拥有 fd
        with stream:
            stage = "write"
            _write_stream(stream, payload)
            stage = "flush"
            _flush_stream(stream)
            stage = "file_fsync"
            _fsync_stream(stream)

        stage = "replace"
        os.replace(tmp_path, path)
        replaced = True
        tmp_path = None

        stage = "directory_fsync"
        return _fsync_parent(path)
    except ConfigRegistryError:
        raise
    except Exception as exc:  # noqa: BLE001 — 所有普通 I/O 实现错误都要稳定分类
        context = {"stage": stage, "path": str(path), "replaced": replaced}
        if replaced:
            raise ConfigDurabilityUnknown(
                f"配置文件已原子替换，但父目录 fsync 失败: {path}", context=context,
            ) from exc
        raise ConfigWriteError(f"配置写入失败({stage}): {path}", context=context) from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

# 图片内网服务支持的模型(调研:全系列实测 15 个)
IMAGE_MODELS = (
    "gemini", "gemini3", "gemini31flash", "openai", "openai-15", "openai-mini",
    "flux", "flux-2-pro", "seedream", "seedream45", "seedream5",
    "qwen", "hunyuan", "fal-gpt-image-2", "dreamomni2",
)


@dataclass
class ConfigItem:
    key: str
    kind: str
    default: Any = None
    label: str = ""
    desc: str = ""
    scope: str = "tool"
    secret: bool = False
    readonly: bool = False                 # framework / 端口类:面板只读、PUT 拒改
    choices: Optional[tuple] = None
    env_var: Optional[str] = None          # 若该项由 env 驱动(框架旋钮)
    default_source: Optional[str] = None   # 如 'hupo:API_KEY' → 从 ZE-hupo-api/.env 取默认
    group: str = "其他"
    allowed_hosts: Optional[tuple[str, ...]] = None


def _read_hupo_env(skills_root: Path) -> dict:
    """最佳努力读 ZE-hupo-api/.env(全系列权威密钥源);只读、缺失返回 {}。"""
    env_path = skills_root / "ZE-hupo-api" / ".env"
    out: dict = {}
    try:
        for ln in env_path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, _, v = ln.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


class ConfigRegistry:
    """声明式配置项 + 值覆盖 + 默认源解析。skill 级单例(放 Repository)。"""

    def __init__(self, *, skills_root: Optional[Path] = None) -> None:
        self._items: dict = {}            # key -> ConfigItem(保序:py3.7+ dict 有序)
        self._values: dict = {}           # key -> override 值(来自 .data/config.json)
        self._lock = threading.RLock()    # 值 candidate→落盘→swap 的完整进程内事务锁
        self._version = 0                 # 进程内 CAS；跨进程版本由 W06 持久存储提供
        self._loaded_path: Optional[Path] = None
        self._directory_fsync_supported: Optional[bool] = None
        # ZE-hupo-api 默认密钥源:skills_root 缺省 = build_root 的父(skills/)
        root = skills_root or Path(__file__).resolve().parents[3]
        self._hupo = _read_hupo_env(root)

    @property
    def version(self) -> int:
        """当前内存值版本。只保证本进程内 CAS，不冒充多进程版本。"""
        with self._lock:
            return self._version

    @property
    def directory_fsync_supported(self) -> Optional[bool]:
        """最近一次成功 replace 后，平台是否支持父目录 fsync；未写过为 None。"""
        with self._lock:
            return self._directory_fsync_supported

    # ---- 声明(框架 seed + mod register 共用)----
    def declare(self, key: str, kind: str, *, default: Any = None, label: str = "",
                desc: str = "", scope: str = "tool", secret: bool = False,
                readonly: bool = False, choices: Optional[tuple] = None,
                env_var: Optional[str] = None, default_source: Optional[str] = None,
                group: str = "其他",
                allowed_hosts: Optional[tuple[str, ...]] = None) -> None:
        if kind not in cs.VALID_KINDS:
            raise ValueError(f"未知 kind: {kind}({key})")
        if scope not in cs.VALID_SCOPES:
            raise ValueError(f"未知 scope: {scope}({key})")
        if not _KEY_RE.match(key):
            raise ValueError(f"非法配置键(需小写点分 [a-z][a-z0-9._-]): {key!r}")
        if (kind == cs.KIND_SECRET) != bool(secret):
            raise ValueError(f"secret kind/flag 必须一致: {key}")
        if secret and scope != "secret":
            raise ValueError(f"secret 配置必须使用 scope='secret': {key}")
        if kind == cs.KIND_ENUM and not choices:
            raise ValueError(f"enum 配置必须声明 choices: {key}")
        normalized_choices = tuple(choices) if choices is not None else None
        if normalized_choices is not None and len(set(normalized_choices)) != len(normalized_choices):
            raise ValueError(f"choices 不得重复: {key}")
        normalized_hosts = None
        if allowed_hosts is not None:
            if kind != cs.KIND_ENDPOINT:
                raise ValueError(f"allowed_hosts 只能用于 endpoint: {key}")
            normalized_hosts = tuple(sorted({str(host).strip().lower() for host in allowed_hosts}))
            if not normalized_hosts or any(not host for host in normalized_hosts):
                raise ValueError(f"allowed_hosts 不得为空: {key}")
        with self._lock:
            if key in self._items:
                raise ValueError(
                    f"配置 {key!r} 已声明；只改默认值请用 override_default()",
                )
            item = ConfigItem(
                key=key, kind=kind, default=default, label=label, desc=desc, scope=scope,
                secret=secret, readonly=readonly, choices=normalized_choices, env_var=env_var,
                default_source=default_source, group=group, allowed_hosts=normalized_hosts,
            )
            if default not in (None, ""):
                item.default = self._coerce_item(item, default)
            self._items[key] = item

    def override_default(self, key: str, default: Any) -> None:
        """只替换既有声明的默认值，不允许 mod 重写安全属性。"""
        with self._lock:
            current = self._items.get(key)
            if current is None:
                raise KeyError(f"未声明配置: {key}")
            candidate = replace(current, default=default)
            if default not in (None, ""):
                candidate.default = self._coerce_item(candidate, default)
            self._items[key] = candidate

    # ---- 值 ----
    def load_values(self, path: Path) -> None:
        """载入完整 override 快照。

        文件缺失是合法的空配置；但已有文件不可读、坏 JSON、非 object、未知/只读键或值
        非法都会抛 ``ConfigLoadError``，且保留调用前内存，防止策略/密钥损坏后静默用默认值启动。
        """
        p = Path(path)
        with self._lock:
            try:
                raw = p.read_text(encoding="utf-8")
            except FileNotFoundError:
                candidate: dict[str, Any] = {}
            except UnicodeError as exc:
                raise ConfigLoadError(
                    f"配置文件不是合法 UTF-8: {p}",
                    context={"path": str(p), "stage": "decode"},
                ) from exc
            except OSError as exc:
                raise ConfigLoadError(
                    f"配置文件不可读: {p}", context={"path": str(p), "stage": "read"},
                ) from exc
            else:
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, UnicodeError) as exc:
                    raise ConfigLoadError(
                        f"配置文件不是合法 JSON: {p}",
                        context={"path": str(p), "stage": "parse"},
                    ) from exc
                if not isinstance(data, dict):
                    raise ConfigLoadError(
                        f"配置文件顶层必须是 object: {p}",
                        context={"path": str(p), "stage": "shape"},
                    )
                candidate = {}
                problems = []
                for key, value in data.items():
                    item = self._items.get(key)
                    if item is None:
                        problems.append((key, "未声明"))
                        continue
                    if item.readonly:
                        problems.append((key, "只读项不得持久化覆盖"))
                        continue
                    try:
                        candidate[key] = self._coerce_item(item, value)
                    except Exception as exc:  # noqa: BLE001 — 汇总后 fail closed
                        problems.append((key, str(exc)))
                if problems:
                    raise ConfigLoadError(
                        f"配置文件包含非法 override: {p}",
                        context={"path": str(p), "stage": "validate", "problems": problems},
                    )
                try:
                    _json_payload(candidate)
                except (TypeError, ValueError) as exc:
                    raise ConfigLoadError(
                        f"配置文件包含不可序列化值: {p}",
                        context={"path": str(p), "stage": "serialize"},
                    ) from exc

            self._values = candidate
            self._loaded_path = p
            self._version += 1

    def save_values(self, path: Path) -> None:
        """原子持久化当前内存快照。

        新 PUT 应使用 :meth:`update_values`，因为分开的 ``set_values(); save_values()``
        在两个调用之间不是事务；本方法只保留给初始化/迁移代码。
        """
        p = Path(path)
        with self._lock:
            try:
                payload = _json_payload(self._values)
            except (TypeError, ValueError) as exc:
                raise ConfigWriteError(
                    f"配置快照无法序列化: {p}",
                    context={"stage": "serialize", "path": str(p), "replaced": False},
                ) from exc
            try:
                supported = _atomic_replace(p, payload)
            except ConfigDurabilityUnknown:
                # replace 已发生；当前内存本来就是该 candidate，保持一致并诚实上抛。
                self._loaded_path = p
                self._directory_fsync_supported = None
                raise
            self._loaded_path = p
            self._directory_fsync_supported = supported

    def _coerce_item(self, item: ConfigItem, raw: Any) -> Any:
        # 显式 override 的空串对 text/endpoint 是**合法覆盖值**(空=关闭该功能,如
        # ze.image.service_url/ze.usage_collector_url 的 desc 本就写「空=关闭」)。
        # coerce 的「空串→default」语义只适用于 env 未设场景;若把它套到 override,
        # C 端档案把 service_url 置空会被顶回内网默认,auto 路线随之挑 service——
        # 不变式 2(B/C 零直连)被击穿(2026-07-21 C 端本地彩排实测)。
        if raw == "" and item.kind in (cs.KIND_TEXT, cs.KIND_ENDPOINT):
            return ""
        spec = cs.KnobSpec(
            item.key, item.env_var or item.key, item.kind, item.default,
            item.label, item.desc, scope=item.scope, secret=item.secret,
            choices=item.choices,
        )
        value = coerce(spec, None if raw is None else str(raw))
        if item.kind == cs.KIND_ENDPOINT and value not in (None, "") and item.allowed_hosts:
            host = (urlsplit(str(value)).hostname or "").lower()
            if host not in set(item.allowed_hosts):
                raise ValueError(
                    f"{item.key}: endpoint host {host!r} 不在运维允许集 {list(item.allowed_hosts)}",
                )
        # 单值也先过严格 JSON；拒绝 NaN/Infinity 与任意自定义对象。
        _json_payload({item.key: value})
        return value

    def _candidate(self, updates: Mapping[str, Any]) -> tuple[dict[str, Any], list]:
        if not isinstance(updates, Mapping):
            raise TypeError("updates 必须是 mapping")
        candidate = dict(self._values)
        rejected = []
        for key, raw in updates.items():
            item = self._items.get(key)
            if item is None:
                rejected.append((key, "未声明")); continue
            if item.readonly:
                rejected.append((key, "只读(框架/端口类)")); continue
            try:
                candidate[key] = self._coerce_item(item, raw)
            except Exception as exc:  # noqa: BLE001 — 校验失败即拒
                rejected.append((key, str(exc)))
        return candidate, rejected

    def set_values(self, updates: Mapping[str, Any]) -> list:
        """校验并应用一批 override(用于 PUT)。返回被拒的 (key, 原因) 列表;其余已应用。

        这是兼容的**纯内存**入口；需要落盘的 PUT 必须调用 ``update_values``，避免
        内存先变、磁盘后写的分叉窗口。拒绝:未声明键 / 只读项 / enum 越界 / 类型不符。
        """
        with self._lock:
            candidate, rejected = self._candidate(updates)
            if candidate != self._values:
                self._values = candidate
                self._version += 1
            return rejected

    def update_values(
        self,
        path: Path,
        updates: Mapping[str, Any],
        *,
        expected_version: Optional[int] = None,
    ) -> list:
        """校验、原子落盘、最后 swap 内存的一次完整 PUT 事务。

        返回形状与 ``set_values`` 相同：非法项进入 ``[(key, reason)]``，其余合法项在
        同一个 candidate 中提交。replace 前任一步失败时内存和目标文件都保持旧值；若
        replace 已完成而目录 fsync 报真实 I/O 错误，则完整新文件已可见，本方法同步 swap
        内存后抛 ``ConfigDurabilityUnknown``（不可安全盲重试）。
        """
        p = Path(path)
        with self._lock:
            if expected_version is not None and expected_version != self._version:
                raise ConfigVersionConflict(
                    f"配置版本冲突: expected={expected_version}, actual={self._version}",
                    context={"expected_version": expected_version, "actual_version": self._version},
                )
            candidate, rejected = self._candidate(updates)
            if candidate == self._values:
                return rejected
            try:
                payload = _json_payload(candidate)
            except (TypeError, ValueError) as exc:
                raise ConfigWriteError(
                    f"配置 candidate 无法序列化: {p}",
                    context={"stage": "serialize", "path": str(p), "replaced": False},
                ) from exc
            try:
                supported = _atomic_replace(p, payload)
            except ConfigDurabilityUnknown:
                # 文件名已经指向完整 candidate；为避免运行态与磁盘分叉，内存必须跟随。
                self._values = candidate
                self._version += 1
                self._loaded_path = p
                self._directory_fsync_supported = None
                raise
            self._values = candidate
            self._version += 1
            self._loaded_path = p
            self._directory_fsync_supported = supported
            return rejected

    # ---- 读 ----
    def effective(self, key: str) -> Any:
        """生效值:override > env > default_source(hupo) > default。"""
        with self._lock:
            item = self._items[key]
            if key in self._values:
                return self._values[key]
            if item.env_var and item.env_var in os.environ:
                spec = cs.KnobSpec(item.key, item.env_var, item.kind, item.default, item.label,
                                   item.desc, scope=item.scope, secret=item.secret, choices=item.choices)
                return coerce(spec, os.environ.get(item.env_var))
            if item.default_source and item.default_source.startswith("hupo:"):
                return self._hupo.get(item.default_source.split(":", 1)[1], item.default)
            return item.default

    def get(self, key: str, fallback: Any = None) -> Any:
        """业务/框架代码取配置值(密钥取明文,供实际调用)。"""
        with self._lock:
            if key not in self._items:
                return fallback
            return self.effective(key)

    def snapshot(self, *, reveal_secret: bool = False) -> list:
        """面板用:全部项 + 生效值(secret 默认脱敏 ***)。按 group 分组顺序保留声明序。"""
        with self._lock:
            out = []
            for item in self._items.values():
                val = self.effective(item.key)
                shown: Any
                if item.secret and not reveal_secret:
                    # 密钥**永不**回明文:有值脱敏 ***,无值给 ""(不返 None/原值)
                    shown = MASK if val not in (None, "") else ""
                elif isinstance(val, Path):
                    shown = str(val)
                else:
                    shown = val
                out.append({
                    "key": item.key, "kind": item.kind, "label": item.label, "desc": item.desc,
                    "scope": item.scope, "secret": item.secret, "readonly": item.readonly,
                    "choices": list(item.choices) if item.choices else None,
                    "group": item.group, "value": shown,
                })
            return out


# ==============================================================
# 框架自带配置组(治全系列命名碎片化)
# ==============================================================

def seed_framework_config(reg: ConfigRegistry) -> None:
    """装入框架旋钮(只读展示)+ 通用配置组(LLM 路线 / 图片服务)。

    新 v3 skill 直接继承这套标准命名,不再各起名(HUPO_API_KEY/ZE_*_API_KEY 各搞一套)。
    mod 在 config_register.py 只加本 skill 特有项 + 覆盖默认。
    """
    # —— § 框架旋钮(只读展示;值来自 env,经 config_schema)——
    for s in cs.FRAMEWORK_KNOBS:
        if s.match != "exact":
            continue  # 前缀族不单列展示
        reg.declare(s.key, s.kind, default=s.default, label=s.label, desc=s.desc,
                    scope=s.scope, readonly=True, env_var=s.env_var, group="系统(只读)",
                    secret=s.secret, choices=s.choices)

    # —— § LLM 路线组 ——
    reg.declare("ze.llm.route", cs.KIND_ENUM, default="claude-cli",
                choices=("claude-cli", "codex-cli", "gateway"),
                label="推理路线", desc="本地 claude / 本地 codex / HTTP 网关", group="LLM 路线")
    reg.declare("ze.llm.model", cs.KIND_MODEL_REF, default="",
                label="推理模型", desc="留空=按路线默认(claude→sonnet / codex→gpt / 网关→claude-opus-4-8)",
                group="LLM 路线")
    reg.declare("ze.llm.gateway_base", cs.KIND_ENDPOINT, default="https://api.ai.hupo.games/v1",
                allowed_hosts=("api.ai.hupo.games",),
                label="网关地址", desc="OpenAI 兼容 /chat/completions", group="LLM 路线")
    reg.declare("ze.llm.gateway_key", cs.KIND_SECRET, default="", secret=True, scope="secret",
                default_source="hupo:API_KEY",
                label="网关密钥", desc="默认取 ZE-hupo-api/.env;可覆盖", group="LLM 路线")
    reg.declare("ze.llm.allow_global_gateway_key", cs.KIND_BOOL, default=False,
                label="允许全局网关密钥", desc="迁移期兼容开关;关闭时登录用户必须绑定个人 Hupo NewAPI Key",
                group="LLM 路线")
    reg.declare("ze.llm.codex_account", cs.KIND_TEXT, default="",
                label="codex 账号", desc="codex 多账号时指定(空=默认账号)", group="LLM 路线")
    reg.declare("ze.llm.codex_bin", cs.KIND_TEXT, default="codex",
                label="codex 可执行", desc="codex CLI 路径", group="LLM 路线")
    reg.declare("ze.llm.claude_bin", cs.KIND_TEXT, default="claude",
                label="claude 可执行", desc="claude CLI 路径", group="LLM 路线")
    reg.declare("ze.llm.timeout_sec", cs.KIND_INT, default=120, env_var="ZE_LLM_TIMEOUT_SEC",
                label="推理超时(秒)", desc="单次 LLM 调用最长等待;超时不自动重试(避免多倍计费与白等)",
                group="LLM 路线")

    # —— § 图片服务组 ——
    reg.declare("ze.image.service_url", cs.KIND_ENDPOINT, default="http://10.0.0.252:12018",
                allowed_hosts=("10.0.0.252",),
                label="图片服务地址", desc="内网 keyless 图片服务", group="图片服务")
    reg.declare("ze.image.model", cs.KIND_ENUM, default="gemini31flash", choices=IMAGE_MODELS,
                label="图片模型", desc="内网图片服务支持的模型", group="图片服务")
    reg.declare("ze.image.timeout_sec", cs.KIND_INT, default=600, env_var="ZE_IMAGE_TIMEOUT_SEC",
                label="生成超时(秒)", desc="单次生成/改图调用最长等待;超时不自动重试(避免多倍计费与白等)",
                group="图片服务")
    # AI 通道层(3.56.0):生图路线可选;显式选定绝不静默回退(裁定 1),仅 auto 允许挑可用路线。
    reg.declare("ze.image.route", cs.KIND_ENUM, default="service",
                choices=("auto", "service", "codex-cli", "openai-api", "gemini-api"),
                label="生图路线", desc="service=内网共享;codex-cli=个人 Codex 订阅;"
                "openai-api/gemini-api=个人 API key;auto=按可用性自动挑选",
                group="图片服务")
    reg.declare("ze.image.codex_model", cs.KIND_TEXT, default="gpt-5.5",
                label="Codex 生图模型档", desc="codex-cli 生图路线的 -m 档(0.125.0 实证 gpt-5.5)",
                group="图片服务")
    reg.declare("ze.image.codex_max_concurrency", cs.KIND_INT, default=1,
                label="Codex 生图并发上限", desc="同一用户 codex 子进程并发(订阅额度保护,默认 1=串行)",
                group="图片服务")
    reg.declare("ze.image.route_fallback", cs.KIND_BOOL, default=False,
                label="路线降级", desc="仅 auto 语义下允许失败后尝试下一条可用路线;显式路线永不回退",
                group="图片服务")

    # —— § AI 同事治理组(source='ai' 命令的 provenance/safety/cost/approval)——
    # 把 05-ai-coworker 的 AiGovernor 接进运行时:这些 policy 旋钮喂给 bootstrap 构造 governor。
    # governance_enabled 默认 False = opt-in:关闭时 bootstrap 不构造 governor、dispatch 行为不变(向后兼容)。
    reg.declare("ze.ai.governance_enabled", cs.KIND_BOOL, default=False,
                label="启用 AI 治理", desc="开启后 source='ai' 命令经 provenance/safety/cost/approval 治理",
                group="AI 同事治理")
    reg.declare("ze.ai.require_provenance", cs.KIND_BOOL, default=True,
                label="强制溯源", desc="source='ai' 写入必须带非空 provenance(治理启用时生效)",
                group="AI 同事治理")
    reg.declare("ze.ai.per_call_cap_usd", cs.KIND_FLOAT, default=0.0,
                label="单次成本上限(USD)", desc="单条 AI 命令估算成本超此值即拒;0=不限",
                group="AI 同事治理")
    reg.declare("ze.ai.approval_threshold_usd", cs.KIND_FLOAT, default=0.0,
                label="人审成本阈值(USD)", desc="估算成本 ≥ 此值的 AI 命令走两段式确认;0=不按成本触发",
                group="AI 同事治理")
    # 部署受众(§4.3 红线):决定「AI 设置」里候选上游的可见性。internal=组织内部(内网免费服务路线
    # 可见);public=对外 C 端(隐藏组织内部服务,只留用户自绑的个人账号路线);local=本地开发(同 internal)。
    # server.py /api/ai/options 按此过滤 resolver 候选。config 拒未声明键 → 不在此声明则部署侧无法配 public。
    reg.declare("ze.ai.audience", cs.KIND_ENUM, default="internal",
                choices=("internal", "public", "local"),
                label="部署受众", desc="internal=组织内部(内网服务可见);public=对外 C 端(隐组织内部服务,"
                "只留用户自绑个人账号路线);local=本地开发。决定 AI 设置里候选上游可见性(§4.3)",
                group="AI 同事治理")

    # —— § 内容合规同意(对外公测门:首次 AI 生成前须接受)——
    # 机制在 core(AiConsentGate + useAiConsent);此三项是 policy,部署方填。GET /api/ai-consent 出条款,
    # 前端按 version 比对 localStorage 决定是否弹;version 变更即要求重签。全空 = 门不激活(本地开发/
    # 未配 skill 零影响)。是否已接受由前端持有,后端不存接受态。
    reg.declare("ze.ai.terms_text", cs.KIND_TEXT, default="",
                label="条款正文", desc="AI 生成前须接受的服务条款 + 内容须知正文;空=不弹同意门",
                group="内容合规")
    reg.declare("ze.ai.terms_url", cs.KIND_TEXT, default="",
                label="条款链接", desc="可选;完整条款外链(弹窗内「查看完整条款」)",
                group="内容合规")
    reg.declare("ze.ai.terms_version", cs.KIND_TEXT, default="",
                label="条款版本", desc="条款版本号;变更即要求用户重新接受(前端按版本比对)。空=门不激活",
                group="内容合规")

    # —— § 浮层 AI 协作面板(copilot)——
    # 每个 ZE web 工具常驻的悬浮 AI 面板:用户经 claude -p / codex exec agentic 操作本工具。
    reg.declare("ze.ai.copilot_enabled", cs.KIND_BOOL, default=True,
                label="启用浮层 AI", desc="常驻悬浮 AI 协作面板(agentic 操作本工具)",
                group="浮层 AI")
    reg.declare("ze.ai.copilot_allow_ambient", cs.KIND_BOOL, default=True,
                label="允许本机登录兜底", desc="用户未绑定个人账号时,用服务器本机 claude/codex 登录跑(本地开发用;服务端多租户应关闭以强制每人绑定)",
                group="浮层 AI")

    # —— § 引导精灵(guide sprite,变形体 v2)——
    # AI 行为具象化:AI 经命令总线改动实体/长任务起止时,精灵飞过去点一下/圈一下(纯视觉回执)。
    reg.declare("ze.guide.ai_receipt", cs.KIND_BOOL, default=True,
                label="AI 改动回执", desc="AI 改动实体或长任务起止时,引导精灵给出视觉回执(点/圈+跨面板高亮)。纯视觉;高危命令的确认拉起不受此开关影响",
                group="引导精灵")
    reg.declare("ze.guide.sound", cs.KIND_BOOL, default=True,
                label="精灵点击音", desc="精灵落点确认 tick 一声(40ms,音量 0.05);reduced-motion 下强制静音",
                group="引导精灵")
    reg.declare("ze.ai.copilot_engine", cs.KIND_ENUM, default="claude",
                choices=("claude", "codex"),
                label="默认引擎", desc="用户未绑定/未指定时的默认 agentic 引擎(claude -p / codex exec)",
                group="浮层 AI")
    reg.declare("ze.ai.copilot_timeout_sec", cs.KIND_INT, default=300,
                label="单轮超时(秒)", desc="一轮 agentic 对话的最长运行时间",
                group="浮层 AI")
