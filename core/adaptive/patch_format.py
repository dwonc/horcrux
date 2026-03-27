"""
core/adaptive/patch_format.py — Phase 2: JSON Patch Format Standardization

propose_patch 포맷을 JSON patch로 통일하여 director merge 가능하게 함.

Schema:
  {
    "file": "path/to/file",
    "hunks": [
      {
        "start_line": 10,
        "end_line": 15,
        "original": "old code",
        "proposed": "new code",
        "reason": "why this change"
      }
    ]
  }
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class PatchHunk:
    """단일 코드 변경 블록."""
    start_line: int
    end_line: int
    original: str
    proposed: str
    reason: str

    def to_dict(self) -> dict:
        return {
            "start_line": self.start_line,
            "end_line": self.end_line,
            "original": self.original,
            "proposed": self.proposed,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PatchHunk":
        return cls(
            start_line=d.get("start_line", 0),
            end_line=d.get("end_line", 0),
            original=d.get("original", ""),
            proposed=d.get("proposed", ""),
            reason=d.get("reason", ""),
        )


@dataclass
class FilePatch:
    """단일 파일에 대한 patch."""
    file: str
    hunks: List[PatchHunk] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "hunks": [h.to_dict() for h in self.hunks],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "FilePatch":
        return cls(
            file=d.get("file", ""),
            hunks=[PatchHunk.from_dict(h) for h in d.get("hunks", [])],
        )

    @property
    def total_lines_changed(self) -> int:
        return sum(h.end_line - h.start_line + 1 for h in self.hunks)


@dataclass
class PatchSet:
    """여러 파일에 대한 patch 묶음."""
    patches: List[FilePatch] = field(default_factory=list)
    source_agent: str = ""
    source_model: str = ""

    def to_dict(self) -> dict:
        return {
            "source_agent": self.source_agent,
            "source_model": self.source_model,
            "patches": [p.to_dict() for p in self.patches],
            "total_files": len(self.patches),
            "total_hunks": sum(len(p.hunks) for p in self.patches),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PatchSet":
        return cls(
            patches=[FilePatch.from_dict(p) for p in d.get("patches", [])],
            source_agent=d.get("source_agent", ""),
            source_model=d.get("source_model", ""),
        )


def parse_patch_from_llm_output(text: str, source_agent: str = "", source_model: str = "") -> PatchSet:
    """LLM 출력에서 JSON patch를 파싱.

    LLM이 정확한 JSON을 반환하지 않을 수 있으므로 여러 전략 시도:
    1. 전체 텍스트를 JSON으로 파싱
    2. ```json ... ``` 블록 추출 후 파싱
    3. 파싱 실패 시 빈 PatchSet 반환
    """
    # Strategy 1: direct JSON parse
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "file" in data:
            return PatchSet(
                patches=[FilePatch.from_dict(data)],
                source_agent=source_agent,
                source_model=source_model,
            )
        if isinstance(data, list):
            return PatchSet(
                patches=[FilePatch.from_dict(d) for d in data if isinstance(d, dict)],
                source_agent=source_agent,
                source_model=source_model,
            )
        if isinstance(data, dict) and "patches" in data:
            ps = PatchSet.from_dict(data)
            ps.source_agent = source_agent
            ps.source_model = source_model
            return ps
    except (json.JSONDecodeError, KeyError):
        pass

    # Strategy 2: extract ```json blocks
    json_blocks = re.findall(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    for block in json_blocks:
        try:
            data = json.loads(block)
            if isinstance(data, dict) and "file" in data:
                return PatchSet(
                    patches=[FilePatch.from_dict(data)],
                    source_agent=source_agent,
                    source_model=source_model,
                )
            if isinstance(data, list):
                return PatchSet(
                    patches=[FilePatch.from_dict(d) for d in data if isinstance(d, dict)],
                    source_agent=source_agent,
                    source_model=source_model,
                )
        except (json.JSONDecodeError, KeyError):
            continue

    # Strategy 3: fallback — empty
    return PatchSet(source_agent=source_agent, source_model=source_model)


def merge_patch_sets(patches: List[PatchSet]) -> PatchSet:
    """여러 agent의 patch를 merge. 같은 파일의 겹치는 hunk은 첫 번째 우선."""
    merged_files: Dict[str, FilePatch] = {}

    for ps in patches:
        for fp in ps.patches:
            if fp.file not in merged_files:
                merged_files[fp.file] = FilePatch(file=fp.file, hunks=list(fp.hunks))
            else:
                existing = merged_files[fp.file]
                existing_ranges = {(h.start_line, h.end_line) for h in existing.hunks}
                for h in fp.hunks:
                    if (h.start_line, h.end_line) not in existing_ranges:
                        existing.hunks.append(h)

    # sort hunks by start_line
    for fp in merged_files.values():
        fp.hunks.sort(key=lambda h: h.start_line)

    return PatchSet(
        patches=list(merged_files.values()),
        source_agent="director_merge",
        source_model="merged",
    )


PATCH_PROPOSAL_PROMPT_SUFFIX = """

Respond with ONLY a JSON patch in this exact format:
{
  "file": "path/to/file",
  "hunks": [
    {
      "start_line": <line number>,
      "end_line": <line number>,
      "original": "<exact original code>",
      "proposed": "<your replacement code>",
      "reason": "<why this change>"
    }
  ]
}

If multiple files need changes, return a JSON array of objects.
No markdown, no explanation outside JSON."""
