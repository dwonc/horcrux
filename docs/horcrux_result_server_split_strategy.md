# Horcrux server.py 모듈 분리 전략 (통합안 v3)

## 1. 현재 구조 진단

`server.py`는 사실상 6개 시스템이 한 파일에 겹쳐 있다:
- **시스템 A**: 앱 부트스트랩과 환경 로딩 (`Flask`, `.env`, `LOG_DIR`, `app.run`)
- **시스템 B**: AI 호출 어댑터 (`call_claude`, `call_codex`, `call_gemini`, aux critic 호출)
- **시스템 C**: debate/pair/self_improve/pipeline orchestration (`run_debate`, `run_pair`, `run_self_improve`, `run_debate_pair_pipeline`)
- **시스템 D**: 전역 런타임 상태 저장소 (`debates`, `pairs`, `pipelines`, `self_improves`, `horcrux_states`, `interactive_sessions`)
- **시스템 E**: HTTP API 및 UI (`@app.route`, `HTML_TEMPLATE`, SSE)
- **시스템 F**: Horcrux unified router (`/api/horcrux/run`)와 analytics/classification 브리지

**핵심 인사이트**: 이 파일의 진짜 문제는 길이가 아니라 **전역 상태(dict)와 self-HTTP 호출(`requests.post("http://localhost:5000/...")`)이 결합의 허브**라는 점이다. 라우트부터 옮기면 순환 import와 동작 회귀가 거의 확실하다.

## 2. 아키텍처 옵션 비교 및 선택

분리 전략에는 세 가지 현실적 경로가 있다. 각각의 회귀 위험, 노력량, 되돌리기 용이성을 비교한다.

### 옵션 A: "Extract Services In-Place" (최소 침습)
- **방법**: `server.py` 안에서 함수/클래스를 별도 `.py` 파일로 꺼내되, 전역 dict는 그대로 `server.py`에 남긴다. 서비스 파일은 `from server import debates` 식으로 역참조.
- **장점**: 회귀 위험 최소, 1~2일 내 완료 가능, 되돌리기 trivial.
- **단점**: 순환 import 위험 상존, `server.py`가 여전히 300줄+ 상태 허브로 남음, self-HTTP 문제 미해결.
- **적합 상황**: 리팩터링에 투자할 시간이 1주 미만이거나, 팀원이 대규모 변경에 동의하지 않을 때.

### 옵션 B: "Blueprint-First + Legacy Globals" (중간 경로)
- **방법**: Flask Blueprint로 라우트만 먼저 분리. 전역 dict는 `server.py`에 유지하고, Blueprint 파일에서 `from server import debates` 등으로 접근. RuntimeRegistry는 도입하지 않음.
- **장점**: Flask 표준 패턴, 라우트 파일 분리만으로 가독성 대폭 개선.
- **단점**: 전역 dict 직접 참조가 레포 전체에 퍼짐, 테스트 격리 어려움, self-HTTP 문제 미해결.
- **적합 상황**: 라우트 수가 많아 네비게이션이 주 병목일 때.

### 옵션 C: "App Factory + Identity-Preserving Registry + DI" (본 전략 — 선택)
- **방법**: RuntimeRegistry가 **기존 전역 dict 객체 자체를 내부 저장소로 사용**(새 dict 생성 X), app factory가 DI container를 통해 서비스에 주입, self-HTTP를 ServiceDispatcher로 교체.
- **장점**: 테스트 격리 완전, 순환 import 근절, self-HTTP 제거로 latency/신뢰성 개선.
- **단점**: 노력량 가장 큼(2~3주), 단계별 검증 필수.
- **적합 상황**: 장기 유지보수 가치가 높고, CI가 갖춰져 있을 때.

**선택 근거**: 옵션 C를 선택한다. self-HTTP 호출과 전역 상태 난맥이 현재 버그와 테스트 불가능성의 근본 원인이며, 옵션 A/B는 이를 해결하지 못한다. 단, 옵션 C의 Phase 0~1은 사실상 옵션 A와 동일한 안전한 추출이므로, 중간에 멈춰도 개선 효과가 남는다.

