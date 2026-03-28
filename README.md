# Horcrux

**6-Model Adaptive AI Orchestration Engine**

6개 AI 모델이 역할을 나눠 토론하고, 검증하고, 수렴합니다. 하나의 프롬프트만 넣으면 난이도를 자동 분류해서 최적 파이프라인으로 라우팅합니다.

## 정체성

Horcrux는 **코드 생성기가 아닙니다.** 코드 생성은 Claude Code 같은 단일 에이전트가 전체 컨텍스트를 유지하면서 짜는 게 더 빠르고 정확합니다.

Horcrux가 진짜 강한 영역은 **반복적 품질 개선이 필요한 작업**입니다:

- **서류/문서 작업** — 기획서, 보고서, 이력서 피드백. 초안 → 5개 모델이 각자 관점에서 비판 → 개선 → 수렴할 때까지 반복. 사람이 검토하는 과정을 자동화.
- **코드 리팩토링** — 기존 코드를 3개 모델이 독립 분석(아키텍처/코드품질/유지보수성) → 통합 플랜 → 5개 모델 크리틱 → 리비전 반복. Deep Refactor 모드로 대형 프로젝트도 모듈별 자동 분할 분석.
- **의사결정/분석** — debate 모드로 찬반 검토, 여러 관점에서 검증.
- **브레인스토밍** — 3개 모델이 독립적으로 아이디어 생성 → 종합 → 검증.

핵심 원칙: **Generator ≠ Critic ≠ Synthesizer** — 자기확증 편향을 구조적으로 제거.

### 6 Models, 5 Providers, 3 Architectures

| # | Model | Provider | Role |
|---|-------|----------|------|
| 1 | **Claude Opus 4.6** | Anthropic | Generator, Judge |
| 2 | **Codex 5.4** | OpenAI | Counter-Generator, Critic |
| 3 | **Gemini 2.5 Flash** | Google | Aux Critic |
| 4 | **Llama 3.3 70B** | Meta (via Groq) | Aux Critic |
| 5 | **DeepSeek Chat** | DeepSeek | Aux Critic |
| 6 | **GPT-OSS 120B** | OpenRouter | Aux Critic |

```
Claude(Generator) → Codex+Gemini+Llama+DeepSeek+GPT-OSS(5 Critics 병렬) → Codex(Synthesizer)
                                    ↑ score < 8.0 ↓
                              다음 라운드 (수렴까지 반복)
```

**점수 = Core min(Claude, Codex) × core_weight + Aux avg(Gemini, Llama, DeepSeek, GPT-OSS) × aux_weight**

> 가중치는 critic 신뢰도 데이터 기반으로 자동 튜닝됩니다. 10회 완료마다 `config.json`에 반영.

## Quick Start

```bash
git clone https://github.com/dwonc/horcrux.git
cd horcrux
pip install -r requirements.txt && npm install
cp .env.example .env   # API 키 입력
python server.py        # http://localhost:5000
```

## CLI

```bash
horcrux "fix typo in README"                          # Auto — 자동 분류
horcrux --mode fast "간단한 버그 수정"                  # Fast — 즉시 처리
horcrux --mode full --risk high "보안 감사"             # Full — 6모델 풀체인
horcrux --mode parallel --pair-mode pair3 "풀스택 앱"   # Parallel — 3AI 병렬
horcrux --mode deep_refactor "전체 리팩토링"            # Deep Refactor — 멀티모델 코드 분석
horcrux classify "아키텍처 리팩토링"                     # 분류만 미리보기
horcrux --server "브레인스토밍해줘"                      # 서버 경유 (웹 UI 공유)
```

> Windows: `horcrux.bat`, Linux/Mac: `./horcrux` (또는 `python adaptive_orchestrator.py`)

## Modes

### Auto (기본)
```
Task → Classifier(heuristic) → intent + 난이도 자동 감지 → 최적 엔진 선택
```
모드를 몰라도 됩니다. 자연어로 말하면 알아서 라우팅.

| 이렇게 말하면 | 이렇게 돌아감 |
|--------------|-------------|
| "typo 수정해줘" | fast → adaptive_fast → 즉시 결과 |
| "브레인스토밍해줘" | standard → planning_pipeline → 4-phase |
| "아키텍처 리팩토링" | full → adaptive_full → debate loop |
| "PPT 만들어줘" | full → planning_pipeline → artifact 생성 |
| "3개 AI로 동시에" | parallel → pair_generation → 병렬 |
| "이 솔루션 3번 개선" | standard → self_improve → 반복 개선 |
| "전체 코드 리팩토링 분석" | full → deep_refactor → 멀티모델 분석 |

