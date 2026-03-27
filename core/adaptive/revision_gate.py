"""
core/adaptive/revision_gate.py — Revision Loop Hard Cap + 중단 판정

Phase 1: revision loop를 최대 2회로 제한하고,
무의미한 반복(같은 blocker, progress 없음)을 감지하여 조기 중단.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .config import get_config


@dataclass
class RevisionDecision:
    """should_continue_revision()의 반환값."""
    should_continue: bool
    reason: str
    current_round: int

    def to_dict(self) -> dict:
        return {
            "should_continue": self.should_continue,
            "reason": self.reason,
            "current_round": self.current_round,
        }


def should_continue_revision(
    current_round: int,
    converged: bool = False,
    blocking_issue_count: int = 0,
    progress_delta: float = 0.0,
    timeout_budget_remaining_ms: int = -1,
    same_blockers_repeated: bool = False,
    previous_blockers: Optional[List[str]] = None,
    current_blockers: Optional[List[str]] = None,
) -> RevisionDecision:
    """
    Revision을 계속할지 판정한다.

    Stop conditions (어느 하나라도 해당되면 중단):
      1. current_round >= hard_cap (default 2)
      2. converged == True
      3. same_blockers_repeated == True
      4. progress_delta <= min_progress_delta
      5. timeout_budget_remaining <= 0

    Args:
        current_round: 현재 revision 라운드 (1-based)
        converged: 수렴 판정 여부
        blocking_issue_count: 현재 미해결 blocker 수
        progress_delta: 이전 라운드 대비 개선 정도 (0.0 ~ 1.0)
        timeout_budget_remaining_ms: 남은 timeout 예산 (ms). -1이면 무제한.
        same_blockers_repeated: 이전과 같은 blocker가 반복되는지
        previous_blockers: 이전 라운드 blocker 목록 (자동 비교용)
        current_blockers: 현재 라운드 blocker 목록 (자동 비교용)

    Returns:
        RevisionDecision
    """
    cfg = get_config()
    hard_cap = cfg.revision.hard_cap
    min_delta = cfg.revision.min_progress_delta

    # ── 자동 blocker 비교 ──
    if not same_blockers_repeated and previous_blockers and current_blockers:
        # 이전 blocker가 현재에 모두 포함되면 반복으로 판단
        prev_set = set(previous_blockers)
        curr_set = set(current_blockers)
        if prev_set and prev_set <= curr_set:
            same_blockers_repeated = True

    # ── Stop condition 체크 (우선순위 순) ──

    # 1. 수렴
    if converged:
        return RevisionDecision(
            should_continue=False,
            reason="converged — revision 불필요",
            current_round=current_round,
        )

    # 2. Hard cap
    if current_round >= hard_cap:
        return RevisionDecision(
            should_continue=False,
            reason=f"hard cap 도달: round {current_round} >= {hard_cap}",
            current_round=current_round,
        )

    # 3. Same blockers repeated
    if same_blockers_repeated:
        return RevisionDecision(
            should_continue=False,
            reason=f"같은 blocker 반복 — 추가 revision으로 해결 불가 판단",
            current_round=current_round,
        )

    # 4. Progress delta too small
    if current_round > 1 and progress_delta <= min_delta:
        return RevisionDecision(
            should_continue=False,
            reason=f"progress_delta={progress_delta:.3f} <= {min_delta} — 개선 미미",
            current_round=current_round,
        )

    # 5. Timeout budget exhausted
    if timeout_budget_remaining_ms == 0:
        return RevisionDecision(
            should_continue=False,
            reason="timeout budget exhausted",
            current_round=current_round,
        )

    # ── 계속 진행 ──
    return RevisionDecision(
        should_continue=True,
        reason=f"round {current_round}/{hard_cap}, blockers={blocking_issue_count}, delta={progress_delta:.3f}",
        current_round=current_round,
    )
