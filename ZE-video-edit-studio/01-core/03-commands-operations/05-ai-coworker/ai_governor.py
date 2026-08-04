"""05-ai-coworker — AI 同事治理(组合既有原语,非新机制)。

不造 v2 那样的 ai_safety/ai_cost/approval 四个专用模块,而是把 source='ai' 的命令治理
拆成 v3 已有正交原语的**组合**,在 dispatch 链上一处接起来:

  - provenance(GATE-AI-01):source='ai' 的写入必须带非空溯源(args 里 provenance/ai_provenance),
    缺则 raise `ai_provenance_required`。
  - safety filter(GATE-AI-04):moderator 钩(pluggable,默认放行)过 args 文本,拦截则 raise
    `ai_safety_blocked`。moderator 由项目/mod 经 config 注入,core 只给挂点。
  - cost cap(GATE-AI-06):单次估算 cost(args['cost_usd'] 优先,否则 spec.ai_cost_usd)超
    per-call 上限则 raise `ai_cost_exceeded`。累计/日预算需持久 ledger,留后续(本层无状态)。
  - approval(GATE-AI-05):估算 cost ≥ approval 阈值 或 spec.ai_high_risk → 要求两段式确认
    (返回 require_confirm=True;dispatch 据此复用既有 PendingConfirmation 门控,零新机制)。

机制在 core(本文件),阈值/moderator/预算是 policy → 由 04-ops config 注入,core 保持零业务。
governor 是 opt-in:CommandRegistry 不挂 governor 时 dispatch 行为完全不变(向后兼容)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from entity import ZeError  # leaf import(_paths 注入)

# moderator 钩:输入待审文本 → True=放行 / False=拦截。默认 None=不审。
Moderator = Callable[[str], bool]


@dataclass(frozen=True)
class GuardDecision:
    """治理结论。require_confirm=True → dispatch 走两段式确认(人审后才执行)。"""
    require_confirm: bool = False


class AiGovernor:
    """source='ai' 命令的治理器(无状态;阈值/moderator 注入)。

    per_call_cap_usd / approval_threshold_usd:None=该项关闭。
    moderator:None=不做安全过滤。require_provenance:source='ai' 是否强制带溯源。
    """

    def __init__(
        self,
        *,
        per_call_cap_usd: float | None = None,
        approval_threshold_usd: float | None = None,
        moderator: Moderator | None = None,
        require_provenance: bool = True,
    ) -> None:
        self._per_call_cap = per_call_cap_usd
        self._approval_threshold = approval_threshold_usd
        self._moderator = moderator
        self._require_provenance = require_provenance

    @staticmethod
    def _text_of(args: dict[str, Any]) -> str:
        """收集 args 里的字符串叶子(浅层 + 一层 dict/list)供 moderation。"""
        out: list[str] = []

        def walk(v: Any, depth: int) -> None:
            if isinstance(v, str):
                out.append(v)
            elif depth < 2 and isinstance(v, dict):
                for x in v.values():
                    walk(x, depth + 1)
            elif depth < 2 and isinstance(v, (list, tuple)):
                for x in v:
                    walk(x, depth + 1)

        walk(args, 0)
        return "\n".join(out)

    @staticmethod
    def _has_provenance(args: dict[str, Any]) -> bool:
        for k in ("provenance", "ai_provenance"):
            v = args.get(k)
            if v:  # 非空 dict / 对象
                return True
        return False

    def estimate_cost(self, spec: Any, args: dict[str, Any]) -> float:
        """估算单次成本。args['cost_usd'] 优先(调用方实测/预估),否则 spec.ai_cost_usd 声明值。"""
        v = args.get("cost_usd")
        if isinstance(v, (int, float)) and v >= 0:
            return float(v)
        return float(getattr(spec, "ai_cost_usd", 0.0) or 0.0)

    def guard(self, spec: Any, args: dict[str, Any], actor: Any) -> GuardDecision:
        """对一条 source='ai' 命令做治理。通过返回 GuardDecision;违规 raise ZeError。"""
        cmd_id = getattr(spec, "id", "?")

        # provenance(AI-01)
        if self._require_provenance and not self._has_provenance(args):
            raise ZeError(
                "ai_provenance_required",
                f"command {cmd_id!r} from source='ai' requires non-empty provenance "
                f"(args.provenance / args.ai_provenance)",
                context={"command_id": cmd_id},
            )

        # safety filter(AI-04)
        if self._moderator is not None:
            text = self._text_of(args)
            if not self._moderator(text):
                raise ZeError(
                    "ai_safety_blocked",
                    f"command {cmd_id!r} blocked by safety moderator",
                    context={"command_id": cmd_id},
                )

        # cost cap(AI-06,单次)
        est = self.estimate_cost(spec, args)
        if self._per_call_cap is not None and est > self._per_call_cap:
            raise ZeError(
                "ai_cost_exceeded",
                f"command {cmd_id!r} est cost {est} > per-call cap {self._per_call_cap}",
                context={"command_id": cmd_id, "est_usd": est, "cap_usd": self._per_call_cap},
            )

        # approval(AI-05):高成本或高风险 → 走两段式确认
        require_confirm = bool(getattr(spec, "ai_high_risk", False)) or (
            self._approval_threshold is not None and est >= self._approval_threshold
        )
        return GuardDecision(require_confirm=require_confirm)
