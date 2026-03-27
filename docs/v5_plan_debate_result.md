# Horcrux v5.0 기획안
## 첫 번째 실전 디베이트 결과물 (2026-03-18)

### 디베이트 참여 AI
- **Generator (Claude Opus 4.6)**: 기획안 초안 작성
- **Critic (GPT-5.4)**: 코드리뷰 + 점수 (7/10 추정)
- **Verifier (Gemini)**: QA 검증 (7/10)
- **평균**: 7.0/10 → threshold 8.0 미달 → 다음 라운드 진행

---

## Generator 출력 (Claude Opus 4.6)

### 현재 상태 진단

| 구성요소 | 실제 상태 |
|---------|----------|
| AI 호출 | CLI vs Web 인터페이스 불일치 |
| 점수 파싱 | CLI: regex, Web: JSON+fallback — 별도 구현 |
| 로그 저장 | CLI: .jsonl+_result.json, Web: 단일 .json — 스키마 호환 불가 |
| config 가중치 | weights 설정 있지만 avg=(c+v)/2 하드코딩 — dead code |
| UI | server.py 내 인라인 HTML 130줄 — 유지보수 불가 |

### Phase 0: 구조 통합 (선행 필수)
- core.py 공통 모듈 추출
- 로그 포맷 v2 통합 (단일 .json)
- UI 분리 (templates/index.html)
- 에러 처리 개선 (재시도 + skip)

### Phase 1: 카테고리별 구조화 점수
- accuracy, completeness, edge_cases, practicality
- 3단계 fallback 파서 (JSON → key:value → 단일점수)
- 가중 평균으로 (c+v)/2 교체

### Phase 2: Git Diff 코드 리뷰 모드
- --diff 플래그로 staged changes 리뷰
- lockfile/binary 제외, 시크릿 마스킹
- 리뷰 특화 카테고리 (security, correctness, readability, performance)

### Phase 3: 세션 비교 + 히스토리
- /api/compare 엔드포인트
- 태그 시스템 (수동 v1)
- 라운드별 점수 추이 시각화

---

## Verifier 검증 (Gemini) — 7/10

### 핵심 지적사항
1. **파싱 실패 시 5.0 기본값**: '판단 불가능'을 '보통'으로 오인하는 논리적 결함
2. **파일 Race Condition**: Windows 파일 잠금에서 CLI-Web 병행 시 충돌 가능
3. **Diff Truncation**: 3만자 자르기는 뒷부분 변경사항 무시 — 파일별 균등 포함 필요

### QA 권고
- 원자적 파일 저장 (os.replace)
- Fail-Fast 점수 정책
- 스키마 버전 관리

---

## 권장 순서
Phase 0 → 1 → 2 → 3 (0이 나머지 전부의 기반)
