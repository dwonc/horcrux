# Horcrux v7.0 — 최종 기획안 (R4 Synthesizer)

## 디베이트 진화 과정
- v5.0: 4 Phase 대규모 → GPT 5/10, Gemini 7/10 (과도한 범위)
- v6.0: Phase 0+1 압축 → 6개 지적 반영
- v7.0: **단일 Phase, 3개 작업** → 최종 수렴

## 3개 작업

### 작업 1: core.py 공통 모듈
- call_claude/codex/gemini 통합 (Web 방식 기준)
- call_with_retry (1회 재시도)
- extract_json (3단계 추출)
- load_config

### 작업 2: 점수 체계 정비
- ScoreResult dataclass (fail-closed)
- parse_category_scores (JSON→regex→single→failed)
- load_weights (list/dict 호환)
- weighted_average (파싱 실패 시 None)
- compute_round_score (degraded mode)

### 작업 3: 로그 원자적 저장
- save_session (tempfile + os.replace)
- load_session (파싱 실패 로깅)
- 스키마 v2 통합 (.jsonl 폐기)

## 의도적 제외
- templates/ 분리 (순수 리팩터링, 후속)
- Git Diff 모드 (별도 기획)
- 히스토리 비교 (별도 기획)
