"""机检 / machine verification report — pure verdict logic (V8).

The four checks (标注残留 OCR / 框外过改帧差 / 接缝 SSIM / 时长帧率漂移) each yield a
green|yellow|red sub-verdict; the overall verdict is the worst of them. Policy
(confirmation #6): yellow = flag-not-block (the candidate system already backs
the human up); red (duration/fps drift, conversion failure) = adoption forbidden.

Running the checks needs media (ffmpeg/OCR) and lives in the hosting skill's
``candidate.verify`` stage, which degrades gracefully — an unavailable check
records ``skipped`` (treated as green for the verdict, surfaced in detail). This
module holds only the pure builders + summariser so it is fully unit-testable.
"""

from __future__ import annotations

from typing import Any, Mapping

# check names (stable ids)
CHECK_ANNOTATION_RESIDUE = "annotation_residue"   # OCR + template match on annotations
CHECK_OUT_OF_BBOX = "out_of_bbox_change"          # frame diff outside the annotation bbox
CHECK_SEAM = "seam_jump"                          # SSIM / frame diff at the repaste seam
CHECK_DURATION_DRIFT = "duration_drift"           # |cand - segment| > 1 frame, or fps mismatch

ALL_CHECKS = (CHECK_ANNOTATION_RESIDUE, CHECK_OUT_OF_BBOX, CHECK_SEAM, CHECK_DURATION_DRIFT)

_ORDER = {"green": 0, "skipped": 0, "yellow": 1, "red": 2}

# which checks may raise a RED (block adoption). Only hard integrity failures.
_RED_ALLOWED = {CHECK_DURATION_DRIFT}


def make_check(name: str, verdict: str, detail: str = "", **metrics: Any) -> dict[str, Any]:
    """Build one check result. Non-integrity checks are capped at yellow."""
    if verdict == "red" and name not in _RED_ALLOWED:
        verdict = "yellow"  # policy: only integrity checks may block
    if verdict not in ("green", "yellow", "red", "skipped"):
        raise ValueError(f"bad verdict {verdict!r}")
    return {"name": name, "verdict": verdict, "detail": detail, "metrics": dict(metrics)}


def summarize(checks: Mapping[str, Mapping[str, Any]]) -> str:
    """Overall verdict = worst sub-verdict (red > yellow > green/skipped)."""
    worst = "green"
    for check in checks.values():
        v = check.get("verdict", "green")
        if _ORDER.get(v, 0) > _ORDER.get(worst, 0):
            worst = v
    return "red" if worst == "red" else ("yellow" if worst == "yellow" else "green")


def adoption_allowed(verdict: str) -> bool:
    """Red forbids adoption; yellow/green allow it (human decides on yellow)."""
    return verdict != "red"


def build_report(candidate_id: str, checks: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Assemble a verify_report payload (checks keyed by name + overall verdict)."""
    keyed = {c["name"]: c for c in checks}
    verdict = summarize(keyed)
    return {
        "candidateId": candidate_id,
        "checks": keyed,
        "verdict": verdict,
        "adoptionAllowed": adoption_allowed(verdict),
    }
