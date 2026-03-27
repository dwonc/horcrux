# Horcrux v6.0 기획안 (라운드 4 Synthesizer 출력)

## 디베이트 히스토리
- **R1**: Claude Opus → v5.0 기획 (4 Phase 대규모)
- **R2**: GPT-5.4 → 5/10 (범위 과다, Race Condition, fallback 5.0 위험)
- **R2**: Gemini → 7/10 (원자성, fail-fast, 스키마 버전)
- **R3**: Synthesizer → 피드백 반영 개선
- **R4**: Claude Opus → v6.0 (6개 지적 전부 반영)

## Critic 지적 → 반영 매핑

| # | Critic/Verifier 지적 | 반영 내용 |
|---|---------------------|----------|
| 1 | Race Condition (파일 동시접근) | tempfile + os.replace() 원자적 교체 |
| 2 | skip 시 점수 계산 불가 | degraded 모드: 한쪽만으로 계산, 양쪽 실패 시 즉시 중단 |
| 3 | 파싱 실패 5.0 기본값 위험 | ScoreResult 클래스 + fail-closed + None 반환 |
| 4 | Diff truncation 편향 | Phase 2로 분리 (후속 기획) |
| 5 | weights 포맷 호환성 | load_weights()에서 list/dict 모두 수용 |
| 6 | 범위 과다 | Phase 0+1로 압축, Phase 2-3 후속 분리 |

## 핵심 설계 결정

### 원자적 저장
```python
fd, tmp_path = tempfile.mkstemp(dir=log_dir, suffix=".tmp")
with os.fdopen(fd, 'w') as f:
    json.dump(state, f)
os.replace(tmp_path, path)  # NTFS/POSIX 모두 원자적
```

### Degraded 모드 점수 계산
| Critic | Verifier | 처리 |
|--------|----------|------|
| ✅ | ✅ | weighted_avg(c, v) |
| ✅ | ❌ | critic 점수만 사용 + degraded=true |
| ❌ | ✅ | verifier 점수만 사용 + degraded=true |
| ❌ | ❌ | 즉시 중단 + status="error" |

### Fail-Closed ScoreResult
```python
@dataclass
class ScoreResult:
    scores: dict[str, float]   # 성공한 것만
    parse_method: str          # "json"|"regex"|"single"|"failed"
    missing: list[str]         # 실패한 카테고리
```
- None 반환 = 파싱 완전 실패 → degraded 모드 진입
- 5.0 기본값 사용 안 함

### Weights 마이그레이션
```python
if isinstance(raw_weights, list):   # v1
    return dict(zip(categories, raw_weights))
elif isinstance(raw_weights, dict): # v2
    return raw_weights
```