## 3. 핵심 원칙

1. 루트 `server.py`는 삭제하지 않고 **50~120줄짜리 호환용 엔트리포인트**로 남긴다
2. 기존 URL, JSON shape, job id prefix(`pair_`, `dp_`, `si_`, `hrx_`, `adp_`, `plan_`), 로그 파일명, 상태 키 이름은 **전부 유지**한다
3. 전역 dict를 바로 없애지 않고 **Identity-Preserving Registry 패턴으로 감싼다** (아래 상세)
4. `requests.post("http://localhost:5000/...")` self-HTTP 호출은 **레포 전체에서 탐색하여 direct service dispatch로 제거**한다
5. `planning_v2`는 이미 DI 스타일(`inject_callers`)을 갖고 있으므로, 분리의 **선례이자 기준점**으로 삼는다
6. 라우트 기준이 아니라 **state/service/config 경계**부터 분리한다
7. **Import 호환성은 감이 아니라 감사(audit)로 보장**한다 — Phase별 `grep` 기반 검증 필수

## 4. 목표 디렉터리 구조


horcrux/
├── server.py                          # 얇은 호환 shim (create_app import + app.run)
├── horcrux_app/
│   ├── __init__.py                    # 공개 패키지 인터페이스 (아래 상세)
│   ├── app_factory.py                 # Flask 생성, config load, DI container, blueprint 등록
│   ├── config.py                      # .env, LOG_DIR, model lists, AUX_CRITIC_ENDPOINTS, 상수
│   ├── dependencies.py                # build_container() → app.extensions["horcrux"]
│   │
│   ├── state/
│   │   ├── __init__.py
│   │   ├── registry.py                # RuntimeRegistry (Identity-Preserving, 아래 상세)
│   │   ├── stores.py                  # 카테고리별 store 헬퍼
│   │   └── models.py                  # TypedDict / dataclass (2차 정리용)
│   │
│   ├── repos/
│   │   ├── __init__.py
│   │   └── job_logs.py                # JSON log 파일 read/write/load-through-cache
│   │
│   ├── ai/
│   │   ├── __init__.py
│   │   ├── providers.py               # call_claude, call_codex, call_gemini, fallback
│   │   ├── critic_endpoints.py        # AUX_CRITIC_ENDPOINTS, _truncate_for_aux, _call_aux_critic
│   │   └── parsing.py                 # extract_json, extract_score, format_issues_compact
│   │
│   ├── prompts/
│   │   ├── __init__.py
│   │   ├── debate.py                  # GENERATOR_PROMPT, CRITIC_PROMPT, SYNTHESIZER_PROMPT
│   │   ├── pair.py                    # SPLIT_PROMPT, PART_PROMPT
│   │   ├── self_improve.py            # SELF_IMPROVE_PROMPT
│   │   └── common.py                  # 공통 프롬프트 조각
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   ├── critic_service.py          # run_multi_critic, normalize, convergence
│   │   ├── debate_service.py          # run_debate, extract_debate_artifact
│   │   ├── pair_service.py            # run_pair, _save_pair_files
│   │   ├── pipeline_service.py        # run_debate_pair_pipeline
│   │   ├── self_improve_service.py    # run_self_improve
│   │   ├── project_context_service.py # _read_project_files
│   │   ├── dispatch.py                # ServiceDispatcher (self-HTTP 제거 핵심)
│   │   └── analytics_service.py       # analytics/classification
│   │
│   ├── http/
│   │   ├── __init__.py
│   │   ├── api_common.py              # /api/threads, /api/delete, /api/timing
│   │   ├── routes_debate.py           # /api/start, /api/status, /api/result, /api/stop
│   │   ├── routes_pair.py             # /api/pair*
│   │   ├── routes_pipeline.py         # /api/debate_pair, /api/pipeline/*
│   │   ├── routes_self_improve.py     # /api/self_improve*
│   │   ├── routes_horcrux.py          # /api/horcrux/*, interactive_sessions
│   │   ├── routes_analytics.py        # /api/analytics*
│   │   ├── routes_ui.py               # / → render_template("index.html")
│   │   └── sse.py                     # SSE 스트리밍 유틸리티
│   │
│   └── templates/
│       └── index.html                 # HTML_TEMPLATE 외부화
│
├── planning_v2.py                     # 기존 유지, injection만 app_factory로 이관
├── tests/
│   ├── test_no_self_http.py           # self-HTTP 호출 부재 검증 테스트
│   ├── test_registry_identity.py      # Registry가 원본 dict 동일 객체임을 검증
│   ├── test_import_compat.py          # server.py re-export가 유효한지 검증
│   └── test_migration_contracts.py    # Phase별 API contract 검증
└── core/                              # 기존 코어 로직 유지