### Fast
```
Claude(1-pass) → Light Critic → 결과        20~60초
```

### Standard
```
Claude+Codex(병렬 생성) → Synthesizer → Core Critic → Revision(max 2) → 결과        60~180초
```

### Full
```
Claude+Codex(병렬) → Synthesizer → 5 Critics(병렬) → Convergence → Revision Loop → 결과        3~10분
```

### Parallel
```
Architect(Claude) → 2~3 AI 병렬 생성 (비판 없음, 순수 속도)
```
독립적인 작업에 적합: 여러 문서 동시 작성, 다른 접근법 비교, 독립 서비스 설계.

### Deep Refactor (NEW)
```
Phase 0: Auto-Split — 프로젝트를 모듈 그룹으로 자동 분할
Phase 1: 그룹별 × 3모델 병렬 분석 (아키텍처/코드품질/유지보수성)
Phase 2: 전체 그룹 분석 → 1개 통합 리팩토링 플랜
Phase 3: 5모델 크리틱 → 리비전 반복 (수렴까지)
Phase 4: 최종 리팩토링 플랜 출력
```
- 대형 프로젝트도 모듈별 자동 분할 → 50K 제한 극복, **전체 코드 100% 커버**
- 특정 모듈만 지정 분석도 가능 (`project_dir`을 해당 모듈 경로로)
- 예: 4그룹 × 3모델 = 12개 분석 동시 실행

## Web UI

`http://localhost:5000` — 모드 버튼 6개: **Auto** / **Fast** / **Standard** / **Full** / **Parallel** / **Deep Refactor**

## MCP (Claude Desktop)

`claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "horcrux": {
      "command": "node",
      "args": ["D:\\path\\to\\horcrux\\mcp_server.js"],
      "env": {
        "GROQ_API_KEY": "gsk_xxxx",
        "DEEPSEEK_API_KEY": "sk-xxxx",
        "OPENROUTER_API_KEY": "sk-or-v1-xxxx"
      }
    }
  }
}
```

MCP 도구 5개: `run` / `check` / `classify` / `analytics` / `horcrux_test`

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Auto / Fast / Standard / Full / Parallel / Deep Refactor        │
│                     │                                            │
│              /api/horcrux/run                                     │
│                     │                                            │
│              ┌──────▼──────┐                                     │
│              │  Classifier  │ heuristic intent detect             │
│              └──────┬──────┘                                     │
│  ┌───────┬──────┬───┼────┬───────────┬──────────┬──────────┐    │
│  ▼       ▼      ▼   ▼    ▼           ▼          ▼          ▼    │
│ adaptive adaptive adaptive planning  pair     self       deep  │
│ _fast    _standard _full  _pipeline  _gen     _improve   _refactor│
│                    debate                                        │
│                    _loop                                         │
└──────────────────────────────────────────────────────────────────┘
```

## Subscriptions

| Tier | 구독 | 월 비용 | 품질 |
|------|------|--------|------|
| Full | Claude Max + ChatGPT Plus | $120 | Opus 4.6 + Codex 5.4 |
| Balanced | Claude Pro + ChatGPT Plus | $40 | Sonnet 4.6 + GPT-4o |
| Minimal | Claude Pro only | $20 | Sonnet + OpenAI API fallback |

Aux Critics(Groq, DeepSeek, OpenRouter)는 무료 tier로 운영됩니다.

## Project Structure

```
horcrux/
├── server.py                  # Flask 웹 서버 + 통합 API
├── adaptive_orchestrator.py   # CLI + 오케스트레이터
├── deep_refactor.py           # Deep Refactoring 파이프라인 (NEW)
├── mcp_server.js              # MCP 서버 v8 (Claude Desktop)
├── horcrux / horcrux.bat      # CLI shortcut
├── planning_v2.py             # Planning 파이프라인
├── config.json                # 설정 (scoring 가중치 포함)
├── core/
│   ├── adaptive/              # classifier, memory, analytics, fallback...
│   ├── provider.py            # 6-model provider 추상화
│   └── ...
├── docs/                      # 스펙 문서
├── logs/                      # 세션 로그
└── tests/                     # 테스트
```

> 상세: [GUIDE.md](GUIDE.md) | 변경 이력: [CHANGELOG.md](CHANGELOG.md)

## License

MIT
