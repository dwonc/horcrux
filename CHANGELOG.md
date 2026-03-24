# Debate Chain Changelog

## v8.1.0 — 2026-03-23

### 버그 수정
- **Codex CLI**: `-q` 플래그 → `exec --skip-git-repo-check` stdin 방식으로 복원 (TTY 체크 우회)
- **Claude CLI**: `cwd=tempfile.gettempdir()` 적용 — 프로젝트 파일 노출 차단 (기존 파일 읽고 "이미 구현됨" 오판 방지)
- **pair 모드**: splitting 단계에서 Claude가 파일시스템 탐색 후 JSON 대신 분석 텍스트 반환하던 문제 수정
- **check 툴**: pair job `error` 상태를 "Not found"로 잘못 처리하던 버그 수정
- **early return 로그 누락**: `run_pair` early return 시 로그 파일 미저장 문제 수정 (에러 내용 포함)

### 기능 추가
- **`output_dir` 파라미터**: pair2/pair3 완료 시 생성된 파일을 프로젝트 경로에 자동 저장
  - `run(task="...", mode="pair2", output_dir="D:\\MyProject")` 형식
  - `_save_pair_files()` 함수: results의 `files[]` 배열 파싱 → 경로 기준 자동 저장
- **`project_dir` 파라미터**: debate/debate_pair 모드에서 프로젝트 코드를 자동으로 읽어 context로 첨부
  - `_read_project_files()`: `.py` 파일을 크기 순 정렬해 max_chars 이내로 수집
  - 파일 읽기는 내가(Claude.ai) 하고, 분석/판단은 debate가 하는 구조
- **Claude timeout**: 600초 → 900초 (코드 생성 시 timeout 방지)
- **Codex timeout**: 600초 유지

### 프롬프트 개선
- **`SPLIT_PROMPT`**: "Do NOT read or analyze any files. Ignore the current directory." 추가 — Claude가 기존 파일 탐색 후 오판하는 문제 차단
- **`PART_PROMPT`**: "Write NEW code from scratch. Do NOT read or reference any existing files." 추가 — 코드 대신 플레이스홀더 반환 방지

### 인프라
- **대시보드 제거**: `web_ui/index.html` 및 `server_patch.py` `/dashboard` 라우트 제거 (기존 `localhost:5000` UI로 충분)
- **`start_full.bat` 정리**: Dashboard 문구 제거, 단계 번호 제거, 브라우저 자동 오픈 (`localhost:5000`)
- **MCP `run` 툴 스키마**: `output_dir`, `project_dir` 파라미터 추가

---

## v8.0.0 — 2026-03-23

### 아키텍처
- **`core/` 모듈 생성**: 10개 개선사항 파일화
  - `security.py`, `job_store.py`, `provider.py`, `async_worker.py`, `sse.py`
  - `cost_tracker.py`, `convergence.py`, `router.py`, `tools.py`, `types.py`
- **`server_patch.py`**: server.py 무수정 monkey-patch, `/api/jobs`, `/api/system` 엔드포인트

### AI Callers 수정 (v8)
- **Claude**: `-p "<prompt>"` 인자 방식 (stdin 혼용 버그 제거 — 폴더 탐색 무한대기 원인)
- **Codex**: `-q` 플래그 방식 시도 (v8.1.0에서 exec stdin으로 재수정)
- **공통**: `_truncate_prompt()` — MAX 12000자, 재시도 6000자, 양끝 보존 중간 압축
- **timeout**: Claude 300초, Codex 900초

---

## v7.0.0 — 이전

### 핵심 기능
- **Multi-Critic**: Codex + Gemini 병렬 Critic, 보수적 점수(min) 적용
- **Synthesizer = Codex**: Generator(Claude)와 다른 모델로 분리
- **다차원 수렴**: correctness / completeness / security / performance 4개 차원
- **Regression detection**: 이전 라운드 수정 사항 회귀 탐지
- **pair2/pair3 모드**: Claude + Codex 병렬 코드 생성
- **debate_pair 파이프라인**: debate → pair 자동 연결
- **self_improve 루프**: 자기개선 반복
- **MCP 서버**: Claude Desktop 연동, `run` / `check` / `self_improve` 통합 툴
