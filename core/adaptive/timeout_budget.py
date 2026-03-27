"""
core/adaptive/timeout_budget.py — Stage별 Timeout Budget + Latency Logging

Phase 1 핵심: 느린 모델 하나가 전체 라운드를 붙잡지 못하도록 단계별 시간 예산 부여.
모든 stage 실행 결과는 jsonl로 기록하여 Phase 3 auto-tuning 기반 데이터를 축적.
"""

from __future__ import annotations

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import get_config


# ─── Stage 실행 상태 ───

class StageStatus:
    COMPLETED     = "completed"
    TIMED_OUT     = "timed_out"
    FALLBACK_USED = "fallback_used"
    SKIPPED       = "skipped"
    FAILED        = "failed"


@dataclass
class StageResult:
    """단일 task의 실행 결과."""
    task_id: str
    status: str
    result: Any = None
    latency_ms: int = 0
    error: Optional[str] = None


@dataclass
class TimeoutBudgetResult:
    """run_with_timeout_budget()의 반환값."""
    completed_results: List[StageResult]
    timed_out_results: List[str]
    fallback_used: bool
    fallback_action: str
    unresolved_flag: bool

    def to_dict(self) -> dict:
        return {
            "completed_count": len(self.completed_results),
            "timed_out_count": len(self.timed_out_results),
            "timed_out_ids": self.timed_out_results,
            "fallback_used": self.fallback_used,
            "fallback_action": self.fallback_action,
            "unresolved_flag": self.unresolved_flag,
        }


# ─── Default Fallback Actions ───

_DEFAULT_FALLBACK_ACTIONS: Dict[str, str] = {
    "generator":     "use_partial_results",
    "pair_generation": "use_partial_results",
    "synth":         "use_best_candidate_as_is",
    "light_critic":  "skip",
    "core_critic":   "retry_once_then_flag_unresolved",
    "conditional_aux_critic": "skip",
    "revision":      "keep_current_version_flag_blockers",
    "revision_optional": "skip",
    "convergence_check": "skip",
    "finalize":      "use_partial_results",
}


# ─── Session ID 생성 ───

def generate_session_id() -> str:
    """요청 진입 시점에 UUID v4로 세션 ID 생성."""
    return str(uuid.uuid4())


# ─── Latency Logging ───

_log_lock = None

def _get_log_lock():
    global _log_lock
    if _log_lock is None:
        import threading
        _log_lock = threading.Lock()
    return _log_lock


def log_stage_latency(
    session_id: str,
    stage_name: str,
    model: str,
    latency_ms: int,
    status: str,
    mode: str,
    timeout_budget_ms: int,
) -> None:
    """
    Stage latency를 jsonl 파일에 기록.
    Phase 3 auto-tuning을 위한 구조화된 로그.
    """
    cfg = get_config()
    log_path = cfg.logging.log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "stage_name": stage_name,
        "model": model,
        "latency_ms": latency_ms,
        "status": status,
        "mode": mode,
        "timeout_budget_ms": timeout_budget_ms,
    }

    lock = _get_log_lock()
    with lock:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            # 로깅 실패는 메인 흐름을 중단하지 않음
            print(f"[log_stage_latency] write failed: {e}")


# ─── 메인: Timeout Budget 실행 ───

_executor = ThreadPoolExecutor(max_workers=6)


