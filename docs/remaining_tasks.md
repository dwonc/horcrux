# Horcrux 남은 작업 목록

> 2026-03-29 세션 기준. 우선순위 순.

## 높은 우선순위

### 1. Web UI history 표시 수정
- **문제**: 동기 응답(`hrx_`) 스레드가 UI에서 "Generator (Claude)" 1개로만 표시
- **원인**: `run_adaptive`의 내부 라운드(Generator -> Critic -> Synthesizer -> Revision)가 `hrx_` state의 messages에 반영 안 됨
- **해결**: `_run_standard`/`_run_fast`/`_run_full_horcrux` 내부 단계를 messages로 저장하거나, `_result.json`의 history를 `hrx_` state에 매핑
- **영향**: UI에서 실시간 진행 상황 + 각 모델별 출력 확인 가능

### 2. ZoomScribe 프로젝트 완성
- **상태**: 코드 생성 완료, venv 설정 완료, .env 설정 완료
- **위치**: `D:\Projects\zoomscribe`
- **남은 작업**:
  - 실제 Zoom 녹음 파일로 테스트
  - whisperx 모델 다운로드 확인
  - pyannote HuggingFace 라이선스 동의 확인

## 중간 우선순위

### 3. Deep Refactor 모드 실제 테스트
- **상태**: 코드 작성 완료, 서버 연동 완료
- **미검증**: 실제 프로젝트에서 auto-split + 병렬 분석 동작 확인
- **테스트 방법**: `mode=deep_refactor, project_dir=D:/Custom_AI-Agent_Project/horcrux`

### 4. 추가 실험 과제
- Full 모드: Sonnet+full vs Opus+standard 재현성 확인 (T05 hard 이례치)
- Planning_pipeline 모드 Opus vs Sonnet 비교
- 10개 이상 태스크로 통계적 유의성 확보
- 실행당 토큰 소비량 실측

### 5. 404 폴링 근본 해결
- **현상**: 서버 재시작 시 running 상태였던 planning 스레드가 메모리에서 사라짐
- **현재 대응**: poll()에서 404 시 폴링 중단 (클라이언트 측)
- **근본 해결**: 서버 시작 시 logs/ 폴더에서 running 상태 스레드를 "aborted"로 마킹

## 낮은 우선순위

### 6. Standard 모드 정리 여부 결정
- 현재: auto 라우팅에서 제외, 사용자 명시 시만 동작
- 검토: 코드에서 완전 제거할지, 레거시로 유지할지
- 판단: 유지하는 게 리스크 적음 (현재 상태 OK)

### 7. Scoring 가중치 자동 튜닝 검증
- `_maybe_auto_tune_scoring()`이 10회마다 실행되는지 확인
- 현재 core_weight=0.6, aux_weight=0.4 → 데이터 축적 후 변동 추이 모니터링

### 8. Pair 모드 architect 프롬프트 검증
- SPLIT_PROMPT 강화 후 실제 pair 생성에서 인터페이스 불일치가 줄었는지 테스트

### 9. MCP 서버 업데이트
- deep_refactor 모드를 MCP 도구에 추가
- Sonnet 기본 모델 변경 반영 확인