## 5. 파일별 분리 내용 상세

### `horcrux_app/__init__.py` — 공개 패키지 인터페이스
python
"""horcrux_app public interface.

외부 모듈(planning_v2, core/*, 사용자 스크립트)은 이 파일에서
export된 이름만 사용해야 한다. 내부 모듈 직접 import는 비공개로 간주.
"""
from horcrux_app.app_factory import create_app
from horcrux_app.ai.providers import call_claude, call_codex, call_gemini
from horcrux_app.services.debate_service import run_debate
from horcrux_app.services.pair_service import run_pair
from horcrux_app.services.pipeline_service import run_debate_pair_pipeline
from horcrux_app.services.self_improve_service import run_self_improve

__all__ = [
    "create_app",
    "call_claude", "call_codex", "call_gemini",
    "run_debate", "run_pair", "run_debate_pair_pipeline", "run_self_improve",
]

**유지보수 규칙**: 새 public API를 추가할 때 반드시 `__all__`에 등록. Phase 3 이후 `server.py` re-export는 deprecation warning으로 전환.

### `server.py` (호환 shim)
python
import warnings
from horcrux_app.app_factory import create_app

app = create_app()

# === 호환용 re-export ===
# planning_v2 등 기존 모듈이 `from server import X` 패턴을 사용할 수 있다.
# Phase 3 이후 deprecation warning 활성화 예정.
from horcrux_app import (  # noqa: E402, F401
    call_claude, call_codex, call_gemini,
    run_debate, run_pair, run_debate_pair_pipeline, run_self_improve,
)

# 전역 dict 호환 alias — registry 내부의 **동일 객체**를 가리킨다 (복사 아님)
_reg = app.extensions["horcrux"].registry
debates = _reg.debates        # 동일 dict 객체
pairs = _reg.pairs
pipelines = _reg.pipelines
self_improves = _reg.self_improves
horcrux_states = _reg.horcrux_states
interactive_sessions = _reg.interactive_sessions

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

유지 이유: 배치 파일, 문서, 기존 import 경로, 실행 습관 보존.

### `horcrux_app/app_factory.py`
- `Flask()` 생성, config load, template/static folder 설정
- DI container 생성 (`build_container()`): **Phase 1부터 실제 서비스 인스턴스를 container에 등록**
- container를 `app.extensions["horcrux"]`에 저장
- 모든 blueprint 등록
- `planning_v2.register_planning_v2_routes(app)` 호출
- `planning_v2.inject_callers(...)` 주입 수행

#### DI Container 통합 상세
python
def build_container(app):
    """Phase별 점진적 등록. 각 Phase에서 새 서비스가 추가된다."""
    container = HorcruxContainer()
    
    # Phase 0: config + registry (항상)
    container.config = Config.from_env()
    container.registry = RuntimeRegistry.from_existing_globals(
        debates=_DEBATES, pairs=_PAIRS, ...  # 원본 dict 객체 전달
    )
    
    # Phase 1: AI providers
    container.ai_providers = AIProviders(container.config)
    
    # Phase 2: services (registry + providers 의존)
    container.debate_service = DebateService(
        registry=container.registry,
        providers=container.ai_providers,
    )
    # ... pair_service, pipeline_service 등
    
    # Phase 3: dispatcher (모든 서비스 의존)
    container.dispatcher = ServiceDispatcher(
        registry=container.registry,
        debate_svc=container.debate_service,
        pair_svc=container.pair_service,
        # ...
    )
    
    app.extensions["horcrux"] = container
    return container


