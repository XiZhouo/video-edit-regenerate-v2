#!/usr/bin/env python3
"""Skill.Sync — 把 build-v3 当前 core / ops / i18n / toolchain 复制替换到一个已 v3 化的 skill。

四层各自独立判类并复制(对齐 02-mod/_locks/{core,ops,i18n,toolchain}.lock.json):
  - same        : interface_version 同 + checksum 同 → 跳过
  - compat_bump : interface_version 同 + checksum 变 → 安全复制 + 自动 bump lock(加性增量)
  - incompat    : interface_version(MAJOR)变了 → **拒绝复制该层** + 出 diff,lock 不动
  - initialize  : 目标缺该层 lock → 复制 + 初始化 lock

**css 是 opt-in 同步层(不在默认 4 层、不进 LAYERS)**:`03-css-rules/` 是各 skill 的"皮肤"
(整文件夹 = 一套皮肤),默认不动、归 skill 自有。但 core 前端(01-core)长出新编辑器/类时,
**默认皮肤的工具**需要跟上对应 css,否则 `css_contract_check.py` 报缺类 —— 这类工具显式
`--layers css` 即可拉取/对齐当前皮肤(写 css.lock 供下次 classify);**定制了皮肤的工具不要同步本层**。
因 css 不进 LAYERS,`--init-locks`/`--verify-locks` 不强制它 == lock,皮肤定制不会被判漂移。

红线:复制目标只含受管层(core/ops/i18n/toolchain + opt-in css);
**永不碰** target 的 02-mod/ 与 .data/(业务 + 运行态);拒绝把 build 同步到自身。
直接复制安全契约见 DIRECT-COPY-FLYWHEEL.md:下游受管层是只读产物,本地 drift 必须先回灌
build-v3 或迁到 02-mod hook/overlay。过渡期 web.product_dir 由 ZE_SURFACE.json 声明并受保护。

契约 checksum:
  - core: 固定 scheme token + 按目标 surface 投影的完整受管复制文件 inventory
          → sha256(纯文件摘要,不 import sdk)
  - ops : 04-ops 文件内容(排 __pycache__)→ sha256
  - i18n: 05-i18n 文件内容(排 __pycache__/_contract.json)→ sha256(字典 key 词表 + t API)
  - toolchain: _TOOLCHAIN_FILES(校验门禁 + 同步引擎脚本)内容 → sha256

三方协议(防盲拷抹掉下游定制/分叉):classify() 只比 Base(lock 基线)↔ Candidate(当前源),
盲拷会覆盖下游本地改动。sync 额外算 Target(下游当前实际内容自校),把「下游也改了 + 上游也改了」
判成 **conflict**,默认不覆盖。dry-run 出机器可读逐文件计划(--receipt-out);--apply 消费该冻结
计划(--receipt)时二验 Target 未再变;覆盖冲突须显式 --force-overwrite(且仍记账)。

用法:
  python scripts/skill_sync.py --target <skill_dir>                # dry-run(只报告 + 三方计划)
  python scripts/skill_sync.py --target <skill_dir> --receipt-out plan.json   # 冻结三方计划
  python scripts/skill_sync.py --target <skill_dir> --apply        # 真复制 + bump lock(冲突默认拒绝覆盖)
  python scripts/skill_sync.py --target <skill_dir> --apply --receipt plan.json   # 消费冻结计划(二验 Target)
  python scripts/skill_sync.py --target <skill_dir> --apply --receipt plan.json --force-overwrite  # 覆盖冲突(记账)
  python scripts/skill_sync.py --target <skill_dir> --layers ops   # 只同步某层
  # core 联动闭环:同步 core 时自动带 i18n + css(css 受定制护栏:目标 tokens.css 含 [data-mod]
  # 定制块则不带并提示;--no-coupled 关闭联动)。防「上游 bump 漏发下游」(lock 只校自身不校上游落差)。
  python scripts/skill_sync.py --init-locks                        # 给 build-v3 自身 lock 写基线 checksum

同步四步闸(先理解 → 等价确认 → 反哺 → 金样硬门;详见 sync gate 段注释):
  receipt 增 understanding(tool_understanding.json 摘要)/ equivalence(apply 前后 eval_suite
  对照)/ upstream_gaps[](--log-upstream-gap "code:note" 落账)三段;--apply 落 target 根
  ZE_SYNC_RECEIPT.json 供 collect_upstream_gaps.py 聚合。scripts/_sync_gate_policy.json 金样名单
  (mode: hard|soft 默认 soft):hard 下 understanding 缺失或 equivalence diff 非空 → --apply 拒绝。

MAJOR 迁移编舞(跨主版本,如 1.x→3.x;design coresync 首航验证,2026-07-18):
  1. 隔离演练:先在目标的完整拷贝上走通全流程,真身零改动;
  2. 迁移声明:incompat 连 --force 都拒(设计如此)——在目标 lock 手动 bump interface_version
     到目标主版本并加 `_` 前缀附注(如 _comment)说明「按 MIGRATIONS 适配」,sync 即转 compat;
  3. --apply --force-overwrite 过三方冲突落层,随后按上游 MIGRATIONS 适配 02-mod,门禁驱动收敛;
  4. 部署预检(门禁测不到的面):webapp 改依赖必同步 package-lock(npm install --package-lock-only,
     否则远端 npm ci 失锁);3.x 存储校验拒组/他写,存量 .data 需 start.sh `umask 077 +
     chmod -R go-rwx`(纯元数据,BIF 先例);渲染产物 grep 豁免标记须为 0(形态契约见
     mod_renderer_check.py 头注)。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

try:
    from surface_profile import (
        deploy_info,
        has_surface,
        is_service_surface,
        load_surface_profile,
        validate_managed_surface_paths,
        validate_surface_profile,
    )
except Exception:  # pragma: no cover - compatibility fallback for older synced copies
    has_surface = None
    is_service_surface = None
    load_surface_profile = None
    deploy_info = None
    validate_surface_profile = None
    validate_managed_surface_paths = None

try:
    import layer_manifest
except Exception:  # pragma: no cover - compatibility fallback for partially synced copies
    layer_manifest = None

BUILD_ROOT = Path(__file__).resolve().parents[1]   # ZE-skill-build/

# 每层的复制目标(相对 skill root)+ 契约版本来源 + lock 文件名
LAYER_DIRS = {
    # webapp/src 是混合目录:App/lib/AmbientField 属 core,product/ 属每个 skill 的业务前端。
    # copy_layer 对它走特殊合并:更新 core 文件,保留下游 webapp/src/product。
    "core": ["01-core", "sdk", "webapp/src"],
    "ops": ["04-ops"],
    "i18n": ["05-i18n"],
    "toolchain": [],   # 纯具名文件层(见 LAYER_FILES)
    # css(皮肤)= **opt-in 同步层**:不进 LAYERS(下方),故 --init-locks / --verify-locks 不管它,
    # 不会把皮肤定制误判成漂移。默认皮肤的工具用 `--layers css` 拉取/对齐当前皮肤 —— core 前端
    # 长出新编辑器/类时皮肤必须跟上(否则 css-contract 缺类);**定制了皮肤的工具不要同步本层**。
    "css": ["03-css-rules"],
}
# 工具链层:校验门禁 + 同步引擎(随 sync 下发到每个 skill;不含 build-v3 专用的 skill_scaffold)。
_TOOLCHAIN_FILES = [
    "scripts/verify_all.py",
    "scripts/topology_doctor.py",
    "scripts/deployment_profile.py",
    "scripts/artifact_variant.py",
    "scripts/release_builder.py",
    "scripts/TOOLCHAIN_PY_DEPS.json",
    "contracts/artifact/README.md",
    "contracts/artifact/ze.artifact-build.v1.schema.json",
    "contracts/artifact/ze.artifact-variant-manifest.v1.schema.json",
    "contracts/artifact/ze.series-overlay.v1.schema.json",
    "contracts/artifact/ze.source-inventory.v1.schema.json",
    "contracts/artifact/ze.runtime-package.v1.schema.json",
    "scripts/css_contract_check.py",
    "scripts/mod_renderer_check.py",
    "scripts/ops_contract_check.py",
    "scripts/i18n_contract_check.py",
    "scripts/i18n_english_check.py",
    "scripts/i18n_audit.py",
    "scripts/i18n_sync.py",
    "scripts/i18n_keys_lib.py",
    "scripts/i18n_dump_keys.mjs",
    "scripts/skill_sync.py",
    "scripts/surface_profile.py",
    "scripts/local_launcher.py",
    "scripts/dependency_manifest.py",
    "scripts/environment_broker.py",
    "start.command",
    "start.bat",
    "start.ps1",
    "scripts/layer_manifest.py",
    "scripts/managed_drift_check.py",
    # 防文档状态漂移门禁 + 规则(全 skill 继承;状态纪律见 STATUS_DISCIPLINE.md)
    "scripts/doc_status_check.py",
    "scripts/usability_check.py",   # 可用性烟雾(未声明 usability_cases 软跳过;声明后强制覆盖)
    "scripts/eval_suite.py",        # 声明式离线 command eval/benchmark(未声明 eval_cases 软跳过)
    # 类型库门禁(GATE-TYPE-07 词表 + GATE-TYPE-09 mod 防漂移):09 半边在下游真把关(mod 是下游
    # 自己的、能修),与层级门禁「上游-only」裁定不冲突(那批校验只读皮肤,下游无从修)。
    "scripts/type_registry_check.py",
    "scripts/ai_interface_check.py",
    "scripts/command_async_check.py",   # 慢命令必须 stage(防 native 包 LLM/出图);随 core 进各 skill
    "scripts/ai_effect_write_check.py", # ai-callable producer 禁 artifact 协议外文件写/子进程(安全);随 core 进各 skill
    # ProviderHub C 端红线 + §5.3 Build 硬门:全扫**下游自有内容**(02-mod 业务代码 / webapp/src/product /
    # ZE_AI_REQUIREMENTS.json)——须随 toolchain 进各 skill 逐 skill 把关(同 css_contract 是下游消费者门,
    # 非「上游创作门」),否则 foundry 自己的 mod 改动无门可拦。
    "scripts/public_ai_surface_check.py",     # C 端前端显示层禁内部品牌/内网地址(§4.3)
    "scripts/mod_backend_brand_check.py",     # mod 后端 .py 禁硬编码内部 endpoint/品牌(§5.3)
    "scripts/mod_fixed_model_enum_check.py",  # mod 禁硬编码固定 model 目录(§5.3/§6.3 G2)
    "scripts/mod_raw_secret_check.py",        # mod 实体 schema 禁 raw secret 字段(§5.3)
    "scripts/ai_requirements_check.py",       # AI 工具须声明 ZE_AI_REQUIREMENTS.json capability manifest(§5.2/§5.3)
    # 注:皮肤层级门禁(css_layer_guard / css_container / responsive / editor_frame)+ 元门禁
    # rule_registry_check **刻意不进**本清单 —— 它们校验「皮肤/核心如何被创作」,而创作只在 build-v3 上游
    # 发生;迁移出的 skill 是皮肤**只读消费者**(其 03-css-rules / 01-core 是 sync 来的产物、常落后)。
    # 在下游对只读且落后的皮肤跑这些层级门禁,只会报「上游尚未同步到此处的旧债」、下游无从修(pilot
    # 实测:同步到 image-cleanup-v3 后 css-layer-guard 938 / responsive 3 / editor-frame 28 全是旧债)。
    # 下游该有的那道门 = css_contract_check(已在本清单、必跑):校验它 JSX 用的 .ze-* / 颜色合法。
    # 层级把关留在源头(DESIGN-SYSTEM §0.2 授权与传播模型)。
    # stdlib_shadow_check(注入目录模块名禁与 stdlib 撞名)同此裁定:叶子/注入机制的创作只在上游,
    # 下游 01-core 是 sync 产物 —— 撞名只可能在源头产生,在源头拦(先例:queue.py 遮蔽 stdlib queue)。
    "scripts/STATUS_DISCIPLINE.md",
    "scripts/test_skill_sync.py",
    "scripts/test_deployment_profile.py",
    "scripts/test_artifact_variant.py",
    "scripts/test_release_builder.py",
    "scripts/test_surface_profile.py",
    "scripts/test_local_launcher.py",
    "scripts/test_dependency_manifest.py",
    "scripts/test_environment_broker.py",
    "scripts/test_verify_all.py",
    "scripts/test_layer_manifest.py",
    "scripts/test_managed_drift_check.py",
    "scripts/test_css_contract_check.py",
    "scripts/test_mod_renderer_check.py",
    "scripts/test_ops_contract_check.py",
    "scripts/test_i18n_contract_check.py",
    "scripts/test_i18n_english_check.py",
    "scripts/test_i18n_audit.py",
    "scripts/test_i18n_sync.py",
    "scripts/test_doc_status_check.py",
    "scripts/test_eval_suite.py",
    "scripts/test_type_registry_check.py",
    "scripts/test_ai_interface_check.py",
    "scripts/test_command_async_check.py",
    "scripts/test_ai_effect_write_check.py",
    "scripts/_toolchain_contract.json",   # 版本来源(随层复制;但不进 checksum)
]
LAYER_FILES = {
    "core": ["server.py", "scripts/ze_command.py"],
    "ops": [],
    "i18n": [],
    "toolchain": _TOOLCHAIN_FILES,
    "css": [],   # opt-in 皮肤层:只整目录复制 03-css-rules(LAYER_DIRS),无具名文件
}

# 全部受管层(init/verify locks 遍历用)。
LAYERS = ("core", "ops", "i18n", "toolchain")
LOCK_DIR = "02-mod/_locks"
_BASE_COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__", ".mypy_cache", "*.pyc", "*.pyo", ".venv", "node_modules", "dist",
    ".data", "_legacy-*", "*.tmp", ".DS_Store",
    # 前端单测(*.test.ts/tsx)是 build-v3 框架自测,不下发下游(下游是只读消费者,不该跑框架测)。
    # 否则 core sync 把它们拷进下游,下游 webapp/tsconfig.json 若没排除就 tsc 拉进来 → import 'vitest'
    # 解析不到 → typecheck 红(drift)。受管 .py 门禁测(test_*.py)经 LAYER_FILES 具名拷贝,不受此影响。
    "*.test.ts", "*.test.tsx",
)


def _copy_ignore_names(src_dir: str, names: list[str], source_root: Path) -> set[str]:
    """One ignore decision shared by copytree and the core checksum inventory."""
    ignored = set(_BASE_COPY_IGNORE(src_dir, names))
    src = Path(src_dir).resolve()
    if src == (source_root / "webapp" / "src").resolve():
        ignored.add("gallery")
    return ignored


def _copy_ignore_for_root(source_root: Path):
    source_root = source_root.resolve()

    def ignore(src_dir: str, names: list[str]) -> set[str]:
        return _copy_ignore_names(src_dir, names, source_root)

    return ignore


def COPY_IGNORE(src_dir: str, names: list[str]) -> set[str]:
    """copytree ignore:排除通用构建垃圾 + build-v3 开发展柜 webapp/src/gallery。

    注意:`gallery` 也是一个真实 core editor class(`editors/gallery/GalleryEditor.tsx`)。
    不能用 basename 级 ignore_patterns("gallery"),否则 scaffold 产物会缺静态 import 的编辑器。
    """
    return _copy_ignore_names(src_dir, names, BUILD_ROOT)


WEBAPP_SRC_ALWAYS_SOURCE_SKIP = {"gallery"}


class SurfaceProfileError(ValueError):
    """An explicit Surface Profile exists but cannot safely drive projection."""

    def __init__(self, root: Path, errors: list[str], *, explicit: bool = True):
        self.root = root
        self.path = root / "ZE_SURFACE.json"
        self.errors = list(errors) or ["unknown Surface Profile error"]
        self.explicit = explicit
        super().__init__(f"{self.path} invalid: {'; '.join(self.errors)}")

    def as_dict(self) -> dict:
        return {
            "status": "invalid",
            "path": str(self.path),
            "explicit": self.explicit,
            "errors": self.errors,
        }


class LayerManifestError(ValueError):
    """A declared ownership boundary is invalid and cannot drive projection."""

    def __init__(self, root: Path, errors: list[str]):
        self.root = Path(root)
        self.path = self.root / "ZE_LAYER_MANIFEST.json"
        self.errors = list(errors) or ["unknown Layer Manifest error"]
        super().__init__(f"{self.path} invalid: {'; '.join(self.errors)}")


class ManagedTargetError(RuntimeError):
    """An existing managed destination shape could escape its declared layer."""

    def __init__(self, root: Path, errors: list[str]):
        self.root = Path(root)
        self.errors = list(errors) or ["unknown managed target error"]
        super().__init__(f"{self.root} unsafe managed target: {'; '.join(self.errors)}")


def surface_profile_state(root: Path) -> dict:
    """Load one profile without letting an explicit invalid file become inference.

    ``surface_profile.load_surface_profile`` intentionally infers a legacy
    profile when the file is absent.  It also historically inferred when JSON
    was malformed, which is unsafe for sync: an explicit bad declaration could
    silently project as headless and produce a matching lock.  This boundary
    therefore reads an explicit file strictly, while retaining inference only
    for the genuinely absent-file compatibility path.
    """
    root = Path(root)
    path = root / "ZE_SURFACE.json"
    explicit = path.exists() or path.is_symlink()
    profile = None
    errors: list[str] = []

    if explicit:
        if (load_surface_profile is None or validate_surface_profile is None
                or validate_managed_surface_paths is None):
            errors.append("surface_profile validator unavailable")
        else:
            try:
                profile = load_surface_profile(root)
                if not isinstance(profile, dict):
                    errors.append("profile must be a JSON object")
                    profile = None
                else:
                    errors.extend(validate_surface_profile(root, profile))
                    errors.extend(validate_managed_surface_paths(profile))
            except Exception as exc:  # noqa: BLE001 - turn validator failure into a hard guard
                errors.append(f"validator failed: {type(exc).__name__}")
    elif load_surface_profile is not None:
        # Compatibility contract: only a missing file may use legacy inference.
        try:
            profile = load_surface_profile(root)
            if validate_surface_profile is not None:
                errors.extend(validate_surface_profile(root, profile))
            if validate_managed_surface_paths is not None:
                errors.extend(validate_managed_surface_paths(profile))
        except Exception as exc:  # noqa: BLE001 - legacy inference remains best effort
            errors.append(f"inference failed: {type(exc).__name__}")

    return {
        "path": str(path),
        "explicit": explicit,
        "profile": profile,
        "errors": errors,
    }


def _validated_target_surface_profile(root: Path) -> dict | None:
    """Return a valid explicit/inferred profile; never project known errors."""
    state = surface_profile_state(root)
    if state["errors"]:
        raise SurfaceProfileError(
            Path(root), state["errors"], explicit=bool(state["explicit"])
        )
    return state["profile"]


def _webapp_src_preserve_names(dst_root: Path) -> set[str]:
    """Names under webapp/src that belong to the downstream product surface."""
    preserve: set[str] = set()
    if layer_manifest is not None:
        manifest = layer_manifest.load_layer_manifest(dst_root)
        manifest_errors = layer_manifest.validate_layer_manifest(dst_root, manifest)
        if manifest_errors:
            raise LayerManifestError(dst_root, manifest_errors)
        for product_dir in layer_manifest.product_dirs(dst_root, manifest):
            parts = Path(product_dir).parts
            if len(parts) >= 3 and parts[0] == "webapp" and parts[1] == "src":
                preserve.add(parts[2])
    profile = _validated_target_surface_profile(dst_root)
    if profile is None:
        return preserve
    web = profile.get("web") if isinstance(profile, dict) else None
    product_dir = (web or {}).get("product_dir") if isinstance(web, dict) else None
    if not isinstance(product_dir, str) or not product_dir:
        return preserve
    parts = Path(product_dir).parts
    if len(parts) >= 3 and parts[0] == "webapp" and parts[1] == "src":
        preserve.add(parts[2])
    return preserve


def _webapp_src_source_skip_names(
        source_root: Path, *, target_root: Path | None = None) -> set[str]:
    """Top-level webapp/src names excluded from this projected core copy.

    The source and destination can declare different product directories.  Both
    are excluded while projecting an upstream source, but a target auditing
    itself excludes only its own declared product directory.  This keeps a
    stale, undeclared default ``product`` directory visible to the checksum.
    """
    names = set(WEBAPP_SRC_ALWAYS_SOURCE_SKIP)
    names.update(_webapp_src_preserve_names(source_root))
    if target_root is not None:
        names.update(_webapp_src_preserve_names(target_root))
    return names


def _copy_webapp_src(src: Path, dst: Path, dst_root: Path) -> None:
    """Copy core webapp/src while preserving target-owned product code.

    `webapp/src/product` is the surface profile's product_dir, so it belongs to
    the downstream skill, not the build-v3 core. Older sync rmtree'd webapp/src
    wholesale, which erased product modules such as PSD readers/writers.
    """
    _guard_inside(dst, dst_root)
    copy_ignore = _copy_ignore_for_root(src.parents[1])
    preserve_names = _webapp_src_preserve_names(dst_root)
    source_skip = _webapp_src_source_skip_names(
        src.parents[1], target_root=dst_root
    )
    source_skip.update(copy_ignore(str(src), [child.name for child in src.iterdir()]))
    if dst.exists():
        for child in list(dst.iterdir()):
            if child.name in preserve_names:
                continue
            if child.is_symlink():
                child.unlink()
            elif child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    dst.mkdir(parents=True, exist_ok=True)
    for child in sorted(src.iterdir(), key=lambda path: path.name):
        if child.name in source_skip:
            continue
        target = dst / child.name
        if child.is_dir():
            shutil.copytree(child, target, ignore=copy_ignore)
        else:
            shutil.copy2(child, target)


# ==============================================================
# 契约 version + checksum(对 SOURCE = build_root 计算)
# ==============================================================

def _core_version(root: Path) -> str:
    txt = (root / "sdk" / "_contract.py").read_text()
    m = re.search(r'INTERFACE_VERSION\s*=\s*["\']([^"\']+)', txt)
    return m.group(1) if m else "0.0.0"


def _copytree_regular_files(src: Path, copy_ignore, *, top_level_skip=()) -> list[Path]:
    """Enumerate exactly the regular files a copytree-style managed copy sees."""
    if not src.is_dir():
        return []
    top_level_skip = set(top_level_skip)
    files: list[Path] = []

    def visit(directory: Path, *, is_root: bool = False) -> None:
        children = sorted(directory.iterdir(), key=lambda path: path.name)
        ignored = set(copy_ignore(str(directory), [child.name for child in children]))
        if is_root:
            ignored.update(top_level_skip)
        for child in children:
            if child.name in ignored:
                continue
            if child.is_dir():
                visit(child)
            elif child.is_file():
                files.append(child)

    visit(src, is_root=True)
    return files


def _core_managed_files(root: Path, *, target_root: Path | None = None) -> dict[str, Path]:
    """Enumerate the complete regular-file inventory copied by the managed core layer.

    Returns ``{root-relative POSIX path: source Path}``.  The same ignore
    callback drives ``shutil.copytree`` and this traversal.  Product-owned
    ``webapp/src/product`` (including a target's custom surface ``product_dir``)
    and the upstream-only ``gallery`` are shared top-level exclusions.  Optional
    web/server files are projected from ``target_root``.
    """
    target_root = root if target_root is None else target_root
    # A direct inventory read is also an authority read: validate the complete
    # destination shape before following or hashing any managed path.
    audit_managed_target(target_root, ["core"])
    copy_ignore = _copy_ignore_for_root(root)
    by_relative_path: dict[str, Path] = {}
    for rel in ("01-core", "sdk"):
        for file_path in _copytree_regular_files(root / rel, copy_ignore):
            by_relative_path[file_path.relative_to(root).as_posix()] = file_path

    if _target_has_surface(target_root, "web"):
        webapp_src = root / "webapp" / "src"
        for file_path in _copytree_regular_files(
                webapp_src,
                copy_ignore,
                top_level_skip=_webapp_src_source_skip_names(
                    root, target_root=target_root)):
            by_relative_path[file_path.relative_to(root).as_posix()] = file_path

    for rel in LAYER_FILES["core"]:
        if rel == "server.py" and not _target_is_service(target_root):
            continue
        file_path = root / rel
        if file_path.is_file():
            by_relative_path[Path(rel).as_posix()] = file_path

    return by_relative_path


def _core_managed_file_fingerprint(root: Path, *, target_root: Path | None = None) -> list[str]:
    """Hash the complete regular-file inventory copied by the managed core layer.

    Relative paths use POSIX form so the digest is host-neutral.
    """
    by_relative_path = _core_managed_files(root, target_root=target_root)
    return [
        f"CORE:{rel}:{hashlib.sha256(by_relative_path[rel].read_bytes()).hexdigest()}"
        for rel in sorted(by_relative_path)
    ]


def _core_checksum(root: Path, *, target_root: Path | None = None) -> str:
    """Stable pure-file digest for the complete managed core copy inventory.

    Public API compatibility is owned by ``PUBLIC_SURFACE.lock.json`` and
    ``surface_diff_check``. Sync only needs to know whether copied core files
    changed, so it hashes the same inventory as copy and never imports ``sdk``.
    ``target_root`` projects optional web/server files exactly as copy_layer
    does for web, headless CLI, and API consumers.
    """
    lines = ["SCHEME:ze.core-content.v1"]
    lines.extend(_core_managed_file_fingerprint(root, target_root=target_root))
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _managed_tree_checksum(
        root: Path,
        relative_dir: str,
        *,
        excluded_relpaths: frozenset[str] = frozenset(),
) -> str:
    """Hash exactly the regular-file inventory copied from one managed tree."""
    tree_root = root / relative_dir
    copy_ignore = _copy_ignore_for_root(root)
    h = hashlib.sha256()
    for file_path in _copytree_regular_files(tree_root, copy_ignore):
        rel = file_path.relative_to(tree_root).as_posix()
        if rel in excluded_relpaths:
            continue
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(file_path.read_bytes())
    return h.hexdigest()


def _ops_version(root: Path) -> str:
    d = json.loads((root / "04-ops" / "_contract.json").read_text())
    return d.get("interface_version", "0.0.0")


def _ops_checksum(root: Path) -> str:
    # 排 _contract.json(它只承载 version,跟 css/core 一致:checksum 独立于版本字段)
    return _managed_tree_checksum(
        root, "04-ops", excluded_relpaths=frozenset({"_contract.json"}),
    )


def _i18n_version(root: Path) -> str:
    d = json.loads((root / "05-i18n" / "_contract.json").read_text())
    return d.get("interface_version", "0.0.0")


def _i18n_checksum(root: Path) -> str:
    # 同 ops:排 _contract.json(只承载 version);05-i18n 全文件内容 sha256。
    # 译文改 → checksum 变 → compat_bump(重拷,非破坏);删/改 key 由作者 bump MAJOR → incompat。
    return _managed_tree_checksum(
        root, "05-i18n", excluded_relpaths=frozenset({"_contract.json"}),
    )


def _css_version(root: Path) -> str:
    p = root / "03-css-rules" / "_contract.json"
    if not p.is_file():
        return "0.0.0"
    return json.loads(p.read_text()).get("interface_version", "0.0.0")


def _css_checksum(root: Path) -> str:
    # 同 ops/i18n:排 _contract.json(只承载 version);03-css-rules 全文件内容 sha256。
    # 皮肤 opt-in 同步用;改皮肤 → checksum 变 → compat_bump(重拷);删/改 token 词表由作者 bump MAJOR。
    return _managed_tree_checksum(
        root, "03-css-rules", excluded_relpaths=frozenset({"_contract.json"}),
    )


def _toolchain_version(root: Path) -> str:
    p = root / "scripts" / "_toolchain_contract.json"
    if not p.is_file():
        return "0.0.0"
    return json.loads(p.read_text()).get("interface_version", "0.0.0")


def _toolchain_checksum(root: Path) -> str:
    """受管脚本(_TOOLCHAIN_FILES)内容 sha256;改任一脚本 → checksum 变 → compat_bump。"""
    h = hashlib.sha256()
    for rel in _TOOLCHAIN_FILES:
        if rel.endswith("_toolchain_contract.json"):
            continue  # 版本字段载体,独立于 checksum(同 ops/i18n 排 _contract.json)
        f = root / rel
        if not f.is_file():
            continue
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(f.read_bytes())
    return h.hexdigest()


_CONTRACT = {
    "core": (_core_version, _core_checksum),
    "ops": (_ops_version, _ops_checksum),
    "i18n": (_i18n_version, _i18n_checksum),
    "toolchain": (_toolchain_version, _toolchain_checksum),
    "css": (_css_version, _css_checksum),   # opt-in(不入 LAYERS;仅 --layers css 时用)
}


# ==============================================================
# 三方协议:逐文件受管清单(与各层 checksum 共用同一 ignore/inventory,
# 用于 dry-run receipt 的 per-file 计划;不改各层 checksum 语义)
# ==============================================================

def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _managed_tree_files(
        root: Path,
        relative_dir: str,
        *,
        excluded_relpaths: frozenset[str] = frozenset(),
) -> dict[str, Path]:
    """Root-relative POSIX inventory for one managed tree, mirroring its checksum."""
    tree_root = root / relative_dir
    copy_ignore = _copy_ignore_for_root(root)
    out: dict[str, Path] = {}
    for file_path in _copytree_regular_files(tree_root, copy_ignore):
        rel = file_path.relative_to(tree_root).as_posix()
        if rel in excluded_relpaths:
            continue
        out[(PurePosixPath(relative_dir) / rel).as_posix()] = file_path
    return out


def _toolchain_managed_files(root: Path) -> dict[str, Path]:
    """Named toolchain files present under root (drops the version-carrier json)."""
    out: dict[str, Path] = {}
    for rel in _TOOLCHAIN_FILES:
        if rel.endswith("_toolchain_contract.json"):
            continue
        f = root / rel
        if f.is_file():
            out[PurePosixPath(rel).as_posix()] = f
    return out


_TREE_LAYER_DIRS = {"ops": "04-ops", "i18n": "05-i18n", "css": "03-css-rules"}


def _layer_managed_files(
        layer: str, root: Path, *, target_root: Path | None = None) -> dict[str, Path]:
    """Per-file inventory for one layer, mirroring exactly what its checksum hashes."""
    if layer == "core":
        return _core_managed_files(root, target_root=target_root)
    if layer in _TREE_LAYER_DIRS:
        return _managed_tree_files(
            root, _TREE_LAYER_DIRS[layer],
            excluded_relpaths=frozenset({"_contract.json"}),
        )
    if layer == "toolchain":
        return _toolchain_managed_files(root)
    raise ValueError(f"unknown managed layer: {layer}")


def source_contract(
        layer: str,
        root: Path = BUILD_ROOT,
        *,
        target_root: Path | None = None,
) -> tuple[str, str]:
    """Read a source layer contract, projecting optional core files to target."""
    if target_root is not None:
        audit_managed_target(Path(target_root), [layer])
    ver_fn, sum_fn = _CONTRACT[layer]
    checksum = sum_fn(root, target_root=target_root) if layer == "core" else sum_fn(root)
    return ver_fn(root), checksum


# ==============================================================
# 分类 + 复制
# ==============================================================

def _major(v: str) -> str:
    return v.split(".")[0]


def classify(src_ver: str, src_sum: str, lock: dict | None) -> str:
    if lock is None:
        return "initialize"
    old_ver = lock.get("interface_version", "0.0.0")
    old_sum = lock.get("checksum", "")
    if old_ver == src_ver and old_sum == src_sum:
        return "same"
    if _major(old_ver) != _major(src_ver):
        return "incompat"
    return "compat_bump"


# ==============================================================
# 三方协议(Base / Target / Candidate)
#   Base      = 下游 lock 记的上次同步基线(_read_lock 返回的 checksum)
#   Target    = 下游当前实际受管内容(source_contract(layer, dst) 自校)
#   Candidate = build-v3 当前源投影到该 dst(source_contract(layer, src, target_root=dst))
# classify() 只看 Base↔Candidate(上游是否变),盲拷会抹掉下游定制;三方再引入 Target,
# 把「下游也改了 + 上游也改了」判成 conflict,默认不覆盖。
# ==============================================================

SYNC_RECEIPT_SCHEMA = "ze.sync_receipt.v1"


def classify_three_way(
        base: dict | None,
        cand_ver: str,
        cand_sum: str,
        tgt_ver: str,
        tgt_sum: str,
        *,
        tgt_present: bool,
) -> str:
    """Base/Target/Candidate verdict. ``base`` is a validated lock dict or None.

    - initialize : no recorded base (first sync of this layer)
    - skip       : upstream unchanged since base (nothing to push; may include
                   downstream-only drift the sync will not push)
    - incompat   : upstream MAJOR bump
    - update     : upstream changed, downstream still at base (safe to update)
    - already    : upstream changed, downstream already equals candidate
    - conflict   : upstream changed AND downstream diverged to a third state
    """
    if base is None:
        return "initialize"
    base_ver = base.get("interface_version")
    base_sum = base.get("checksum")
    if cand_ver == base_ver and cand_sum == base_sum:
        return "skip"
    if _major(base_ver or "0.0.0") != _major(cand_ver):
        return "incompat"
    if tgt_sum == base_sum:
        return "update"
    if tgt_sum == cand_sum:
        return "already"
    if not tgt_present:
        # Downstream holds no managed content for this layer (legacy/empty):
        # populating it is an initialize-like update, never a fork conflict.
        return "update"
    return "conflict"


def _per_file_entries(
        layer: str,
        cand_files: dict[str, Path],
        tgt_files: dict[str, Path],
        verdict: str,
) -> list[dict]:
    """Machine-readable per-file plan. Per-file base is not tracked in the lock
    (it only records a layer checksum), so ``base`` is null and the layer-level
    three-way verdict decides whether a differing file is ``update`` or ``conflict``.
    """
    entries: list[dict] = []
    for path in sorted(set(cand_files) | set(tgt_files)):
        c = cand_files.get(path)
        t = tgt_files.get(path)
        c_sum = _file_sha256(c) if c is not None else None
        t_sum = _file_sha256(t) if t is not None else None
        if c is not None and t is None:
            action, owner, decision = "add", "upstream", "upstream adds file"
        elif t is not None and c is None:
            action, owner, decision = (
                "delete", "downstream", "file absent upstream; downstream still has it")
        elif c_sum == t_sum:
            action, owner, decision = "skip", "both", "identical bytes"
        elif verdict == "conflict":
            action, owner, decision = (
                "conflict", "both", "downstream and upstream both diverged from base")
        elif verdict == "skip":
            action, owner, decision = (
                "skip", "downstream", "downstream-only change; upstream unchanged")
        else:
            action, owner, decision = (
                "update", "upstream", "upstream changed; downstream not diverged")
        entries.append({
            "path": path, "layer": layer, "action": action, "owner": owner,
            "base": None, "target": t_sum, "candidate": c_sum, "decision": decision,
        })
    return entries


def three_way_plan(
        layer: str,
        source_root: Path,
        dst_root: Path,
        lock: dict | None,
        *,
        candidate: tuple[str, str] | None = None,
) -> dict:
    """Base/Target/Candidate fingerprints + per-file plan for one managed layer.

    ``lock`` is the validated downstream base lock (or None). ``candidate`` may
    pass a precomputed (version, checksum) so callers avoid a second source read.
    """
    if candidate is None:
        cand_ver, cand_sum = source_contract(layer, source_root, target_root=dst_root)
    else:
        cand_ver, cand_sum = candidate
    tgt_ver, tgt_sum = source_contract(layer, dst_root)
    cand_files = _layer_managed_files(layer, source_root, target_root=dst_root)
    tgt_files = _layer_managed_files(layer, dst_root, target_root=dst_root)
    verdict = classify_three_way(
        lock, cand_ver, cand_sum, tgt_ver, tgt_sum, tgt_present=bool(tgt_files),
    )
    return {
        "layer": layer,
        "verdict": verdict,
        "base": (
            None if lock is None
            else {"version": lock.get("interface_version"), "checksum": lock.get("checksum")}
        ),
        "candidate": {"version": cand_ver, "checksum": cand_sum},
        "target": {"version": tgt_ver, "checksum": tgt_sum},
        "files": _per_file_entries(layer, cand_files, tgt_files, verdict),
    }


def _receipt_layer_stale(receipt: dict, layer: str, src_sum: str, tgt_sum: str) -> str | None:
    """Return a reason string if a frozen receipt no longer matches reality."""
    layers = receipt.get("layers") if isinstance(receipt, dict) else None
    plan = layers.get(layer) if isinstance(layers, dict) else None
    if not isinstance(plan, dict):
        return f"冻结计划缺 {layer} 层,重新 --receipt-out 冻结后再 apply"
    recorded_target = (plan.get("target") or {}).get("checksum")
    if recorded_target != tgt_sum:
        return (f"下游 {layer} 在冻结计划之后又被改动(Target 指纹不符),"
                f"重新 --receipt-out 复核后再 apply")
    recorded_candidate = (plan.get("candidate") or {}).get("checksum")
    if recorded_candidate != src_sum:
        return (f"上游 {layer} 源在冻结计划之后又变了(Candidate 指纹不符),"
                f"重新 --receipt-out 冻结后再 apply")
    return None


# ==============================================================
# 同步四步闸(先理解 → 等价确认 → 反哺 → 金样硬门)
#   门禁绿 ≠ 功能在:三方协议只拦「字节级双改冲突」,checksum 不测行为语义 —— MINOR 夹带的
#   行为变化可以在全部现有门禁绿的情况下顶掉下游功能。本节把用户的四步工作方法机械化为
#   receipt 证据物 + apply 钩子;纯增量,不削弱任何既有护栏(三方冲突默认拒 / receipt 二验 /
#   02-mod + product + .data 永不覆盖)。
#   第 1 步(先理解工具):target 根 tool_understanding.json(design 侧 ze.tool-understanding.v1
#     sibling 产物,schema 见 ZE-skill-design/contracts/tool-understanding/)。缺失 → WARN 并记
#     receipt.understanding={present:false};存在 → 摘要(sha256 + key_flows 数)入 receipt。
#     文件归下游自有(_BASE_PROTECTED_TARGET_PATHS),sync 永不覆盖。
#   第 2 步(确认不顶功能):apply 前后各跑一次 target 自己的 scripts/eval_suite.py
#     (registry.eval_cases 已声明才有信号;未声明记 {cases:0, skipped:true}),前后结果 diff 写
#     receipt.equivalence —— 这是行为语义变化的机械兜底。
#   第 3 步(反哺通道):receipt.upstream_gaps[](schema {code, note, file})落「工具错 vs
#     build-v3 缺」裁定;CLI --log-upstream-gap "code:note" 记账;apply 落 ZE_SYNC_RECEIPT.json,
#     scripts/collect_upstream_gaps.py 聚合全舰队出反哺清单(并入 BUILD_V3_INTEGRATION_BACKLOG)。
#   第 4 步(金样硬门):scripts/_sync_gate_policy.json 金样名单(mode: hard|soft,默认 soft)。
#     hard 模式 --apply:understanding 缺失 → 复制前拒绝(零变更);equivalence diff 非空 →
#     exit≠0 + receipt 记 blocked(此时受管层已复制,需 git 还原后排查)。
# ==============================================================

TOOL_UNDERSTANDING_FILE = "tool_understanding.json"
SYNC_GATE_POLICY_FILE = "scripts/_sync_gate_policy.json"
SYNC_RECEIPT_FILE = "ZE_SYNC_RECEIPT.json"
_GATE_MODES = ("soft", "hard")


def _tool_understanding_summary(dst_root: Path) -> dict:
    """四步闸第 1 步证据物摘要(只读;symlink 视同缺失,不跟出树)。"""
    path = Path(dst_root) / TOOL_UNDERSTANDING_FILE
    if path.is_symlink() or not path.is_file():
        reason = "unsafe symlink" if path.is_symlink() else "missing"
        return {"present": False, "path": str(path),
                "warning": f"tool_understanding.json {reason} — 四步闸第 1 步(先理解工具)无证据物"}
    raw = path.read_bytes()
    summary: dict = {"present": True, "path": str(path),
                     "sha256": hashlib.sha256(raw).hexdigest()}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        summary["parse_error"] = "not valid JSON"
        return summary
    if isinstance(data, dict):
        summary["schema"] = data.get("schema")
        key_flows = data.get("key_flows")
        summary["key_flows_count"] = len(key_flows) if isinstance(key_flows, list) else 0
    else:
        summary["parse_error"] = "not a JSON object"
    return summary


def _load_sync_gate_policy(source_root: Path, override_path=None) -> dict:
    """读四步闸策略。默认文件缺失 → 全 soft(未采用,不新增失败面);
    文件存在但损坏/形状非法 → 返回 error(权威损坏 fail-closed,由 sync() 拒绝)。"""
    path = Path(override_path) if override_path else Path(source_root) / SYNC_GATE_POLICY_FILE
    if not path.is_file():
        if override_path:
            return {"error": f"gate policy 不存在: {path}"}
        return {"default_mode": "soft", "skills": {}, "source": None}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"error": f"gate policy 不可解析: {exc}", "source": str(path)}
    if not isinstance(data, dict):
        return {"error": "gate policy 必须是 JSON object", "source": str(path)}
    default_mode = data.get("default_mode", "soft")
    raw_skills = data.get("skills", {})
    if default_mode not in _GATE_MODES or not isinstance(raw_skills, dict):
        return {"error": "gate policy: default_mode 须为 soft|hard 且 skills 须为 object",
                "source": str(path)}
    skills: dict[str, str] = {}
    for name, entry in raw_skills.items():
        mode = entry.get("mode", "soft") if isinstance(entry, dict) else entry
        if mode not in _GATE_MODES:
            return {"error": f"gate policy: {name}.mode 须为 soft|hard", "source": str(path)}
        skills[str(name)] = mode
    return {"default_mode": default_mode, "skills": skills, "source": str(path)}


def _gate_mode_for(policy: dict, skill_name: str) -> str:
    return policy.get("skills", {}).get(skill_name, policy.get("default_mode", "soft"))


def _normalize_upstream_gaps(gaps) -> "list[dict] | str":
    """校验 upstream_gaps 结构({code, note, file});非法时返回错误字符串。"""
    out: list[dict] = []
    for gap in gaps or []:
        if not isinstance(gap, dict):
            return "upstream_gap 必须是 {code, note, file} 结构"
        code, note, file_ref = gap.get("code"), gap.get("note"), gap.get("file")
        if not isinstance(code, str) or not code.strip():
            return "upstream_gap.code 须为非空字符串"
        if not isinstance(note, str) or not note.strip():
            return "upstream_gap.note 须为非空字符串"
        if file_ref is not None and (not isinstance(file_ref, str) or not file_ref.strip()):
            return "upstream_gap.file 须为 null 或非空字符串"
        out.append({"code": code.strip(), "note": note.strip(),
                    "file": file_ref.strip() if isinstance(file_ref, str) else None})
    return out


def _run_target_eval(dst_root: Path) -> dict:
    """在 target 自身跑 scripts/eval_suite.py --json(隔离 ZE_DATA_DIR/ZE_USERS_PATH/
    ZE_SYSTEM_AUTH_DIR,防污染真实运行态与共享 auth;C2 教训)。

    返回解析后的 eval payload;不可用时返回 {"_unavailable": reason}。stdout 可能带
    bootstrap 期的告警前缀行(如 credential key 提示),从首个 '{' 起解析。"""
    dst_root = Path(dst_root)
    script = dst_root / "scripts" / "eval_suite.py"
    if not script.is_file():
        return {"_unavailable": "target 缺 scripts/eval_suite.py(toolchain 未同步到含 eval_suite 的版本)"}
    with tempfile.TemporaryDirectory(prefix="ze-sync-eval-") as td:
        env = {**os.environ,
               "ZE_DATA_DIR": os.path.join(td, "data"),
               "ZE_USERS_PATH": os.path.join(td, "users.json"),
               "ZE_SYSTEM_AUTH_DIR": os.path.join(td, "auth")}
        try:
            proc = subprocess.run(
                [sys.executable, "scripts/eval_suite.py", "--json"],
                cwd=str(dst_root), env=env, capture_output=True, text=True, timeout=600,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {"_unavailable": f"eval_suite 进程失败: {type(exc).__name__}: {exc}"}
    stdout = proc.stdout or ""
    idx = stdout.find("{")
    if idx < 0:
        stderr_tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return {"_unavailable":
                f"eval_suite 无 JSON 输出(exit {proc.returncode}): {stderr_tail[0][:200]}"}
    try:
        payload = json.loads(stdout[idx:])
    except ValueError as exc:
        return {"_unavailable": f"eval_suite 输出不可解析: {exc}"}
    if not isinstance(payload, dict):
        return {"_unavailable": "eval_suite 输出不是 JSON object"}
    return payload


def _stable_case_errors(errors) -> list[str]:
    """时延预算抖动不入等价对照(eval_suite 自身的 latency 门仍在;这里只比行为)。"""
    return [str(e) for e in (errors or []) if not str(e).startswith("latency ")]


def _eval_snapshot(payload) -> dict:
    """把 eval payload 归一成可对照快照(去 latency 抖动,保 status/errors)。"""
    if not isinstance(payload, dict):
        return {"available": False, "reason": "no payload"}
    if payload.get("_unavailable"):
        return {"available": False, "reason": str(payload["_unavailable"])}
    if payload.get("skipped"):
        return {"available": True, "declared": False}
    cases: dict[str, dict] = {}
    for case in payload.get("cases") or []:
        if not isinstance(case, dict):
            continue
        cases[str(case.get("id"))] = {
            "status": case.get("status"),
            "errors": _stable_case_errors(case.get("errors")),
        }
    return {"available": True, "declared": True, "ok": bool(payload.get("ok")),
            "case_count": int(payload.get("case_count") or 0), "cases": cases}


def _equivalence_diff(pre: dict, post: dict) -> list[dict]:
    """逐 case 对照 pre/post 快照;post 侧整套不可用/未声明本身就是行为面破坏。"""
    if not post.get("available"):
        return [{"case": "__suite__", "kind": "suite_unavailable",
                 "note": f"apply 后 eval_suite 不可用: {post.get('reason')}"}]
    if not post.get("declared"):
        return [{"case": "__suite__", "kind": "suite_undeclared",
                 "note": "apply 后 registry.eval_cases 不再可见"}]
    diffs: list[dict] = []
    pre_cases, post_cases = pre.get("cases") or {}, post.get("cases") or {}
    for cid in sorted(set(pre_cases) | set(post_cases)):
        p, q = pre_cases.get(cid), post_cases.get(cid)
        if p is None:
            diffs.append({"case": cid, "kind": "added", "post": q})
        elif q is None:
            diffs.append({"case": cid, "kind": "removed", "pre": p})
        elif p != q:
            diffs.append({"case": cid, "kind": "changed", "pre": p, "post": q})
    return diffs


def _equivalence_section(pre_payload, dst_root: Path, *, apply: bool,
                         run_eq: bool, copied_any: bool) -> dict:
    """组装 receipt.equivalence 段(四步闸第 2 步)。"""
    if not run_eq:
        return {"cases": 0, "skipped": True,
                "reason": "not_run(dry-run 未要求等价对照;--receipt-out 或 --apply 时运行)"}
    pre = _eval_snapshot(pre_payload)
    if not pre["available"]:
        return {"cases": 0, "skipped": True,
                "reason": f"eval_suite unavailable: {pre.get('reason', 'unknown')}"}
    if not pre["declared"]:
        return {"cases": 0, "skipped": True,
                "reason": "registry.eval_cases 未声明(声明后此闸才有行为信号)"}
    section: dict = {"cases": pre.get("case_count", 0), "skipped": False, "pre": pre}
    if not apply:
        section.update({"mode": "baseline_only", "post": None, "diff": None})
        return section
    if not copied_any:
        section.update({"mode": "pre_post", "post": None, "diff": [],
                        "note": "受管层无实际复制,前后行为面未变"})
        return section
    post = _eval_snapshot(_run_target_eval(dst_root))
    section.update({"mode": "pre_post", "post": post,
                    "diff": _equivalence_diff(pre, post)})
    return section


def _write_sync_receipt(dst_root: Path, receipt_doc: dict) -> dict:
    """--apply 落账:receipt 写 target 根 ZE_SYNC_RECEIPT.json(collect_upstream_gaps 聚合源)。"""
    path = Path(dst_root) / SYNC_RECEIPT_FILE
    try:
        _guard_no_target_symlink(path, Path(dst_root))
        if path.exists() and not path.is_file():
            raise RuntimeError("receipt destination is not a regular file")
        path.write_text(json.dumps(receipt_doc, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    except (OSError, RuntimeError) as exc:
        return {"written": False, "path": str(path), "reason": f"{type(exc).__name__}: {exc}"}
    return {"written": True, "path": str(path)}


def _assert_constant_allowlist() -> None:
    """静态自检:LAYER_DIRS/FILES 常量不得含 02-mod / .data / 路径逃逸。

    注意:这只校常量;运行时对 dst_root 的真正护栏是 sync() 里的 is_v3_skill() +
    copy_layer() 里的 _guard_inside()。
    """
    for d in [*sum(LAYER_DIRS.values(), []), *sum(LAYER_FILES.values(), [])]:
        assert not d.startswith("02-mod") and ".data" not in d and ".." not in Path(d).parts, \
            f"unsafe copy target in constant: {d}"


def _guard_inside(dst: Path, dst_root: Path) -> None:
    """运行时护栏:dst 必须严格在 dst_root 内、且不是 dst_root 本身(防 rmtree/写越界)。"""
    dst_r, root_r = dst.resolve(), dst_root.resolve()
    if dst_r == root_r or root_r not in dst_r.parents:
        raise RuntimeError(f"unsafe copy dst {dst_r}(不在 {root_r} 内部)")


_BASE_PROTECTED_TARGET_PATHS = {
    "02-mod",
    LOCK_DIR,
    ".data",
    "ZE_SURFACE.json",
    "ZE_LAYER_MANIFEST.json",
    # 四步闸第 1 步证据物:design 侧 ze.tool-understanding.v1 的 sibling 产物,下游自有,
    # sync 只读摘要入 receipt,永不覆盖。
    "tool_understanding.json",
}


def _clean_target_rel(value) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip().replace("\\", "/").rstrip("/")
    if not raw:
        return None
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix()


def _target_protected_paths(dst_root: Path) -> set[str]:
    """Collect every target-owned path that a managed write must not cross."""
    protected = set(_BASE_PROTECTED_TARGET_PATHS)
    if layer_manifest is None:
        return protected
    try:
        manifest = layer_manifest.load_layer_manifest(dst_root)
    except Exception:
        return protected
    candidates = manifest.get("preserve") if isinstance(manifest, dict) else None
    if isinstance(candidates, list):
        protected.update(
            cleaned for value in candidates if (cleaned := _clean_target_rel(value))
        )
    product = manifest.get("product") if isinstance(manifest, dict) else None
    if isinstance(product, dict):
        for key in ("dirs", "mod_dirs", "runtime_dirs", "metadata_files"):
            values = product.get(key)
            if isinstance(values, list):
                protected.update(
                    cleaned for value in values if (cleaned := _clean_target_rel(value))
                )
    return protected


def _relative_target_path(dst: Path, dst_root: Path) -> str:
    # Containment is lexical: resolving ``dst`` would follow the very symlink
    # this guard is meant to detect.  Keep root and child in the same spelling
    # so macOS /var -> /private/var aliases do not create false escapes.
    root = Path(os.path.abspath(os.fspath(dst_root)))
    child = Path(os.path.abspath(os.fspath(dst)))
    try:
        relative = child.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"unsafe lexical copy dst {dst}(不在 {root} 内部)") from exc
    if not relative.parts:
        raise RuntimeError(f"unsafe copy dst {dst}(不能覆盖 target root)")
    return PurePosixPath(*relative.parts).as_posix()


def _guard_no_target_symlink(dst: Path, dst_root: Path) -> None:
    """Reject the destination and every existing parent symlink before writes."""
    relative = PurePosixPath(_relative_target_path(dst, dst_root))
    root = dst_root.resolve()
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RuntimeError(f"unsafe symlink in managed destination: {current}")


def _guard_managed_destination(
        dst: Path,
        dst_root: Path,
        *,
        allowed_protected_descendants: set[str] | None = None,
) -> None:
    """Preflight one managed destination against symlink and ownership escapes."""
    rel = _relative_target_path(dst, dst_root)
    _guard_no_target_symlink(dst, dst_root)
    root = dst_root.resolve()
    rel_path = PurePosixPath(rel)
    allowed = {
        cleaned
        for value in (allowed_protected_descendants or set())
        if (cleaned := _clean_target_rel(value))
    }
    for protected in sorted(_target_protected_paths(root)):
        protected_path = PurePosixPath(protected)
        if rel_path == protected_path or protected_path in rel_path.parents:
            raise RuntimeError(
                f"managed destination enters protected target path: {rel} vs {protected}"
            )
        if rel_path in protected_path.parents and protected not in allowed:
            raise RuntimeError(
                f"managed replacement contains protected target path: {rel} vs {protected}"
            )
    if dst.exists() and not (dst.is_file() or dst.is_dir()):
        raise RuntimeError(f"unsupported managed destination type: {dst}")


def _copy_file_atomic(src: Path, dst: Path, dst_root: Path) -> None:
    """Replace a named managed file without following target symlinks/hardlinks."""
    _guard_managed_destination(dst, dst_root)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _guard_no_target_symlink(dst, dst_root)
    if dst.exists() and not dst.is_file():
        raise RuntimeError(f"managed file destination is not a regular file: {dst}")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=dst.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        shutil.copy2(src, tmp_path)
        with tmp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        # os.replace changes the directory entry itself; unlike copy2(dst), it
        # cannot truncate a hardlink target or follow a raced final symlink.
        os.replace(tmp_path, dst)
    finally:
        tmp_path.unlink(missing_ok=True)


def _guard_webapp_merge_children(dst: Path, preserve_names: set[str]) -> None:
    """Fail before any layer write on ambiguous top-level merge symlinks."""
    if not dst.exists():
        return
    for child in sorted(dst.iterdir(), key=lambda path: path.name):
        if child.name not in preserve_names and child.is_symlink():
            raise RuntimeError(f"unsafe symlink in webapp/src managed merge: {child}")


def _guard_managed_tree_entries(
        dst: Path,
        dst_root: Path,
        *,
        excluded_subtrees: set[str] | None = None,
) -> None:
    """Reject links/special files anywhere inside a replace/merge inventory.

    Checking only the managed directory itself is insufficient: a nested link
    can make a checksum read runtime/product bytes that the next copy would
    delete as a directory entry.  Product subtrees are excluded because they
    are validated separately and are not part of the managed inventory.
    """
    if not dst.exists() or not dst.is_dir():
        return
    excluded = {
        PurePosixPath(cleaned)
        for value in (excluded_subtrees or set())
        if (cleaned := _clean_target_rel(value))
    }
    stack = [dst]
    while stack:
        directory = stack.pop()
        try:
            with os.scandir(directory) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError as exc:
            raise RuntimeError(
                f"cannot inspect managed directory tree: {directory}: {type(exc).__name__}"
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            rel = PurePosixPath(_relative_target_path(path, dst_root))
            if any(rel == subtree or subtree in rel.parents for subtree in excluded):
                continue
            if entry.is_symlink():
                raise RuntimeError(f"unsafe nested symlink in managed directory: {path}")
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                elif not entry.is_file(follow_symlinks=False):
                    raise RuntimeError(f"unsupported nested managed file type: {path}")
            except OSError as exc:
                raise RuntimeError(
                    f"cannot inspect managed directory entry: {path}: {type(exc).__name__}"
                ) from exc


def _inactive_optional_core_errors(root: Path) -> list[str]:
    """Reject artifacts whose former Surface ownership is no longer declared.

    Once a target disables Web or service delivery, its old managed shell cannot
    simply disappear from the projected checksum.  We also cannot delete that
    tree automatically because it may still contain downstream product data.
    The safe transition is therefore explicit cleanup/migration followed by a
    new sync.
    """
    errors: list[str] = []
    webapp_src = root / "webapp" / "src"
    if not _target_has_surface(root, "web") and (
            webapp_src.exists() or webapp_src.is_symlink()):
        errors.append(
            "inactive web Surface still has webapp/src; migrate/preserve product data "
            "and remove the retired managed Web shell before syncing core"
        )
    server = root / "server.py"
    if not _target_is_service(root) and (server.exists() or server.is_symlink()):
        errors.append(
            "inactive service Surface still has server.py; remove the retired managed "
            "service entrypoint before syncing core"
        )
    return errors


def audit_managed_target(
        dst_root: Path,
        layers: tuple[str, ...] | list[str] = LAYERS,
) -> dict:
    """Read-only preflight for every destination a layer may own.

    This runs even when checksums are ``same``.  Otherwise a managed directory
    can be replaced by a symlink into runtime state while its followed bytes
    still match the lock, producing a false-current result.
    """
    root = Path(dst_root).resolve()
    _validated_target_surface_profile(root)
    if layer_manifest is not None and (
            (root / layer_manifest.MANIFEST_FILE).exists()
            or (root / layer_manifest.MANIFEST_FILE).is_symlink()):
        manifest = layer_manifest.load_layer_manifest(root)
        manifest_errors = layer_manifest.validate_layer_manifest(root, manifest)
        if manifest_errors:
            raise LayerManifestError(root, manifest_errors)

    web_enabled = _target_has_surface(root, "web")
    service_enabled = _target_is_service(root)
    product_names = _webapp_src_preserve_names(root) if web_enabled else set()
    product_paths = {f"webapp/src/{name}" for name in product_names}
    errors: list[str] = []
    for layer in layers:
        if layer not in LAYER_DIRS or layer not in LAYER_FILES:
            errors.append(f"unknown managed layer: {layer}")
            continue
        if layer == "core":
            errors.extend(f"core: {error}" for error in _inactive_optional_core_errors(root))
        for rel in LAYER_DIRS[layer]:
            if rel == "webapp/src" and not web_enabled:
                continue
            if rel == "03-css-rules" and not web_enabled:
                continue
            dst = root / rel
            try:
                _guard_managed_destination(
                    dst,
                    root,
                    allowed_protected_descendants=(
                        product_paths if rel == "webapp/src" else set()
                    ),
                )
                if rel == "webapp/src":
                    _guard_webapp_merge_children(dst, product_names)
                _guard_managed_tree_entries(
                    dst,
                    root,
                    excluded_subtrees=(
                        product_paths if rel == "webapp/src" else set()
                    ),
                )
            except RuntimeError as exc:
                errors.append(f"{layer}:{rel}: {exc}")
        for rel in LAYER_FILES[layer]:
            if rel == "server.py" and not service_enabled:
                continue
            try:
                _guard_managed_destination(root / rel, root)
            except RuntimeError as exc:
                errors.append(f"{layer}:{rel}: {exc}")
    if errors:
        raise ManagedTargetError(root, errors)
    return {"ok": True, "layers": list(layers), "errors": []}


def copy_layer(layer: str, src_root: Path, dst_root: Path) -> list[str]:
    """Copy one managed layer and return copied relative paths.

    Most managed directories are rmtree + copytree. `webapp/src` is special:
    it contains both core shell files and downstream-owned `product/`, so sync
    replaces core files but preserves `webapp/src/product`.
    """
    # Must run before mkdir/rmtree/copy. An explicit invalid profile is not a
    # headless fallback and may never authorize a managed write.
    src_root = Path(src_root).resolve()
    dst_root = Path(dst_root).resolve()
    audit_managed_target(dst_root, [layer])
    copied = []
    copy_ignore = _copy_ignore_for_root(src_root)
    web_enabled = _target_has_surface(dst_root, "web")
    service_enabled = _target_is_service(dst_root)
    directory_plan: list[tuple[str, Path, Path]] = []
    file_plan: list[tuple[str, Path, Path]] = []
    product_names = _webapp_src_preserve_names(dst_root)
    product_paths = {f"webapp/src/{name}" for name in product_names}
    for rel in LAYER_DIRS[layer]:
        if rel == "webapp/src" and not web_enabled:
            continue
        if rel == "03-css-rules" and not web_enabled:
            continue
        src, dst = src_root / rel, dst_root / rel
        if not src.is_dir():
            continue
        allowed = product_paths if rel == "webapp/src" else set()
        _guard_managed_destination(
            dst,
            dst_root,
            allowed_protected_descendants=allowed,
        )
        if rel == "webapp/src":
            _guard_webapp_merge_children(dst, product_names)
        directory_plan.append((rel, src, dst))
    for rel in LAYER_FILES[layer]:
        if rel == "server.py" and not service_enabled:
            continue
        src, dst = src_root / rel, dst_root / rel
        if not src.is_file():
            continue
        _guard_managed_destination(dst, dst_root)
        file_plan.append((rel, src, dst))

    # Nothing mutates before the complete layer plan has passed preflight.
    for rel, src, dst in directory_plan:
        if rel == "webapp/src":
            _copy_webapp_src(src, dst, dst_root)
            copied.append(rel + "/")
            continue
        _guard_inside(dst, dst_root)        # rmtree 前的真正越界护栏
        if dst.exists():
            shutil.rmtree(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, ignore=copy_ignore)
        copied.append(rel + "/")
    for rel, src, dst in file_plan:
        _copy_file_atomic(src, dst, dst_root)
        copied.append(rel)
    return copied


_LOCK_SCHEMA = "ze.interface_lock.v2"
_LOCK_REQUIRED_KEYS = {
    "schema", "layer", "interface_version", "checksum", "locked_at",
}
_LOCK_OPTIONAL_KEYS = {"framework", "contract", "covers", "_comment"}
_LOCK_CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")


def _reject_duplicate_json_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _interface_lock_errors(lock, layer: str) -> list[str]:
    """Validate one interface lock as authority, not best-effort metadata."""
    if not isinstance(lock, dict):
        return ["lock must contain a JSON object"]
    errors: list[str] = []
    keys = set(lock)
    missing = sorted(_LOCK_REQUIRED_KEYS - keys)
    # `_` 前缀 = 人类附注命名空间(如 _comment/_migration):不参与权威校验,任意个数合法。
    # (design coresync 反哺:白名单只认 _comment 时,MAJOR 迁移声明附注被当"损坏"拒。)
    unknown = sorted(k for k in keys - _LOCK_REQUIRED_KEYS - _LOCK_OPTIONAL_KEYS
                     if not k.startswith("_"))
    if missing:
        errors.append("missing required fields: " + ", ".join(missing))
    if unknown:
        errors.append("unknown fields: " + ", ".join(unknown))
    if lock.get("schema") != _LOCK_SCHEMA:
        errors.append(f"schema must be {_LOCK_SCHEMA}")
    if lock.get("layer") != layer:
        errors.append(f"layer must be {layer}")
    version = lock.get("interface_version")
    if not isinstance(version, str) or not version.strip():
        errors.append("interface_version must be a non-empty string")
    checksum = lock.get("checksum")
    if not isinstance(checksum, str) or _LOCK_CHECKSUM_RE.fullmatch(checksum) is None:
        errors.append("checksum must be 64 lowercase hexadecimal characters")
    locked_at = lock.get("locked_at")
    if not isinstance(locked_at, str) or not locked_at.strip():
        errors.append("locked_at must be a non-empty string")
    annotation_keys = [k for k in keys if k.startswith("_")]
    for key in ["framework", "contract", *annotation_keys]:
        if key in lock and (not isinstance(lock[key], str) or not lock[key].strip()):
            errors.append(f"{key} must be a non-empty string when present")
    if "covers" in lock:
        covers = lock["covers"]
        if not isinstance(covers, list) or any(
                not isinstance(item, str) or not item.strip() for item in covers):
            errors.append("covers must be a list of non-empty strings when present")
    return errors


def _read_lock(dst_root: Path, layer: str) -> dict | None:
    dst_root = Path(dst_root).resolve()
    p = dst_root / LOCK_DIR / f"{layer}.lock.json"
    try:
        _guard_no_target_symlink(p, dst_root)
    except RuntimeError:
        return {"_error": "unsafe lock symlink"}
    if not p.exists():
        return None
    if not p.is_file():
        return {"_error": "lock not regular file"}
    try:
        data = json.loads(
            p.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (json.JSONDecodeError, OSError, UnicodeError, ValueError) as exc:
        return {"_error": f"lock not strict JSON: {exc}"}
    errors = _interface_lock_errors(data, layer)
    return {"_error": "; ".join(errors)} if errors else data


def _write_lock(dst_root: Path, layer: str, *, version: str, checksum: str, base: dict | None) -> None:
    dst_root = Path(dst_root).resolve()
    p = dst_root / LOCK_DIR / f"{layer}.lock.json"
    _guard_no_target_symlink(p, dst_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    _guard_no_target_symlink(p, dst_root)
    if p.exists() and not p.is_file():
        raise RuntimeError(f"lock destination is not a regular file: {p}")
    if base:
        base_errors = _interface_lock_errors(base, layer)
        if base_errors:
            raise RuntimeError("refusing to rewrite invalid interface lock: " + "; ".join(base_errors))
    lock = dict(base) if base else {
        "schema": _LOCK_SCHEMA, "layer": layer, "framework": "ZE-skill-build",
    }
    lock.update({
        "schema": _LOCK_SCHEMA, "layer": layer,
        "interface_version": version, "checksum": checksum,
        "locked_at": datetime.now(timezone.utc).isoformat(),
    })
    write_errors = _interface_lock_errors(lock, layer)
    if write_errors:
        raise RuntimeError("refusing to write invalid interface lock: " + "; ".join(write_errors))
    encoded = (json.dumps(lock, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
    tmp_path = Path(tmp_name)
    try:
        handle = os.fdopen(fd, "wb")
        fd = -1
        with handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, p)
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        tmp_path.unlink(missing_ok=True)


# ==============================================================
# 主流程
# ==============================================================

def _has_v3_base_shape(root: Path) -> bool:
    return all(
        (root / directory).is_dir()
        for directory in ("01-core", "02-mod", "04-ops", "05-i18n", "sdk")
    )


def is_v3_skill(root: Path) -> bool:
    if not _has_v3_base_shape(root):
        return False
    required = ["01-core", "02-mod", "04-ops", "05-i18n", "sdk"]
    if _target_has_surface(root, "web"):
        required.extend(["03-css-rules", "webapp"])
    return all((root / d).is_dir() for d in required)


def _target_has_surface(root: Path, surface: str) -> bool:
    if load_surface_profile is None or has_surface is None:
        if (root / "ZE_SURFACE.json").exists() or (root / "ZE_SURFACE.json").is_symlink():
            raise SurfaceProfileError(root, ["surface_profile validator unavailable"])
        return (root / "webapp").exists() if surface == "web" else (root / "scripts" / "ze_command.py").is_file()
    profile = _validated_target_surface_profile(root)
    if profile is None:
        return (root / "webapp").exists() if surface == "web" else False
    return has_surface(profile, surface)


def _target_is_service(root: Path) -> bool:
    if load_surface_profile is None or is_service_surface is None or deploy_info is None:
        if (root / "ZE_SURFACE.json").exists() or (root / "ZE_SURFACE.json").is_symlink():
            raise SurfaceProfileError(root, ["surface_profile validator unavailable"])
        return (root / "webapp").exists() or (root / "server.py").exists()
    profile = _validated_target_surface_profile(root)
    if profile is None:
        return (root / "webapp").exists() or (root / "server.py").exists()
    # An explicit Surface Profile is the control-plane authority.  deploy.json
    # may help infer a legacy target only when that authority file is absent;
    # it cannot secretly turn an explicit CLI-only target back into a service.
    surface_path = root / "ZE_SURFACE.json"
    if surface_path.exists() or surface_path.is_symlink():
        return has_surface(profile, "web") or has_surface(profile, "api")
    return is_service_surface(profile, deploy_info(root))


def _ensure_layer_manifest(dst_root: Path, *, apply: bool, reconcile_toolchain: bool = False) -> dict:
    """Ensure the target declares managed/product ownership boundaries.

    Missing manifests are non-blocking in dry-run so older skills can still be
    inspected. `--apply` writes the manifest before copying managed layers.
    Invalid existing manifests block sync because direct-copy safety depends on
    the boundary being machine-readable.
    """
    if layer_manifest is None:
        return {"status": "unavailable", "reason": "layer_manifest.py not importable"}
    path = dst_root / layer_manifest.MANIFEST_FILE
    if not path.exists() and not path.is_symlink():
        if apply:
            return layer_manifest.write_layer_manifest(
                dst_root,
                toolchain_files=LAYER_FILES["toolchain"],
                overwrite=False,
            )
        return {
            "status": "missing",
            "path": str(path),
            "hint": "will be written by --apply before managed layers are copied",
            "errors": [],
        }
    if apply and reconcile_toolchain:
        return layer_manifest.reconcile_toolchain_files(
            dst_root,
            toolchain_files=LAYER_FILES["toolchain"],
        )
    manifest = layer_manifest.load_layer_manifest(dst_root)
    errors = (
        layer_manifest.validate_layer_manifest(dst_root, manifest)
        + layer_manifest.toolchain_inventory_errors(manifest, LAYER_FILES["toolchain"])
    )
    return {
        "status": "existing",
        "path": str(path),
        "errors": errors,
    }


_PY_DEP_NAME_RE = re.compile(r"[<>=!~\[;]")


def _py_dep_name(spec: str) -> str:
    return _PY_DEP_NAME_RE.split(spec, 1)[0].strip().lower().replace("_", "-")


def _declared_py_deps_check(
    deps_path: Path,
    dst_root: Path,
    *,
    contract_name: str,
) -> "dict | None":
    """Compare one managed dependency declaration with target requirements.

    Missing packages are warnings rather than mutations: requirements.txt is
    target-owned, so Sync reports the exact lower bounds but never rewrites it.
    """
    if not deps_path.is_file():
        return None
    try:
        payload = json.loads(deps_path.read_text(encoding="utf-8"))
        requires = payload.get("requires") or []
    except (OSError, ValueError):
        return {"status": "error", "reason": f"{contract_name} 不可解析"}
    if not isinstance(requires, list) or any(
        not isinstance(spec, str) or not _py_dep_name(spec)
        for spec in requires
    ):
        return {"status": "error", "reason": f"{contract_name}.requires 非法"}
    req_file = Path(dst_root) / "requirements.txt"
    if not req_file.is_file():
        return {"status": "missing_requirements", "required": requires,
                "missing": list(requires)}
    have = set()
    for line in req_file.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            name = _py_dep_name(line)
            if name:
                have.add(name)
    missing = [spec for spec in requires if _py_dep_name(spec) not in have]
    return {"status": "missing" if missing else "ok",
            "required": requires, "missing": missing}


def _core_py_deps_check(source_root: Path, dst_root: Path) -> "dict | None":
    """CORE_PY_DEPS.json vs target requirements.txt (3.55.0 feedback)."""
    return _declared_py_deps_check(
        Path(source_root) / "01-core" / "_protocol" / "CORE_PY_DEPS.json",
        dst_root,
        contract_name="CORE_PY_DEPS.json",
    )


def _toolchain_py_deps_check(source_root: Path, dst_root: Path) -> "dict | None":
    """Managed Build validator dependencies vs target requirements.txt."""
    return _declared_py_deps_check(
        Path(source_root) / "scripts" / "TOOLCHAIN_PY_DEPS.json",
        dst_root,
        contract_name="TOOLCHAIN_PY_DEPS.json",
    )


def _web_config_drift(source_root: Path, dst_root: Path) -> "dict | None":
    """webapp 配置漂移对照(3.55.0 反哺):deps/tsconfig/vite dedupe 三面预警。

    所有权仍归下游(sync-never-clobber),只报 WARN 供 MAJOR/MINOR 迁移时人工对齐;
    任一侧文件缺失或不可解析的面静默跳过。"""
    src_pkg = Path(source_root) / "webapp" / "package.json"
    dst_pkg = Path(dst_root) / "webapp" / "package.json"
    if not src_pkg.is_file() or not dst_pkg.is_file():
        return None
    out: dict = {}
    try:
        s = json.loads(src_pkg.read_text(encoding="utf-8"))
        d = json.loads(dst_pkg.read_text(encoding="utf-8"))
        missing = sorted(set(s.get("dependencies") or {}) - set(d.get("dependencies") or {}))
        if missing:
            out["missing_dependencies"] = missing
    except ValueError:
        out["package_json"] = "unparseable"
    try:
        st = json.loads((Path(source_root) / "webapp" / "tsconfig.json").read_text(encoding="utf-8"))
        dt = json.loads((Path(dst_root) / "webapp" / "tsconfig.json").read_text(encoding="utf-8"))
        sco = st.get("compilerOptions") or {}
        dco = dt.get("compilerOptions") or {}
        drift = sorted(k for k in sco if dco.get(k) != sco[k])
        if drift:
            out["tsconfig_compiler_option_drift"] = drift
    except (ValueError, OSError):
        pass
    try:
        def _dedupe(text: str) -> "list[str] | None":
            m = re.search(r"dedupe:\s*\[([^\]]*)\]", text)
            return sorted(re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))) if m else None
        s_dd = _dedupe((Path(source_root) / "webapp" / "vite.config.ts").read_text(encoding="utf-8"))
        d_dd = _dedupe((Path(dst_root) / "webapp" / "vite.config.ts").read_text(encoding="utf-8"))
        if s_dd is not None and d_dd is not None:
            miss = sorted(set(s_dd) - set(d_dd))
            if miss:
                out["vite_dedupe_missing"] = miss
    except OSError:
        pass
    return out if out else {"status": "aligned"}


def sync(
        target: str,
        *,
        layers: list[str],
        apply: bool,
        auto_bump: bool = True,
        source_root: Path = BUILD_ROOT,
        force_overwrite: bool = False,
        receipt: dict | None = None,
        upstream_gaps: list[dict] | None = None,
        gate_policy_path: str | Path | None = None,
        run_equivalence: bool | None = None,
) -> dict:
    _assert_constant_allowlist()
    dst = Path(target).resolve()
    source_root = Path(source_root).resolve()
    # 三方安全:强覆盖冲突必须消费一份冻结计划(先 --receipt-out 复核、apply 时二验 Target)。
    if force_overwrite and receipt is None:
        return {"ok": False,
                "error": "--force-overwrite 须配冻结 receipt(先 --receipt-out 冻结复核,apply 时二验 Target)"}
    if dst == BUILD_ROOT:
        return {"ok": False, "error": "拒绝把 build-v3 同步到自身(会 rmtree 自己的 core)"}
    if not _has_v3_base_shape(dst):
        return {"ok": False, "error": f"{dst} 不像 v3 skill(缺 01-core/02-mod/03-css-rules/sdk)"}
    try:
        _validated_target_surface_profile(dst)
    except SurfaceProfileError as exc:
        return {
            "ok": False,
            "error": "ZE_SURFACE.json 无效,拒绝计算投影、复制受管层或写 lock",
            "surface_profile": exc.as_dict(),
        }
    if not is_v3_skill(dst):
        return {"ok": False, "error": f"{dst} 不像 v3 skill(缺 01-core/02-mod/03-css-rules/sdk)"}
    # No ownership file, lock, or managed content may change before the entire
    # destination shape has passed a read-only audit.  Missing manifests are
    # safely inferred for this preflight and materialized only afterwards.
    try:
        audit_managed_target(dst, layers)
    except LayerManifestError as exc:
        return {
            "ok": False,
            "error": "ZE_LAYER_MANIFEST.json 无效,拒绝复制受管层",
            "layer_manifest": {
                "status": "invalid",
                "path": str(exc.path),
                "errors": list(exc.errors),
            },
            # design coresync 反哺:旧 manifest(清单/顺序过期)会双卡 dry-run,操作者无从下手。
            "hint": ("目标 manifest 多为上游演进后的过期投影,不是目标自身损坏:确认 product/"
                     "preserve 边界仍正确后,备份并删除目标 ZE_LAYER_MANIFEST.json,"
                     "--apply 会按当前 toolchain 清单重写;或手工对齐 errors 列出的差异项。"),
        }
    except (SurfaceProfileError, ManagedTargetError) as exc:
        return {
            "ok": False,
            "error": "受管目标形状不安全,拒绝分类、复制或写 lock",
            "managed_target": {
                "status": "invalid",
                "kind": type(exc).__name__,
                "errors": list(getattr(exc, "errors", [str(exc)])),
            },
        }

    # ---- 同步四步闸(纯增量;既有三方协议/receipt 二验/保护路径护栏原样) ----
    policy = _load_sync_gate_policy(source_root, override_path=gate_policy_path)
    if policy.get("error"):
        return {"ok": False, "error": f"同步四步闸策略无效: {policy['error']}",
                "gate": {"policy_source": policy.get("source")}}
    gate_mode = _gate_mode_for(policy, dst.name)
    gate_state = {"mode": gate_mode, "policy_source": policy.get("source")}
    gaps = _normalize_upstream_gaps(upstream_gaps)
    if isinstance(gaps, str):
        return {"ok": False, "error": gaps}
    understanding = _tool_understanding_summary(dst)
    warnings: list[str] = []
    blocked: list[dict] = []
    if not understanding["present"]:
        warnings.append(
            "四步闸第 1 步:target 缺 tool_understanding.json"
            "(soft 记录;hard 模式将拒绝 --apply)")
    if apply and gate_mode == "hard" and not understanding["present"]:
        # 硬门第 1 步在任何写入(manifest/层复制/lock)之前拒绝 → 零变更。
        blocked.append({
            "code": "understanding_missing",
            "note": "四步闸第 1 步(先理解工具):target 根缺 tool_understanding.json,hard 模式拒绝 --apply",
            "file": str(dst / TOOL_UNDERSTANDING_FILE),
        })
        return {
            "ok": False,
            "mode": "apply",
            "target": str(dst),
            "source_root": str(source_root),
            "error": "四步闸(hard):target 缺 tool_understanding.json,--apply 拒绝(零变更)。"
                     "先经 design 产线补理解证据物,或在 gate policy 降回 soft。",
            "gate": gate_state,
            "warnings": warnings,
            "receipt": {
                "schema": SYNC_RECEIPT_SCHEMA,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "mode": "apply",
                "source_root": str(source_root),
                "target": str(dst),
                "layers": {},
                "understanding": understanding,
                "equivalence": {"cases": 0, "skipped": True,
                                "reason": "blocked before copy(understanding_missing)"},
                "upstream_gaps": gaps,
                "gate": gate_state,
                "blocked": blocked,
            },
        }

    manifest_state = _ensure_layer_manifest(
        dst,
        apply=apply,
        reconcile_toolchain="toolchain" in layers,
    )
    if manifest_state.get("errors"):
        return {
            "ok": False,
            "error": "ZE_LAYER_MANIFEST.json 无效,拒绝复制受管层",
            "layer_manifest": manifest_state,
        }

    # 四步闸第 2 步:等价对照的 pre 侧(apply 默认跑;dry-run 仅在冻结 receipt 时跑基线)。
    run_eq = bool(apply) if run_equivalence is None else bool(run_equivalence)
    pre_eval = _run_target_eval(dst) if run_eq else None

    results = {}
    receipt_layers: dict[str, dict] = {}
    for layer in layers:
        src_ver, src_sum = source_contract(layer, source_root, target_root=dst)
        lock = _read_lock(dst, layer)
        # 损坏的 lock(JSON 解析失败)→ 硬错,绝不据 fallback 值瞎分类后去 rmtree
        if lock and "_error" in lock:
            results[layer] = {"status": "lock_error",
                              "hint": f"{LOCK_DIR}/{layer}.lock.json 损坏,人工修复(或删掉走 initialize)后重跑"}
            receipt_layers[layer] = {"layer": layer, "verdict": "lock_error"}
            continue
        status = classify(src_ver, src_sum, lock)
        # 三方:Base=lock / Candidate=src / Target=下游自校。classify() 只看前两者;plan 引入 Target。
        plan = three_way_plan(layer, source_root, dst, lock, candidate=(src_ver, src_sum))
        receipt_layers[layer] = plan
        verdict = plan["verdict"]
        tgt_sum = plan["target"]["checksum"]
        entry = {"status": status, "src_version": src_ver, "src_checksum": src_sum[:16] + "…",
                 "three_way": verdict, "target_checksum": tgt_sum[:16] + "…"}

        # receipt 门禁:apply 消费冻结计划时,二验 Target/Candidate 与冻结时一致(否则 dry-run 后又被改)
        if apply and receipt is not None:
            stale = _receipt_layer_stale(receipt, layer, src_sum, tgt_sum)
            if stale is not None:
                entry["status"] = "receipt_stale"
                entry["hint"] = stale
                results[layer] = entry
                continue

        if status == "incompat":
            entry["lock_version"] = lock.get("interface_version")
            entry["hint"] = (f"core 把 {layer} interface_version 从 {lock.get('interface_version')} "
                             f"提到 {src_ver}(MAJOR)。按 {layer} 的 MIGRATIONS.md 改 02-mod 后,"
                             f"手动 bump {LOCK_DIR}/{layer}.lock.json 再重跑。")
            results[layer] = entry
            continue
        if status == "same":
            results[layer] = entry
            continue

        # compat_bump 可能是真三方冲突(下游 fork:Target 偏离基线,上游也偏离)。默认不覆盖。
        if status == "compat_bump" and verdict == "conflict":
            if not force_overwrite:
                entry["status"] = "conflict"
                entry["hint"] = (
                    f"下游改了 {layer}(Target 偏离基线),上游也改了(Candidate 偏离基线)——双改冲突,"
                    f"默认不覆盖。先 `--receipt-out <计划>` 冻结复核,再 "
                    f"`--apply --receipt <计划> --force-overwrite` 才会覆盖(仍记账)。")
                results[layer] = entry
                continue
            entry["forced_overwrite"] = True

        # initialize / compat_bump(含 forced 冲突)→ 复制 + bump lock
        if apply:
            entry["copied"] = copy_layer(layer, source_root, dst)
            if auto_bump or status == "initialize":
                _write_lock(dst, layer, version=src_ver, checksum=src_sum, base=lock)
                entry["lock"] = "written"
            else:
                entry["lock"] = "skipped (--no-auto-bump)"
        else:
            entry["copied"] = "(dry-run)"
        results[layer] = entry

    summary = {}
    for r in results.values():
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    bad = (summary.get("incompat", 0) + summary.get("lock_error", 0)
           + summary.get("conflict", 0) + summary.get("receipt_stale", 0))

    # 四步闸第 2 步:post 侧 + diff(只有真复制过才有 post;diff 非空 → soft WARN / hard 拒)。
    copied_any = any(isinstance(r.get("copied"), list) for r in results.values())
    equivalence = _equivalence_section(
        pre_eval, dst, apply=apply, run_eq=run_eq, copied_any=copied_any)
    eq_diff = equivalence.get("diff") or []
    if eq_diff:
        if apply and gate_mode == "hard":
            blocked.append({
                "code": "equivalence_diff",
                "note": f"四步闸第 2 步:apply 前后 eval 等价对照发现 {len(eq_diff)} 处行为差异"
                        "(受管层已复制;见 receipt.equivalence.diff)",
                "file": None,
            })
        else:
            warnings.append(
                f"四步闸第 2 步:eval 等价对照发现 {len(eq_diff)} 处行为差异"
                "(soft 记录)— 见 receipt.equivalence.diff")

    receipt_doc = {
        "schema": SYNC_RECEIPT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "apply" if apply else "dry-run",
        "source_root": str(source_root),
        "target": str(dst),
        "layers": receipt_layers,
        "understanding": understanding,
        "equivalence": equivalence,
        "upstream_gaps": gaps,
        "gate": gate_state,
    }
    if "core" in layers:
        py_deps = _core_py_deps_check(source_root, dst)
        if py_deps is not None:
            receipt_doc["py_deps_check"] = py_deps
            if py_deps.get("missing"):
                warnings.append(
                    f"core 依赖下限缺口:目标 requirements.txt 缺 {py_deps['missing']}"
                    "(权威=01-core/_protocol/CORE_PY_DEPS.json;请手工补行,sync 不代写)")
        drift = _web_config_drift(source_root, dst)
        if drift is not None:
            receipt_doc["web_config_drift"] = drift
            if drift.get("missing_dependencies"):
                warnings.append(
                    f"webapp 配置漂移:目标 package.json 缺依赖 {drift['missing_dependencies']}"
                    "(补装后 `npm install --package-lock-only` 同步锁文件,否则远端 npm ci 失锁)")
    if "toolchain" in layers:
        toolchain_py_deps = _toolchain_py_deps_check(source_root, dst)
        if toolchain_py_deps is not None:
            receipt_doc["toolchain_py_deps_check"] = toolchain_py_deps
            if toolchain_py_deps.get("missing"):
                warnings.append(
                    "toolchain 依赖下限缺口:目标 requirements.txt 缺 "
                    f"{toolchain_py_deps['missing']}"
                    "(权威=scripts/TOOLCHAIN_PY_DEPS.json;请手工补行,sync 不代写)"
                )
    if blocked:
        receipt_doc["blocked"] = blocked

    next_steps: list[str] = []
    if bad:
        next_steps.append(
            f"{bad} 层 incompat/lock_error/conflict/receipt_stale — 见 per_layer 的 hint;"
            f"双改冲突先 --receipt-out 复核再 --apply --receipt --force-overwrite")
    if blocked:
        next_steps.append(
            "四步闸(hard)拦截 — 见 receipt.blocked;equivalence_diff 情况下受管层已复制,"
            "先 git 还原 target 受管层与 lock,再按 diff 排查上游 MINOR 夹带的行为变化")

    out = {
        "ok": bad == 0 and not blocked,
        "mode": "apply" if apply else "dry-run",
        "target": str(dst),
        "source_root": str(source_root),
        "layer_manifest": manifest_state,
        "summary": summary,
        "per_layer": results,
        "gate": gate_state,
        "receipt": receipt_doc,
        "next_steps": next_steps,
    }
    if warnings:
        out["warnings"] = warnings
    if apply:
        out["receipt_file"] = _write_sync_receipt(dst, receipt_doc)
    return out


def init_self_locks() -> dict:
    """给 build-v3 自身的 02-mod/_locks 写当前 checksum 基线(不复制)。

    只遍历 LAYERS(core/ops/i18n/toolchain)—— css 皮肤层是 opt-in,**故意不写自身 css.lock 基线、
    也不进 verify_self_locks**:build-v3 拥有皮肤源,无需自校;下游工具才在 `--layers css` 时拿到
    css.lock(供下次 classify)。这样皮肤定制永不被判漂移(见 --layers css 用法 + LAYER_DIRS 注)。"""
    try:
        shape = audit_managed_target(BUILD_ROOT, list(LAYERS))
    except (SurfaceProfileError, LayerManifestError, ManagedTargetError) as exc:
        return {
            "ok": False,
            "init_locks": {},
            "managed_target": {
                "status": "invalid",
                "kind": type(exc).__name__,
                "errors": list(getattr(exc, "errors", [str(exc)])),
            },
        }
    bases: dict[str, dict | None] = {}
    lock_errors: dict[str, str] = {}
    for layer in LAYERS:
        base = _read_lock(BUILD_ROOT, layer)
        if base and "_error" in base:
            lock_errors[layer] = str(base["_error"])
        bases[layer] = base
    if lock_errors:
        return {
            "ok": False,
            "init_locks": {},
            "managed_target": shape,
            "lock_errors": lock_errors,
            "hint": "现有 lock 不是严格的 ze.interface_lock.v2；先人工修复或删除后再初始化",
        }

    out = {}
    for layer in LAYERS:
        ver, sm = source_contract(layer)
        _write_lock(BUILD_ROOT, layer, version=ver, checksum=sm, base=bases[layer])
        out[layer] = {"interface_version": ver, "checksum": sm[:16] + "…"}
    manifest_state = None
    if layer_manifest is not None:
        manifest_state = layer_manifest.reconcile_toolchain_files(
            BUILD_ROOT,
            toolchain_files=LAYER_FILES["toolchain"],
        )
    ok = not (manifest_state or {}).get("errors")
    return {"ok": ok, "init_locks": out, "managed_target": shape,
            "layer_manifest": manifest_state}


def verify_self_locks() -> dict:
    """自洽检查:build-v3 的 02-mod/_locks 是否 == 当前 source_contract(不写)。
    用于门禁(verify_all):改了某层源却忘 --init-locks → mismatch,exit 1。"""
    try:
        shape = audit_managed_target(BUILD_ROOT, list(LAYERS))
    except (SurfaceProfileError, LayerManifestError, ManagedTargetError) as exc:
        return {
            "ok": False,
            "drift": [],
            "managed_target": {
                "status": "invalid",
                "kind": type(exc).__name__,
                "errors": list(getattr(exc, "errors", [str(exc)])),
            },
            "hint": "受管目标形状不安全；先修复 symlink/ownership，再验证 lock",
        }
    drift = []
    for layer in LAYERS:
        ver, sm = source_contract(layer)
        lock = _read_lock(BUILD_ROOT, layer) or {}
        lock_error = lock.get("_error")
        if lock_error or lock.get("interface_version") != ver or lock.get("checksum") != sm:
            drift.append({
                "layer": layer,
                "source": {"interface_version": ver, "checksum": sm[:16] + "…"},
                "lock": {"interface_version": lock.get("interface_version"),
                         "checksum": (lock.get("checksum") or "")[:16] + "…",
                         **({"error": lock_error} if lock_error else {})},
            })
    return {"ok": len(drift) == 0, "drift": drift, "managed_target": shape,
            "hint": "" if not drift else "源与 lock 不一致 — 跑 `skill_sync.py --init-locks` 重算"}


def _css_coupling(target_root: Path) -> "tuple[bool, str]":
    """core 联动判定:同步 core 时 css 该不该自动带上(闭环防「上游 bump 漏发下游」)。

    - 目标皮肤 tokens.css 含 `[data-mod]` 定制覆盖块 → **不**自动带(整层复制会覆盖定制;
      该 skill 拿新词表须回 build-v3 + 手动合并)。
    - 上游 css checksum == 目标 css.lock checksum → 已同版,无需带。
    - 其余(缺锁/不同版)→ 自动带 css。
    """
    if not _target_has_surface(target_root, "web"):
        return False, "目标 Surface 不含 web,不需要同步皮肤"
    tokens = target_root / "03-css-rules" / "01-tokens" / "tokens.css"
    if tokens.is_file() and "data-mod" in tokens.read_text(encoding="utf-8", errors="replace"):
        return False, "目标皮肤含 [data-mod] 定制块,不自动同步(防覆盖定制;新词表回 build-v3 后手动合并)"
    css_lock = _read_lock(target_root, "css")
    if css_lock and not css_lock.get("_error"):
        if css_lock.get("checksum") == _css_checksum(BUILD_ROOT):
            return False, "css 已与上游同版"
    return True, "上游 css 与目标不同版/缺锁,core 前端依赖皮肤词表"


def main() -> int:
    ap = argparse.ArgumentParser(description="Skill.Sync — 当前标准 core/ops/i18n/toolchain 复制替换(css 皮肤 opt-in)")
    ap.add_argument("--target", help="目标标准 skill 目录")
    ap.add_argument("--layers", default="core,ops,i18n,toolchain",
                    help="逗号分隔(默认 4 层 core/ops/i18n/toolchain)。css(皮肤)是 opt-in:"
                         "默认皮肤的工具加 `css` 一并拉皮肤(core 前端新增类时须跟上);定制皮肤勿同步")
    ap.add_argument("--apply", action="store_true", help="真复制 + bump lock(默认 dry-run)")
    ap.add_argument("--no-auto-bump", action="store_true", help="compat_bump 不自动改 lock")
    ap.add_argument("--init-locks", action="store_true", help="给 ZE Skill Build 自身写 lock 基线")
    ap.add_argument("--verify-locks", action="store_true", help="自洽检查 lock==源(不写;漂移 exit 1)")
    ap.add_argument("--no-coupled", action="store_true",
                    help="关闭 core 联动(默认:同步 core 时自动带 i18n,并按定制护栏自动带 css)")
    ap.add_argument("--receipt-out", metavar="PATH",
                    help="把三方 dry-run 冻结计划(含逐文件 add/update/delete/conflict/skip)写到该 JSON")
    ap.add_argument("--receipt", metavar="PATH",
                    help="apply 时消费一份冻结计划:apply 前二验 Target/Candidate 未再变才执行")
    ap.add_argument("--force-overwrite", action="store_true",
                    help="覆盖三方冲突(下游+上游双改);须配 --receipt 冻结计划,且仍记账")
    ap.add_argument("--log-upstream-gap", action="append", default=[], metavar="CODE:NOTE",
                    help="反哺通道(四步闸第 3 步):记一条 upstream gap 进 receipt.upstream_gaps[]"
                         "(格式 code:note;可重复;collect_upstream_gaps.py 聚合出反哺清单)")
    ap.add_argument("--gate-policy", metavar="PATH",
                    help="覆盖四步闸策略文件(默认 scripts/_sync_gate_policy.json;消融/测试用)")
    args = ap.parse_args()

    if args.force_overwrite and not args.receipt:
        ap.error("--force-overwrite 须配 --receipt <冻结计划>(先 --receipt-out 冻结复核)")

    upstream_gaps = []
    for raw_gap in args.log_upstream_gap:
        code, _, note = raw_gap.partition(":")
        if not code.strip() or not note.strip():
            ap.error("--log-upstream-gap 形如 code:note")
        upstream_gaps.append({"code": code.strip(), "note": note.strip(), "file": None})

    if args.init_locks:
        print(json.dumps(init_self_locks(), ensure_ascii=False, indent=2))
        return 0
    if args.verify_locks:
        res = verify_self_locks()
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1
    if not args.target:
        ap.error("需要 --target(或 --init-locks)")
    layers = [x.strip() for x in args.layers.split(",") if x.strip() in LAYER_DIRS]
    # core 联动闭环:上游 core bump 后漏带词表/皮肤是实证过的事故源(XSS 防御曾漏发下游)。
    # 同步 core 时:i18n 缺则自动补;css 按定制护栏判定(见 _css_coupling)。stderr 提示,stdout 保持纯 JSON。
    if "core" in layers and not args.no_coupled:
        coupled = []
        if "i18n" not in layers:
            layers.append("i18n")
            coupled.append("i18n(core 前端依赖词表 key)")
        if "css" not in layers:
            try:
                ok, why = _css_coupling(Path(args.target).resolve())
            except SurfaceProfileError:
                # ``sync`` below emits the structured hard failure. Do not
                # infer/fallback or perform any coupled write in this state.
                ok, why = False, "ZE_SURFACE.json 无效;同步预检将拒绝执行"
            if ok:
                layers.append("css")
                coupled.append(f"css({why})")
            else:
                print(f"[coupling] css 未自动带:{why}", file=sys.stderr)
        if coupled:
            print(f"[coupling] core 联动自动加层:{'; '.join(coupled)}(--no-coupled 关闭)", file=sys.stderr)

    frozen = None
    if args.receipt:
        if not args.apply:
            ap.error("--receipt 仅用于 --apply(dry-run 用 --receipt-out 冻结计划)")
        try:
            frozen = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": f"receipt 读取失败: {exc}"}, ensure_ascii=False, indent=2))
            return 1
        if not isinstance(frozen, dict) or frozen.get("schema") != SYNC_RECEIPT_SCHEMA:
            print(json.dumps({"ok": False, "error": f"receipt schema 必须是 {SYNC_RECEIPT_SCHEMA}"},
                             ensure_ascii=False, indent=2))
            return 1
        recorded_target = frozen.get("target")
        if isinstance(recorded_target, str) and recorded_target and (
                Path(recorded_target).resolve() != Path(args.target).resolve()):
            print(json.dumps({"ok": False, "error": "receipt.target 与 --target 不一致,拒绝消费错计划"},
                             ensure_ascii=False, indent=2))
            return 1

    res = sync(args.target, layers=layers, apply=args.apply, auto_bump=not args.no_auto_bump,
               force_overwrite=args.force_overwrite, receipt=frozen,
               upstream_gaps=upstream_gaps, gate_policy_path=args.gate_policy,
               # 冻结 receipt 时 dry-run 也跑等价基线(pre 侧入 receipt);平时 dry-run 不跑,保持秒级。
               run_equivalence=True if args.receipt_out else None)
    if args.receipt_out and isinstance(res.get("receipt"), dict):
        Path(args.receipt_out).write_text(
            json.dumps(res["receipt"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
