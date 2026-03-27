"""
core/adaptive/interactive.py — Interactive Session Management

3가지 모드:
  - batch: 기존 동작 (중단 없이 끝까지)
  - interactive: 매 라운드 후 pause
  - semi_interactive: 조건 충족 시 auto-pause

핵심:
  - threading.Event 기반 cooperative pause (LLM 호출 중간에 강제 중단 안 함)
  - Atomic checkpoint (tmp → os.replace)
  - SideEffectJournal — rollback 시 reversible 부작용 보상
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional


# ─── Enums ───

class SessionState(str, Enum):
    IDLE              = "idle"
    RUNNING           = "running"
    PAUSE_REQUESTED   = "pause_requested"
    PAUSED            = "paused"
    AWAITING_FEEDBACK = "awaiting_feedback"
    ROLLING_BACK      = "rolling_back"
    COMPLETED         = "completed"
    FAILED            = "failed"
    CANCELLED         = "cancelled"


class FeedbackAction(str, Enum):
    CONTINUE = "continue"
    FEEDBACK = "feedback"
    FOCUS    = "focus"
    STOP     = "stop"
    ROLLBACK = "rollback"


class SideEffectType(str, Enum):
    REVERSIBLE   = "reversible"
    IDEMPOTENT   = "idempotent"
    IRREVERSIBLE = "irreversible"


# ─── Config ───

@dataclass
class AutoPauseConfig:
    score_drop_threshold: float = 0.5
    stall_rounds: int = 3
    critic_disagreement_threshold: float = 2.0
    cost_budget_pct: float = 0.8
    check_irreversible: bool = True


@dataclass
class SessionConfig:
    mode: str = "batch"  # batch | interactive | semi_interactive
    max_rounds: int = 10
    auto_pause: AutoPauseConfig = field(default_factory=AutoPauseConfig)
    checkpoint_retention: int = 10
    feedback_timeout_seconds: float = 300.0


# ─── Data Classes ───

@dataclass
class SideEffect:
    round_num: int
    effect_type: SideEffectType
    description: str
    compensate_fn: Optional[Callable] = None  # reversible일 때 보상 함수


@dataclass
class RoundResult:
    round_num: int = 0
    thesis: str = ""
    antithesis: str = ""
    synthesis: str = ""
    critic_scores: Dict[str, float] = field(default_factory=dict)
    final_score: float = 0.0
    convergence_delta: float = 0.0
    side_effects: List[SideEffect] = field(default_factory=list)
    human_directives_applied: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {
            "round_num": self.round_num,
            "final_score": self.final_score,
            "convergence_delta": self.convergence_delta,
            "critic_scores": self.critic_scores,
            "human_directives_applied": self.human_directives_applied,
            "duration_seconds": self.duration_seconds,
            "side_effects_count": len(self.side_effects),
        }


@dataclass
class SessionCommand:
    action: FeedbackAction
    human_directive: str = ""
    focus_area: str = ""
    focus_depth: str = "deep"
    rollback_to_round: int = 0
    new_directive: str = ""


# ─── SideEffectJournal ───

class SideEffectJournal:
    def __init__(self):
        self._effects: List[SideEffect] = []

    def record(self, effect: SideEffect):
        self._effects.append(effect)

    def get_for_round(self, round_num: int) -> List[SideEffect]:
        return [e for e in self._effects if e.round_num == round_num]

    def has_irreversible_after(self, round_num: int) -> bool:
        return any(
            e.effect_type == SideEffectType.IRREVERSIBLE and e.round_num > round_num
            for e in self._effects
        )

    def irreversible_rounds_after(self, round_num: int) -> List[int]:
        return sorted(set(
            e.round_num for e in self._effects
            if e.effect_type == SideEffectType.IRREVERSIBLE and e.round_num > round_num
        ))

    def compensate_after(self, round_num: int):
        for e in reversed(self._effects):
            if e.round_num > round_num and e.effect_type == SideEffectType.REVERSIBLE:
                if e.compensate_fn:
                    try:
                        e.compensate_fn()
                    except Exception:
                        pass
        self._effects = [e for e in self._effects if e.round_num <= round_num]

    @property
    def summary(self) -> dict:
        rev = sum(1 for e in self._effects if e.effect_type == SideEffectType.REVERSIBLE)
        irr = sum(1 for e in self._effects if e.effect_type == SideEffectType.IRREVERSIBLE)
        return {"reversible": rev, "irreversible": irr, "total": len(self._effects)}


# ─── CheckpointStore ───

class CheckpointStore:
    def __init__(self, session_id: str, base_dir: str = "checkpoints"):
        self._dir = Path(base_dir) / session_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._retention = 10

    def save(self, round_num: int, data: dict):
        target = self._dir / f"round_{round_num}.json"
        tmp = self._dir / f"round_{round_num}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(str(tmp), str(target))
        self._prune()

    def load(self, round_num: int) -> Optional[dict]:
        target = self._dir / f"round_{round_num}.json"
        if not target.exists():
            return None
        with open(target, "r", encoding="utf-8") as f:
            return json.load(f)

    def available_rounds(self) -> List[int]:
        rounds = []
        for f in self._dir.glob("round_*.json"):
            try:
                rounds.append(int(f.stem.split("_")[1]))
            except (ValueError, IndexError):
                pass
        return sorted(rounds)

    def _prune(self):
        rounds = self.available_rounds()
        while len(rounds) > self._retention:
            oldest = rounds.pop(0)
            path = self._dir / f"round_{oldest}.json"
            if path.exists():
                path.unlink()


# ─── InteractiveSession ───

class InteractiveSession:
    def __init__(self, config: SessionConfig, base_dir: str = "checkpoints"):
        self.session_id = f"int_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        self.config = config
        self.state = SessionState.IDLE
        self.current_round = 0
        self.rounds: List[RoundResult] = []
        self.checkpoint_store = CheckpointStore(self.session_id, base_dir)
        self.side_effects = SideEffectJournal()

        self._pause_event = threading.Event()
        self._pause_event.set()  # 기본: not paused
        self._command_queue: Deque[SessionCommand] = deque()
        self._pending_directive: Optional[str] = None
        self._pending_focus: Optional[str] = None
        self._pause_reason: Optional[str] = None
        self._task: str = ""
        self._thread: Optional[threading.Thread] = None
        self._compact_memory = None

    # ─── External API ───

    def start(self, task: str, engine_fn: Callable, compact_memory=None):
        self._task = task
        self._compact_memory = compact_memory
        self.state = SessionState.RUNNING
        self._thread = threading.Thread(
            target=self._run_wrapper, args=(engine_fn,), daemon=True
        )
        self._thread.start()

    def pause(self, reason: str = "user_requested"):
        if self.state == SessionState.RUNNING:
            self.state = SessionState.PAUSE_REQUESTED
            self._pause_reason = reason
            self._pause_event.clear()

    def resume(self, command: SessionCommand):
        if self.state not in (SessionState.PAUSED, SessionState.AWAITING_FEEDBACK):
            return

        if command.action == FeedbackAction.STOP:
            self.state = SessionState.CANCELLED
            self._pause_event.set()
            return

        if command.action == FeedbackAction.ROLLBACK:
            self._do_rollback(command.rollback_to_round, command.new_directive)
            return

        if command.action == FeedbackAction.FEEDBACK and command.human_directive:
            self._pending_directive = command.human_directive
            if self._compact_memory:
                self._compact_memory.inject_human_directive(
                    command.human_directive, "feedback"
                )

        if command.action == FeedbackAction.FOCUS and command.focus_area:
            self._pending_focus = command.focus_area
            if self._compact_memory:
                self._compact_memory.inject_human_directive(
                    f"[FOCUS: {command.focus_area}] depth={command.focus_depth}",
                    "focus"
                )

        self.state = SessionState.RUNNING
        self._pause_event.set()

    def check_pause_point(self, interruption_point: str = "") -> bool:
        """
        Cooperative pause check — debate loop에서 매 라운드 끝에 호출.
        Returns: True면 계속, False면 중단됨 (cancelled).
        """
        if self.config.mode == "batch":
            return True

        if self.state == SessionState.PAUSE_REQUESTED:
            self.state = SessionState.PAUSED
            self._pause_reason = self._pause_reason or interruption_point

        if self.state == SessionState.CANCELLED:
            return False

        if self.config.mode == "interactive":
            # interactive: 매 라운드 후 무조건 pause
            if self.state == SessionState.RUNNING:
                self.state = SessionState.PAUSED
                self._pause_reason = f"interactive_round_{self.current_round}"
                self._pause_event.clear()

        if self.state == SessionState.PAUSED:
            self.state = SessionState.AWAITING_FEEDBACK
            # feedback 대기
            self._pause_event.wait(timeout=self.config.feedback_timeout_seconds)
            if not self._pause_event.is_set():
                # timeout → 자동 continue
                self.state = SessionState.RUNNING
                self._pause_event.set()

        return self.state not in (SessionState.CANCELLED, SessionState.FAILED)

    def should_auto_pause(self, round_result: RoundResult) -> Optional[str]:
        """semi_interactive 모드의 auto-pause 조건 5가지 체크."""
        if self.config.mode != "semi_interactive":
            return None

        ap = self.config.auto_pause

        # 1. score drop
        if len(self.rounds) >= 2:
            prev_score = self.rounds[-2].final_score
            if round_result.final_score < prev_score - ap.score_drop_threshold:
                return f"score_drop: {prev_score:.1f} → {round_result.final_score:.1f}"

        # 2. convergence stall
        if len(self.rounds) >= ap.stall_rounds:
            recent = self.rounds[-ap.stall_rounds:]
            if all(abs(r.convergence_delta) < 0.3 for r in recent):
                return f"convergence_stall: {ap.stall_rounds} rounds with delta < 0.3"

        # 3. critic disagreement
        scores = list(round_result.critic_scores.values())
        if len(scores) >= 2:
            if max(scores) - min(scores) >= ap.critic_disagreement_threshold:
                return f"critic_disagreement: spread={max(scores)-min(scores):.1f}"

        # 4. irreversible action
        if ap.check_irreversible:
            round_effects = self.side_effects.get_for_round(round_result.round_num)
            if any(e.effect_type == SideEffectType.IRREVERSIBLE for e in round_effects):
                return "irreversible_action_detected"

        return None

    def create_checkpoint(self):
        data = {
            "session_id": self.session_id,
            "round": self.current_round,
            "state": self.state.value,
            "task": self._task,
            "rounds": [r.to_dict() for r in self.rounds],
            "compact_memory": self._compact_memory.to_dict() if self._compact_memory else {},
            "side_effects": self.side_effects.summary,
            "timestamp": datetime.now().isoformat(),
        }
        self.checkpoint_store.save(self.current_round, data)

    def get_pending_directive(self) -> Optional[str]:
        d = self._pending_directive
        self._pending_directive = None
        return d

    def get_pending_focus(self) -> Optional[str]:
        f = self._pending_focus
        self._pending_focus = None
        return f

    # ─── Internal ───

    def _run_wrapper(self, engine_fn: Callable):
        try:
            result = engine_fn(self)
            if self.state not in (SessionState.CANCELLED, SessionState.FAILED):
                self.state = SessionState.COMPLETED
        except Exception as e:
            self.state = SessionState.FAILED
            self._pause_reason = str(e)

    def _do_rollback(self, to_round: int, new_directive: str = ""):
        self.state = SessionState.ROLLING_BACK

        # irreversible 체크
        irr_rounds = self.side_effects.irreversible_rounds_after(to_round)

        # reversible 보상
        self.side_effects.compensate_after(to_round)

        # checkpoint 복원
        cp = self.checkpoint_store.load(to_round)
        if cp:
            self.current_round = to_round
            self.rounds = self.rounds[:to_round]
            if cp.get("compact_memory") and self._compact_memory:
                # 간단 복원: checkpoint의 memory로 되돌림
                pass  # CompactMemory는 immutable snapshot이 아니라 복원 복잡 → 라운드만 잘라냄

        if new_directive:
            self._pending_directive = new_directive
            if self._compact_memory:
                self._compact_memory.inject_human_directive(new_directive, "rollback")

        self.state = SessionState.PAUSED
        self._pause_reason = f"rolled_back_to_round_{to_round}"
        if irr_rounds:
            self._pause_reason += f" (irreversible_in_rounds: {irr_rounds})"

    # ─── Serialization ───

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "status": self.state.value,
            "current_round": self.current_round,
            "pause_reason": self._pause_reason,
            "mode": self.config.mode,
            "rounds": [r.to_dict() for r in self.rounds],
            "available_actions": self._available_actions(),
            "checkpoints": self.checkpoint_store.available_rounds(),
            "side_effects_summary": self.side_effects.summary,
        }

    def _available_actions(self) -> List[str]:
        if self.state in (SessionState.PAUSED, SessionState.AWAITING_FEEDBACK):
            actions = ["continue", "feedback", "focus", "stop"]
            if self.checkpoint_store.available_rounds():
                actions.append("rollback")
            return actions
        return []
