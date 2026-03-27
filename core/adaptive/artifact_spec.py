"""
core/adaptive/artifact_spec.py — Phase 2: Artifact Spec Optimization

PPT/PDF artifact는 멀티모델이 파일을 직접 수정하지 않고 spec 기반 렌더링으로 간소화.

Rules:
  1. artifact 단계는 final content package를 입력으로 받음
  2. artifact_profile은 내용 재해석 금지
  3. PPT/PDF는 artifact-ready spec을 만든 후 최종 renderer가 생성
  4. artifact critic은 정보량/흐름/누락만 점검
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class SlideSpec:
    """PPT 단일 슬라이드 스펙."""
    slide_number: int
    title: str
    body_points: List[str] = field(default_factory=list)
    notes: str = ""
    layout: str = "title_body"  # title_body | section | image_text | chart

    def to_dict(self) -> dict:
        return {
            "slide_number": self.slide_number,
            "title": self.title,
            "body_points": self.body_points,
            "notes": self.notes,
            "layout": self.layout,
        }


@dataclass
class DocSection:
    """문서 단일 섹션 스펙."""
    heading: str
    level: int = 1  # h1, h2, h3
    content: str = ""
    subsections: List["DocSection"] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "heading": self.heading,
            "level": self.level,
            "content": self.content,
            "subsections": [s.to_dict() for s in self.subsections],
        }


@dataclass
class ArtifactSpec:
    """최종 renderer에 전달할 artifact-ready spec."""
    artifact_type: str  # ppt | pdf | doc
    title: str = ""
    author: str = ""
    date: str = ""
    slides: List[SlideSpec] = field(default_factory=list)
    sections: List[DocSection] = field(default_factory=list)
    metadata: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "artifact_type": self.artifact_type,
            "title": self.title,
            "author": self.author,
            "date": self.date,
            "metadata": self.metadata,
        }
        if self.artifact_type == "ppt":
            d["slides"] = [s.to_dict() for s in self.slides]
            d["slide_count"] = len(self.slides)
        else:
            d["sections"] = [s.to_dict() for s in self.sections]
            d["section_count"] = len(self.sections)
        return d

    @property
    def content_count(self) -> int:
        if self.artifact_type == "ppt":
            return len(self.slides)
        return len(self.sections)


# ═══════════════════════════════════════════
# Artifact Critic (정보량/흐름/누락만 점검)
# ═══════════════════════════════════════════

ARTIFACT_CRITIC_PROMPT = """You are an artifact quality reviewer.
Your job is to check ONLY these aspects:
1. **Information completeness** — are all key points from the content package included?
2. **Flow & structure** — is the ordering logical?
3. **Missing items** — anything important omitted?

DO NOT:
- Rewrite or reinterpret the content
- Change the tone or style
- Add new information not in the source

Rate each aspect 1-10 and provide an overall score.
Respond in JSON:
{
  "completeness": {"score": X, "issues": ["..."]},
  "flow": {"score": X, "issues": ["..."]},
  "missing": {"score": X, "items": ["..."]},
  "overall_score": X,
  "pass": true/false
}"""


ARTIFACT_SPEC_PROMPT = """Convert the following content into an artifact-ready spec.

Artifact type: {artifact_type}

Rules:
- Do NOT reinterpret or modify the content meaning
- Structure it for the target format ({artifact_type})
- Include all key information from the source
- For PPT: create slide specs with title, body_points, notes, layout
- For DOC/PDF: create section specs with heading, level, content

Source content:
{content}

Respond with ONLY JSON in this format:
{{
  "artifact_type": "{artifact_type}",
  "title": "...",
  "slides": [  // for PPT
    {{"slide_number": 1, "title": "...", "body_points": ["..."], "notes": "...", "layout": "title_body"}}
  ],
  "sections": [  // for DOC/PDF
    {{"heading": "...", "level": 1, "content": "..."}}
  ]
}}"""


def build_artifact_spec_prompt(artifact_type: str, content: str) -> str:
    """artifact spec 생성 프롬프트."""
    return ARTIFACT_SPEC_PROMPT.format(
        artifact_type=artifact_type,
        content=content[:8000],
    )


def build_artifact_critic_prompt(spec: ArtifactSpec, source_content: str) -> str:
    """artifact critic 프롬프트."""
    import json
    return (
        f"{ARTIFACT_CRITIC_PROMPT}\n\n"
        f"Source content summary:\n{source_content[:3000]}\n\n"
        f"Artifact spec:\n{json.dumps(spec.to_dict(), ensure_ascii=False, indent=2)[:5000]}"
    )