### `horcrux_app/config.py`
- `.env` 읽기, `LOG_DIR`, provider model list, timeout, endpoint config
- `GEMINI_MODELS`, `CLAUDE_MODELS`, `AUX_CRITIC_ENDPOINTS`, `AUX_MAX_PROMPT_CHARS`
- **원칙**: 상수는 이 파일에서만 정의하고 다른 모듈은 import만 한다

### `horcrux_app/state/registry.py` — **Identity-Preserving Registry (가장 중요)**

#### 핵심 문제: Stale Reference 방지

기존 코드 전체에 `debates["some_id"]["status"] = "done"` 같은 **직접 dict mutation**이 퍼져 있다. 새 dict를 만들면 기존 참조가 stale해진다. 이를 해결하는 전략:

**Identity-Preserving 원칙**: Registry는 새 dict를 생성하지 않고, **기존 전역 dict 객체를 그대로 내부 저장소로 사용**한다.

python
import threading

# Phase 0에서 server.py의 전역 dict를 이 모듈로 이동
_DEBATES: dict = {}      # 기존 server.py의 debates와 동일 객체
_PAIRS: dict = {}
_PIPELINES: dict = {}
_SELF_IMPROVES: dict = {}
_HORCRUX_STATES: dict = {}
_INTERACTIVE_SESSIONS: dict = {}


class RuntimeRegistry:
    """전역 상태 dict의 단일 접근점.
    
    핵심 보장: self.debates IS _DEBATES (동일 객체).
    기존 코드가 debates[k] = v로 직접 mutation해도 registry를 통해 보인다.
    """
    
    def __init__(self, debates, pairs, pipelines, 
                 self_improves, horcrux_states, interactive_sessions):
        # 새 dict 생성 금지 — 전달받은 객체를 그대로 사용
        self.debates = debates
        self.pairs = pairs
        self.pipelines = pipelines
        self.self_improves = self_improves
        self.horcrux_states = horcrux_states
        self.interactive_sessions = interactive_sessions
        self._lock = threading.RLock()
    
    @classmethod
    def from_existing_globals(cls, **dicts):
        """기존 전역 dict 객체를 받아 Registry를 생성.
        assert all(isinstance(v, dict) for v in dicts.values())
        """
        return cls(**dicts)
    
    @classmethod
    def default(cls):
        """신규 생성 (테스트용)."""
        return cls(
            debates=_DEBATES, pairs=_PAIRS, pipelines=_PIPELINES,
            self_improves=_SELF_IMPROVES, horcrux_states=_HORCRUX_STATES,
            interactive_sessions=_INTERACTIVE_SESSIONS,
        )
    
    def get_any_job(self, job_id: str) -> dict | None:
        """ID prefix로 적절한 store에서 job을 찾는다."""
        with self._lock:
            for store in self._all_stores():
                if job_id in store:
                    return store[job_id]
        return None
    
    def delete_job(self, job_id: str) -> bool:
        with self._lock:
            for store in self._all_stores():
                if job_id in store:
                    del store[job_id]
                    return True
        return False
    
    def iter_all_jobs(self, include_planning_v2=True):
        """모든 store의 job을 순회. planning_v2는 extra_iterators로 지원."""
        with self._lock:
            for store in self._all_stores():
                yield from store.items()
        if include_planning_v2 and hasattr(self, '_extra_iterators'):
            for it in self._extra_iterators:
                yield from it()
    
    def register_extra_iterator(self, fn):
        """planning_v2.plannings 등 외부 store 순회 지원."""
        if not hasattr(self, '_extra_iterators'):
            self._extra_iterators = []
        self._extra_iterators.append(fn)
    
    def mark_abort(self, job_id: str) -> bool:
        job = self.get_any_job(job_id)
        if job:
            job["abort"] = True
            return True
        return False
    
    def _all_stores(self):
        return [
            self.debates, self.pairs, self.pipelines,
            self.self_improves, self.horcrux_states,
            self.interactive_sessions,
        ]


