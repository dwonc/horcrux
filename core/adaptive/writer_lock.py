"""
core/adaptive/writer_lock.py — Phase 2: Single Writer Rule Enforcement

여러 CLI 에이전트가 동시에 파일을 write하지 못하게 하여 충돌 방지.

Role-based access control:
  generators: read, propose_patch
  critics:    read, comment
  director:   read, select, merge
  writer:     read, write, run_tests
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set


class AgentRole(str, Enum):
    GENERATOR = "generator"
    CRITIC = "critic"
    DIRECTOR = "director"
    WRITER = "writer"


class Permission(str, Enum):
    READ = "read"
    PROPOSE_PATCH = "propose_patch"
    COMMENT = "comment"
    SELECT = "select"
    MERGE = "merge"
    WRITE = "write"
    RUN_TESTS = "run_tests"


ROLE_PERMISSIONS: Dict[AgentRole, Set[Permission]] = {
    AgentRole.GENERATOR: {Permission.READ, Permission.PROPOSE_PATCH},
    AgentRole.CRITIC:    {Permission.READ, Permission.COMMENT},
    AgentRole.DIRECTOR:  {Permission.READ, Permission.SELECT, Permission.MERGE},
    AgentRole.WRITER:    {Permission.READ, Permission.WRITE, Permission.RUN_TESTS},
}


@dataclass
class WriterLockState:
    """현재 writer 상태 추적."""
    active_writer: Optional[str] = None
    active_role: Optional[AgentRole] = None
    pending_patches: List[dict] = field(default_factory=list)
    write_history: List[dict] = field(default_factory=list)


class WriterLock:
    """Single writer rule: 한 번에 하나의 agent만 write 가능."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state = WriterLockState()

    def can_perform(self, agent_id: str, role: AgentRole, action: Permission) -> bool:
        """agent가 해당 action을 수행할 수 있는지 확인."""
        allowed = ROLE_PERMISSIONS.get(role, set())
        if action not in allowed:
            return False

        # write 권한은 writer role만 + 다른 writer가 활성 상태가 아닐 때만
        if action == Permission.WRITE:
            with self._lock:
                if self._state.active_writer and self._state.active_writer != agent_id:
                    return False
        return True

    def acquire_write(self, agent_id: str) -> bool:
        """write lock 획득. 이미 다른 writer가 있으면 False."""
        with self._lock:
            if self._state.active_writer is None or self._state.active_writer == agent_id:
                self._state.active_writer = agent_id
                self._state.active_role = AgentRole.WRITER
                return True
            return False

    def release_write(self, agent_id: str) -> bool:
        """write lock 해제."""
        with self._lock:
            if self._state.active_writer == agent_id:
                self._state.active_writer = None
                self._state.active_role = None
                return True
            return False

    def submit_patch(self, agent_id: str, patch: dict) -> bool:
        """generator가 patch를 제출. director가 나중에 merge."""
        with self._lock:
            self._state.pending_patches.append({
                "agent_id": agent_id,
                "patch": patch,
            })
            return True

    def get_pending_patches(self) -> List[dict]:
        with self._lock:
            patches = self._state.pending_patches[:]
            return patches

    def clear_patches(self):
        with self._lock:
            self._state.pending_patches.clear()

    def record_write(self, agent_id: str, files: List[str]):
        with self._lock:
            self._state.write_history.append({
                "agent_id": agent_id,
                "files": files,
            })

    @property
    def is_locked(self) -> bool:
        return self._state.active_writer is not None

    def to_dict(self) -> dict:
        return {
            "active_writer": self._state.active_writer,
            "active_role": self._state.active_role.value if self._state.active_role else None,
            "pending_patches": len(self._state.pending_patches),
            "write_history_count": len(self._state.write_history),
        }
