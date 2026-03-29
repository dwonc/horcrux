# Horcrux 모델 성능 실험 리포트

## Opus 4.6 vs Sonnet 4.6 - 모드별 비교

**실험일**: 2026-03-29
**총 실행**: 24회 (Phase 1: 6회, Phase 2A: 6회, Phase 2B: 6회, 중복 포함)
**가중치 설정**: core_weight=0.6, aux_weight=0.4

## 전체 점수 매트릭스

| 조합 | easy | medium | hard | **평균** |
|------|------|--------|------|---------|
| Opus+standard | 7.5 | 6.5 | 5.5 | 6.5 |
| **Opus+full** | **7.5** | 7.5 | **8.2** | **7.7** |
| Sonnet+standard | 4.0 | 5.5 | 5.0 | 4.8 |
| **Sonnet+full** | 7.5 | **8.5** | 7.5 | **7.5** |

## Phase 1: Standard 모드 비교 (Opus vs Sonnet)

| 태스크 | 난이도 | Opus | Sonnet | 차이 |
|--------|--------|------|--------|------|
| T06 React 리팩토링 | easy | **7.5** | 4.0 | +3.5 |
| T01 Flask Service Layer | medium | **6.5** | 5.5 | +1.0 |
| T05 JWT 보안 감사 | hard | **5.5** | 5.0 | +0.5 |
| **평균** | | **6.5** | **4.8** | **+1.7** |

**결론**: Standard 모드에서는 Opus가 확실히 우위. 난이도 높을수록 격차 감소.

## Phase 2A: Full 모드 비교 (Opus vs Sonnet)

| 태스크 | 난이도 | Opus | Sonnet | 차이 |
|--------|--------|------|--------|------|
| T06 React 리팩토링 | easy | 7.5 | 7.2 | +0.3 |
| T01 Flask Service Layer | medium | 7.5 | **8.5** | **-1.0** |
| T05 JWT 보안 감사 | hard | **8.2** | 7.5 | +0.7 |
| **평균** | | **7.7** | **7.7** | **0.0** |

**결론**: Full 모드에서는 Opus와 Sonnet이 **동점**. 오케스트레이션이 모델 차이를 완전히 상쇄.
Medium에서는 Sonnet이 오히려 역전 — 비판-개선 루프의 개선 여지가 더 큼.

## Phase 2B: Sonnet+full vs Opus+standard (크로스 비교)

| 태스크 | 난이도 | Sonnet+full | Opus+std | 차이 |
|--------|--------|------------|----------|------|
| T06 React 리팩토링 | easy | **7.5** | 5.5 | +2.0 |
| T01 Flask Service Layer | medium | **7.5** | 5.5 | +2.0 |
| T05 JWT 보안 감사 | hard | 7.5 | **8.5** | -1.0 |
| **평균** | | **7.5** | **6.5** | **+1.0** |

**결론**: Sonnet+full이 Opus+standard를 평균 1.0점 앞섬. **비싼 모델보다 깊은 오케스트레이션이 더 효과적**.
단, hard 태스크에서는 Opus+standard(8.5)가 이례적 고득점.

## 핵심 발견

### 1. 오케스트레이션 깊이 > 모델 성능
- Standard → Full 전환 시: Opus +1.2, Sonnet +2.7
- Sonnet이 오케스트레이션 이점을 더 많이 받음 (개선 여지가 크기 때문)

### 2. Standard 모드의 존재 이유가 약함
- Standard 평균: 5.7 (Opus 6.5 + Sonnet 4.8) / 2
- Full 평균: 7.7 (Opus 7.7 + Sonnet 7.7) / 2
- 차이: +2.0점, 시간은 3분 vs 10분

### 3. 비용 효율성
| 조합 | 평균 점수 | 예상 비용/회 | 비용 대비 품질 |
|------|----------|-------------|--------------|
| Opus+full | 7.7 | ~$0.80 | 최고 품질 |
| Sonnet+full | 7.5 | ~$0.08 | **최고 가성비** |
| Opus+standard | 6.5 | ~$0.40 | 비효율적 |
| Sonnet+standard | 4.8 | ~$0.04 | 품질 부족 |

### 4. Sonnet이 Full에서 역전하는 이유
- Opus는 첫 생성부터 완성도가 높아 Critics의 지적이 제한적
- Sonnet은 초기 품질이 낮지만, 구체적인 비판을 많이 받아 Synthesizer가 크게 개선
- **비판-개선 루프의 ROI가 Sonnet에서 더 높음**

## 코드 반영 사항

### 1. 자동 라우팅 최적화 (`_route_intent_to_engine`)
- **auto 모드 기본값을 standard에서 full로 변경**
- fast는 `code_fix + small + low_risk`에서만 유지
- feature_add, code_fix(복잡) → 모두 full로 라우팅

### 2. Sonnet 보정 (`apply_sonnet_compensation`)
- Sonnet + hard → full 자동 승격
- Sonnet + easy/medium → Opus 추천 경고

### 3. 최적화 정책 파일 (`.horcrux/optimization.md`)
- 실험 데이터 기반 라우팅 룰 문서화
- classifier가 참조하는 정책 파일

## 한계 및 추후 과제

- 표본: 3태스크 × 4조합 (방향성은 명확하나 통계적 유의성 보완 필요)
- T05 hard에서 Opus+standard(8.5)가 이례적 고득점 — 재현성 확인 필요
- 토큰 소비량 미측정 (예상치 기반)
- planning_pipeline, deep_refactor 모드는 미실험
