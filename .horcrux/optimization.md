# Horcrux 최적 라우팅 정책

> 이 파일은 실험 데이터 기반으로 자동 라우팅을 결정합니다.
> classifier가 이 파일을 참조하여 모드/모델 조합을 최적화합니다.
> 마지막 업데이트: 2026-03-29

## 실험 결과 요약

### 전체 점수 매트릭스 (3태스크 × 4조합, 총 24회 실행)

| 조합 | easy | medium | hard | 평균 |
|------|------|--------|------|------|
| Opus+standard | 5.5 | 5.5 | 8.5 | 6.5 |
| Opus+full | 7.5 | 7.5 | 8.2 | 7.7 |
| Sonnet+standard | 4.0 | 5.5 | 5.0 | 4.8 |
| Sonnet+full | 7.5 | 8.5 | 7.5 | 7.5 |

### 핵심 발견
- full 모드의 오케스트레이션이 모델 성능 차이를 거의 상쇄 (Opus+full 7.7 ≈ Sonnet+full 7.5)
- Sonnet+full(7.5)이 Opus+standard(6.5)보다 우수 — 모델보다 오케스트레이션 깊이가 중요
- standard 모드는 full 대비 1~2점 낮아 가성비 떨어짐
- fast 모드는 간단한 작업에만 적합 (평균 5.0)

## 자동 라우팅 정책

### 기본 원칙
- **standard 모드를 기본에서 제외** — full 또는 fast만 사용
- **모델 선택보다 모드 선택이 품질에 더 큰 영향**

### 라우팅 룰

```
intent=code_fix AND scope=small AND risk=low
  → fast 모드 (모델 무관, 30초)
  → 근거: 간단한 수정에 full은 오버킬

intent=code_fix AND (scope!=small OR risk!=low)
  → full 모드 (모델 무관)
  → 근거: 복잡한 버그는 멀티모델 검증 필요

intent=refactor OR intent=deep_refactor
  → full 모드 (모델 무관)
  → 근거: full에서 Opus/Sonnet 차이 0.2 (무의미)

intent=brainstorm OR intent=artifact OR intent=document
  → planning_pipeline (모델 무관)
  → 근거: planning은 별도 4-phase 파이프라인

intent=feature_add
  → full 모드 (모델 무관)
  → 근거: standard(5.5) vs full(7.5) 차이 2.0

intent=self_improve
  → self_improve 엔진 유지

mode=parallel (사용자 명시)
  → pair_generation 유지

mode=deep_refactor (사용자 명시)
  → deep_refactor 유지
```

### 모델 추천 (참고용, 강제 아님)
- **비용 절감 우선**: Sonnet + full → 평균 7.5, Opus 대비 1/10 비용
- **최고 품질 우선**: Opus + full → 평균 7.7, 특히 hard(8.2)에서 강점
- **속도 우선**: 아무 모델 + fast → 30초, 평균 5.0

### 폐기 대상
- `adaptive_standard` 엔진: full 대비 열등, 시간 절약도 미미 (3분 vs 10분)
  - 단, 사용자가 명시적으로 standard를 요청하면 존중