#### Identity 보장 검증 (CI 필수 테스트)
python
# tests/test_registry_identity.py
def test_registry_uses_same_dict_objects():
    """Registry가 원본 dict와 동일 객체(is)임을 보장."""
    from horcrux_app.state.registry import _DEBATES, _PAIRS, RuntimeRegistry
    reg = RuntimeRegistry.default()
    assert reg.debates is _DEBATES
    assert reg.pairs is _PAIRS
    # 외부에서 직접 mutation해도 registry를 통해 보여야 한다
    _DEBATES["test_123"] = {"status": "running"}
    assert reg.get_any_job("test_123") is not None
    assert reg.debates["test_123"]["status"] == "running"
    del _DEBATES["test_123"]

def test_server_shim_aliases_are_same_objects():
    """server.py의 debates alias가 registry 내부와 동일 객체."""
    from server import debates, app
    reg = app.extensions["horcrux"].registry
    assert debates is reg.debates


#### 동시성 전략 상세
- `_lock`은 `threading.RLock()`으로, **cross-store 연산**(get_any_job, delete_job, iter_all_jobs)에만 사용
- 개별 store 내 단일 key 접근(e.g., `debates[id]["status"] = "done"`)은 GIL이 보호하므로 Phase 1~2에서는 lock 불필요
- Phase 3 이후 asyncio/multi-worker 전환 시: `_lock`을 per-store `threading.Lock()`으로 세분화하고, 장기 보유 방지를 위해 context manager 패턴 도입
- **주의**: `iter_all_jobs`에서 lock 보유 중 yield하므로, caller가 장시간 iteration하면 다른 cross-store 연산이 block된다. Phase 2에서 snapshot-copy 방식(`list(store.items())`)으로 전환 검토

### `horcrux_app/repos/job_logs.py`
- JSON log 파일 read/write/load-through-cache
- 각 route에서 반복되는 `LOG_DIR / f"{id}.json"` 로직 통합
- `load_or_none`, `save_state`, `delete_state`, `list_logged_threads`
- **상태 복원 전략**: 서버 재시작 시 `LOG_DIR`에서 미완료 job을 로드하여 registry에 복원하는 `restore_from_logs(registry)` 함수 제공. app_factory에서 호출.

### `horcrux_app/ai/providers.py`
- `call_claude`, `call_codex`, `call_gemini`, `_call_gemini_with_model`, `_call_openai_sdk`, `_call_opensource_fallback`, `_codex_fallback`
- 모델 fallback lock 포함
- 외부 API 통신은 여기로 집중

### `horcrux_app/ai/critic_endpoints.py`
- `AUX_CRITIC_ENDPOINTS` 정의, `_truncate_for_aux`, `_call_aux_critic`
- provider와 critic-specific config 분리

### `horcrux_app/ai/parsing.py`
- `extract_json`, `extract_score`, `format_issues_compact`, critic schema normalize
- `core/convergence.py`와 통합 검토 가능

### `horcrux_app/prompts/*.py`
- 문자열 prompt 전부 이동 (debate, pair, self_improve 별도)
- **장점**: 프롬프트 수정이 라우트/서비스 로직 diff와 섞이지 않음

### `horcrux_app/services/dispatch.py` — **self-HTTP 제거 핵심 (레포 전체 대상)**

이 모듈은 Horcrux router뿐 아니라 **레포 전체에서 발견되는 모든 self-HTTP 호출**을 대체한다.

#### 마이그레이션 전 필수 단계: 전수 탐색
bash
# 마이그레이션 시작 전 반드시 실행
grep -rn 'requests\.\(post\|get\|put\|delete\).*localhost' . --include='*.py'
grep -rn 'requests\.\(post\|get\|put\|delete\).*127\.0\.0\.1' . --incl