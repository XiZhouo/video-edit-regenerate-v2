"""AdvisoryLock — UI 编辑层互斥锁 (软锁).

完整 SPEC 见同目录 SPEC.md §3.

已实装 (无跨叶子依赖):
  - LockHandle: lock_id / entity / holder / acquired_at / expires_at (frozen)
  - AdvisoryLockManager: acquire / release / state / sweep_expired
  - TTL 过期自动清理 (state / acquire 时顺手清)
  - force 强制接管 (管理员场景; 审计由 caller 负责)
  - 同 user+device 续期 (不算冲突)
"""

from __future__ import annotations

import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

# 共享 ZeError: 从 02-entity 兄弟目录 import
_ENTITY_DIR = Path(__file__).resolve().parent.parent.parent / "01-data-structure" / "02-entity"
if str(_ENTITY_DIR) not in sys.path:
    sys.path.insert(0, str(_ENTITY_DIR))

from entity import ZeError  # noqa: E402


# ==============================================================
# LockHandle
# ==============================================================

@dataclass(frozen=True)
class LockHandle:
    """Lock 句柄. release 时通过 lock_id 校验."""
    lock_id: str
    entity_type: str
    entity_id: str
    holder_user: str
    holder_device: str
    acquired_at: float
    expires_at: float


# ==============================================================
# AdvisoryLockManager
# ==============================================================

class AdvisoryLockManager:
    """单 server 内存版编辑锁. TTL 过期 + force 接管.

    注意: 这是 advisory (软) 锁 — UI 层用; 真数据保护靠 entity.update 的 CAS.
    """

    def __init__(self, ttl_sec: float = 300.0) -> None:
        if ttl_sec <= 0:
            raise ZeError(
                "validation_failed",
                f"ttl_sec must be positive, got {ttl_sec}",
                context={"ttl_sec": ttl_sec},
            )
        self.ttl_sec = ttl_sec
        self._locks: dict[tuple[str, str], LockHandle] = {}

    def acquire(
        self,
        *,
        entity_type: str,
        entity_id: str,
        user_id: str,
        device_id: str = "default",
        force: bool = False,
    ) -> LockHandle:
        """拿锁. 已被别人锁抛 acl_denied; 自己已锁=续期; force=True 强制接管."""
        key = (entity_type, entity_id)
        now = time.time()
        existing = self._locks.get(key)

        # 过期清理
        if existing is not None and existing.expires_at <= now:
            del self._locks[key]
            existing = None

        # 别人持有 + 未 force → 抛错
        if existing is not None:
            is_self = (existing.holder_user == user_id
                       and existing.holder_device == device_id)
            if not is_self and not force:
                raise ZeError(
                    "acl_denied",
                    f"entity {entity_type}/{entity_id} locked by {existing.holder_user!r}",
                    context={
                        "entity_type": entity_type,
                        "entity_id": entity_id,
                        "locked_by_user": existing.holder_user,
                        "locked_by_device": existing.holder_device,
                        "acquired_at": existing.acquired_at,
                        "expires_at": existing.expires_at,
                    },
                )

        # 拿锁 (新 handle, 即使是自己续期也用新 lock_id)
        handle = LockHandle(
            lock_id=uuid.uuid4().hex,
            entity_type=entity_type,
            entity_id=entity_id,
            holder_user=user_id,
            holder_device=device_id,
            acquired_at=now,
            expires_at=now + self.ttl_sec,
        )
        self._locks[key] = handle
        return handle

    def release(self, handle: LockHandle) -> bool:
        """放锁. lock_id 校验; 不是自己的锁返 False (idempotent)."""
        key = (handle.entity_type, handle.entity_id)
        cur = self._locks.get(key)
        if cur is not None and cur.lock_id == handle.lock_id:
            del self._locks[key]
            return True
        return False

    def state(self, entity_type: str, entity_id: str) -> LockHandle | None:
        """查锁状态; 过期自动清; 返 None 表示无锁."""
        key = (entity_type, entity_id)
        lock = self._locks.get(key)
        if lock is None:
            return None
        if lock.expires_at <= time.time():
            del self._locks[key]
            return None
        return lock

    def sweep_expired(self) -> int:
        """主动清过期锁; 返回清了几个."""
        now = time.time()
        before = len(self._locks)
        self._locks = {k: v for k, v in self._locks.items() if v.expires_at > now}
        return before - len(self._locks)

    def __len__(self) -> int:
        return len(self._locks)
