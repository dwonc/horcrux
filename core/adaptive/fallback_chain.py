"""
core/adaptive/fallback_chain.py — Phase 2: Advanced Fallback Chain

Phase 1의 minimum default fallback 위에 더 정교한 stage별 fallback chain 구축.

Fallback chain per stage:
  generator:    retry_once → use_partial_results → fallback_model
  synth:        use_best_candidate → retry_with_shorter_prompt
  core_critic:  retry_once → fallback_critic_model → flag_unresolved
  aux_critic:   skip_immediately
  revision:     keep_current_version → flag_blockers
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class FallbackAction(str, Enum):
    RETRY_ONCE = "retry_once"
    USE_PARTIAL_RESULTS = "use_partial_results"
    FALLBACK_MODEL = "fallback_model"
    USE_BEST_CANDIDATE = "use_best_candidate"
    RETRY_SHORTER_PROMPT = "retry_with_shorter_prompt"
    FALLBACK_CRITIC_MODEL = "fallback_critic_model"
    FLAG_UNRESOLVED = "flag_unresolved"
    SKIP_IMMEDIATELY = "skip_immediately"
    KEEP_CURRENT_VERSION = "keep_current_version"
    FLAG_BLOCKERS = "flag_blockers"


@dataclass
class FallbackResult:
    """fallback chain 실행 결과."""
    action_taken: FallbackAction
    success: bool
    result: Any = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "action_taken": self.action_taken.value,
            "success": self.success,
            "reason": self.reason,
        }


# Stage별 fallback chain 정의
FALLBACK_CHAINS: Dict[str, List[FallbackAction]] = {
    "generator": [
        FallbackAction.RETRY_ONCE,
        FallbackAction.USE_PARTIAL_RESULTS,
        FallbackAction.FALLBACK_MODEL,
    ],
    "pair_generation": [
        FallbackAction.USE_PARTIAL_RESULTS,
        FallbackAction.FALLBACK_MODEL,
    ],
    "synth": [
        FallbackAction.USE_BEST_CANDIDATE,
        FallbackAction.RETRY_SHORTER_PROMPT,
    ],
    "core_critic": [
        FallbackAction.RETRY_ONCE,
        FallbackAction.FALLBACK_CRITIC_MODEL,
        FallbackAction.FLAG_UNRESOLVED,
    ],
    "conditional_aux_critic": [
        FallbackAction.SKIP_IMMEDIATELY,
    ],
    "light_critic": [
        FallbackAction.SKIP_IMMEDIATELY,
    ],
    "revision": [
        FallbackAction.KEEP_CURRENT_VERSION,
        FallbackAction.FLAG_BLOCKERS,
    ],
    "revision_optional": [
        FallbackAction.SKIP_IMMEDIATELY,
    ],
}


@dataclass
class FallbackContext:
    """fallback 실행에 필요한 context."""
    stage_name: str
    partial_results: List[Any] = field(default_factory=list)
    current_solution: str = ""
    blocking_issues: List[str] = field(default_factory=list)
    retry_fn: Optional[Callable] = None
    fallback_model_fn: Optional[Callable] = None
    shorter_prompt_fn: Optional[Callable] = None


def execute_fallback_chain(ctx: FallbackContext) -> FallbackResult:
    """
    stage별 fallback chain을 순서대로 실행.
    첫 번째 성공하는 action에서 중단.
    """
    chain = FALLBACK_CHAINS.get(ctx.stage_name, [FallbackAction.FLAG_UNRESOLVED])

    for action in chain:
        result = _execute_single_fallback(action, ctx)
        if result.success:
            return result

    # chain 전체 실패
    return FallbackResult(
        action_taken=FallbackAction.FLAG_UNRESOLVED,
        success=False,
        reason=f"all fallback actions exhausted for {ctx.stage_name}",
    )


def _execute_single_fallback(action: FallbackAction, ctx: FallbackContext) -> FallbackResult:
    """단일 fallback action 실행."""

    if action == FallbackAction.RETRY_ONCE:
        if ctx.retry_fn:
            try:
                result = ctx.retry_fn()
                if result:
                    return FallbackResult(
                        action_taken=action, success=True,
                        result=result, reason="retry succeeded",
                    )
            except Exception as e:
                return FallbackResult(
                    action_taken=action, success=False,
                    reason=f"retry failed: {str(e)[:100]}",
                )
        return FallbackResult(action_taken=action, success=False, reason="no retry_fn")

    elif action == FallbackAction.USE_PARTIAL_RESULTS:
        if ctx.partial_results:
            # 가장 긴 partial result 사용
            best = max(ctx.partial_results, key=lambda x: len(str(x)))
            return FallbackResult(
                action_taken=action, success=True,
                result=best, reason=f"using best partial result ({len(str(best))} chars)",
            )
        return FallbackResult(action_taken=action, success=False, reason="no partial results")

    elif action == FallbackAction.FALLBACK_MODEL:
        if ctx.fallback_model_fn:
            try:
                result = ctx.fallback_model_fn()
                if result:
                    return FallbackResult(
                        action_taken=action, success=True,
                        result=result, reason="fallback model succeeded",
                    )
            except Exception as e:
                return FallbackResult(
                    action_taken=action, success=False,
                    reason=f"fallback model failed: {str(e)[:100]}",
                )
        return FallbackResult(action_taken=action, success=False, reason="no fallback_model_fn")

    elif action == FallbackAction.USE_BEST_CANDIDATE:
        if ctx.partial_results:
            best = max(ctx.partial_results, key=lambda x: len(str(x)))
            return FallbackResult(
                action_taken=action, success=True,
                result=best, reason="using best candidate as-is",
            )
        return FallbackResult(action_taken=action, success=False, reason="no candidates")

    elif action == FallbackAction.RETRY_SHORTER_PROMPT:
        if ctx.shorter_prompt_fn:
            try:
                result = ctx.shorter_prompt_fn()
                if result:
                    return FallbackResult(
                        action_taken=action, success=True,
                        result=result, reason="shorter prompt succeeded",
                    )
            except Exception:
                pass
        return FallbackResult(action_taken=action, success=False, reason="shorter prompt unavailable")

    elif action == FallbackAction.FALLBACK_CRITIC_MODEL:
        if ctx.fallback_model_fn:
            try:
                result = ctx.fallback_model_fn()
                if result:
                    return FallbackResult(
                        action_taken=action, success=True,
                        result=result, reason="fallback critic model succeeded",
                    )
            except Exception:
                pass
        return FallbackResult(action_taken=action, success=False, reason="no fallback critic")

    elif action == FallbackAction.SKIP_IMMEDIATELY:
        return FallbackResult(
            action_taken=action, success=True,
            result=None, reason="skipped (non-critical stage)",
        )

    elif action == FallbackAction.KEEP_CURRENT_VERSION:
        if ctx.current_solution:
            return FallbackResult(
                action_taken=action, success=True,
                result=ctx.current_solution,
                reason="keeping current version (revision failed)",
            )
        return FallbackResult(action_taken=action, success=False, reason="no current solution")

    elif action == FallbackAction.FLAG_BLOCKERS:
        return FallbackResult(
            action_taken=action, success=True,
            result={"unresolved_blockers": ctx.blocking_issues},
            reason=f"flagging {len(ctx.blocking_issues)} unresolved blockers",
        )

    elif action == FallbackAction.FLAG_UNRESOLVED:
        return FallbackResult(
            action_taken=action, success=False,
            reason="flagged as unresolved",
        )

    return FallbackResult(action_taken=action, success=False, reason=f"unknown action: {action}")