def run_with_timeout_budget(
    stage_name: str,
    tasks: List[Tuple[str, Callable[[], Any]]],
    timeout_budget_ms: int,
    mode: str,
    session_id: str,
    retry_on_failure: bool = False,
) -> TimeoutBudgetResult:
    """
    Stage별 timeout을 적용하고 partial result/fallback을 처리한다.

    Args:
        stage_name: "generator", "core_critic" 등
        tasks: [(task_id, callable), ...] 형태의 실행 대상
        timeout_budget_ms: 이 stage에 허용된 최대 시간 (ms)
        mode: "fast" | "standard" | "full_horcrux"
        session_id: 현재 세션 ID
        retry_on_failure: True면 실패한 task를 1회 재시도 (core_critic용)

    Returns:
        TimeoutBudgetResult
    """
    timeout_sec = timeout_budget_ms / 1000.0
    fallback_action = _DEFAULT_FALLBACK_ACTIONS.get(stage_name, "skip")

    completed: List[StageResult] = []
    timed_out: List[str] = []
    any_fallback = False
    unresolved = False

    # 병렬 실행
    futures = {}
    for task_id, task_fn in tasks:
        future = _executor.submit(task_fn)
        futures[future] = task_id

    # 결과 수집
    import concurrent.futures
    done, not_done = concurrent.futures.wait(
        futures.keys(),
        timeout=timeout_sec,
    )

    # 완료된 것들
    for future in done:
        task_id = futures[future]
        t0 = time.monotonic()
        try:
            result = future.result(timeout=0.1)
            latency = getattr(result, "latency_ms", 0) if hasattr(result, "latency_ms") else 0
            completed.append(StageResult(
                task_id=task_id,
                status=StageStatus.COMPLETED,
                result=result,
                latency_ms=latency,
            ))
            log_stage_latency(
                session_id=session_id,
                stage_name=stage_name,
                model=task_id,
                latency_ms=latency,
                status=StageStatus.COMPLETED,
                mode=mode,
                timeout_budget_ms=timeout_budget_ms,
            )
        except Exception as e:
            completed.append(StageResult(
                task_id=task_id,
                status=StageStatus.FAILED,
                error=str(e)[:300],
            ))
            log_stage_latency(
                session_id=session_id,
                stage_name=stage_name,
                model=task_id,
                latency_ms=0,
                status=StageStatus.FAILED,
                mode=mode,
                timeout_budget_ms=timeout_budget_ms,
            )

    # Timeout된 것들
    for future in not_done:
        task_id = futures[future]
        future.cancel()
        timed_out.append(task_id)
        log_stage_latency(
            session_id=session_id,
            stage_name=stage_name,
            model=task_id,
            latency_ms=timeout_budget_ms,
            status=StageStatus.TIMED_OUT,
            mode=mode,
            timeout_budget_ms=timeout_budget_ms,
        )

    # ── Retry 로직 (core_critic용) ──
    if retry_on_failure and timed_out:
        retry_id = timed_out[0]
        retry_task = None
        for tid, tfn in tasks:
            if tid == retry_id:
                retry_task = tfn
                break

        if retry_task:
            print(f"  [timeout_budget] retrying {retry_id} (1/1)...")
            try:
                future = _executor.submit(retry_task)
                result = future.result(timeout=timeout_sec * 0.5)  # 재시도는 절반 시간
                latency = getattr(result, "latency_ms", 0) if hasattr(result, "latency_ms") else 0
                completed.append(StageResult(
                    task_id=retry_id,
                    status=StageStatus.COMPLETED,
                    result=result,
                    latency_ms=latency,
                ))
                timed_out.remove(retry_id)
                log_stage_latency(
                    session_id=session_id,
                    stage_name=stage_name,
                    model=retry_id,
                    latency_ms=latency,
                    status="retry_completed",
                    mode=mode,
                    timeout_budget_ms=timeout_budget_ms,
                )
            except Exception:
                unresolved = True
                any_fallback = True
                log_stage_latency(
                    session_id=session_id,
                    stage_name=stage_name,
                    model=retry_id,
                    latency_ms=int(timeout_sec * 500),
                    status="retry_failed",
                    mode=mode,
                    timeout_budget_ms=timeout_budget_ms,
                )

    # ── Fallback 판정 ──
    if timed_out:
        any_fallback = True
        # core_critic 재시도 후에도 실패하면 unresolved
        if stage_name in ("core_critic",) and retry_on_failure:
            unresolved = True

    # 모든 task가 실패한 경우
    successful = [r for r in completed if r.status == StageStatus.COMPLETED]
    if not successful and timed_out:
        any_fallback = True
        unresolved = True

    return TimeoutBudgetResult(
        completed_results=completed,
        timed_out_results=timed_out,
        fallback_used=any_fallback,
        fallback_action=fallback_action if any_fallback else "none",
        unresolved_flag=unresolved,
    )
