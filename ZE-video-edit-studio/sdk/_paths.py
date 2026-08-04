"""sdk/_paths.py — 把各 core 叶子目录幂等注入 sys.path.

ZE v3 的 01-core 叶子是平铺布局: 每个叶子一个目录, 叶子之间靠 sys.path 注入
互相 import (叶子自身已幂等自注入其直接依赖的兄弟). 本模块是 sdk 聚合层的地基:
import sdk 时先 import 本模块 (side-effect), 把各「入口叶子」目录加进 sys.path,
之后 `from <leaf_module> import X` 即可; 传递依赖由叶子自注入解决.

幂等: 每次 insert 前查 `not in sys.path`, 重复 import 安全.
注: 只注入有 .py 源的叶子。叶子 basename 必须避让 stdlib —— 平铺注入把叶子目录加进 sys.path[0],
    叶子模块名会遮蔽同名 stdlib。concurrency 的队列模块故意叫 `task_queue.py`(不叫 queue.py):
    否则 `import sdk` 后任何 `import queue` / 惰性 `from queue import ...` 会命中叶子、遮蔽
    stdlib queue,令 ThreadPoolExecutor 等随机炸(曾坑 telemetry/anyio,见各处排雷注释)。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent      # ZE-skill-build/
CORE = ROOT / "01-core"

# 入口叶子目录 (相对 CORE). 顺序无关 (insert(0) 但 basename 不冲突).
_LEAF_DIRS = (
    "01-data-structure/02-entity",          # ZeError / Entity / Actor (最底层, 多数叶子依赖)
    "01-data-structure/01-document",        # Document / DocumentRegistry
    "01-data-structure/04-field-types",     # make_validator / validate
    "01-data-structure/03-asset",           # AssetStore
    "01-data-structure/05-capacity-limit",  # CapacityChecker
    "01-data-structure/06-document-index",  # DocumentIndex / DocumentIndexEntry
    "01-data-structure/07-type-library",    # zt 组合 API(fragments / type_library)
    "03-commands-operations/02-operation",  # Patch / Operation / new_operation / OpLog
    "03-commands-operations/01-command",    # CommandRegistry / CommandSpec / register_builtin_commands
    "03-commands-operations/03-pipeline",   # PipelineRunner / StageRunner
    "03-commands-operations/05-ai-coworker",  # AiGovernor / GuardDecision (AI 同事治理)
    "03-commands-operations/06-candidate",  # CandidateRecord / adopt-reject operations
    "02-data-flow/01-realtime-sync",        # RealtimeBroker
    "02-data-flow/04-history-rollback",     # UndoController / rollback_field / rollback_entity_to_op
    "02-data-flow/05-concurrency",          # AdvisoryLockManager / QueueManager / RateLimiter
    "02-data-flow/06-resource-budget",      # ResourceContract / GateRegistry
    "02-data-flow/07-persistence",          # CommitCoordinator / CommitStore / FileCommitStore
    "04-user-facing/03-authentication",     # UserManager / SessionManager
    "_protocol",                            # change_set: affected_entities / to_broadcast_payload
)


# 跨层: 04-ops 配置层 (sdk barrel re-export ConfigRegistry). ops 是 core 的下游叶子
# (和 css 同角色, 加性单向无环); 注入其 config-management 目录以便 `from registry import ...`.
_OPS_DIRS = (
    "04-ops/01-config-management",           # config_schema / config / registry (ConfigRegistry)
)


def inject() -> None:
    """把所有入口叶子目录幂等加进 sys.path. import sdk 时自动调一次."""
    # ROOT 也注入: importlib 按路径载入的 mod 代码里 `from sdk import ...` 在任意 cwd 都成立.
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    for rel in _LEAF_DIRS:
        d = str((CORE / rel).resolve())
        if d not in sys.path:
            sys.path.insert(0, d)
    for rel in _OPS_DIRS:
        d = str((ROOT / rel).resolve())
        if d not in sys.path:
            sys.path.insert(0, d)


inject()
