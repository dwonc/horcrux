# Horcrux Model Experiment Report

## Opus 4.6 vs Sonnet 4.6 — Standard Mode

**Date**: 2026-03-29
**Mode**: standard (pair gen + synth + core critic + revision, max 2 rounds)
**Config**: core_weight=0.6, aux_weight=0.4

## Results

| Task | Category | Difficulty | Opus | Sonnet | Delta | Opus Time | Sonnet Time |
|------|----------|-----------|------|--------|-------|-----------|-------------|
| T06 | React refactor | easy | **7.5** | 4.0 | +3.5 | 263s | 205s |
| T01 | Flask Service Layer | medium | **6.5** | 5.5 | +1.0 | 160s | 178s |
| T05 | JWT security audit | hard | **5.5** | 5.0 | +0.5 | 153s | 96s |
| **AVG** | | | **6.5** | **4.8** | **+1.7** | **192s** | **160s** |

## Key Findings

### 1. Opus is consistently better (+1.7 avg)
All three difficulty levels showed Opus outperforming Sonnet. The gap is significant (>0.5 threshold).

### 2. Orchestration compensates more for hard tasks
| Difficulty | Gap | Interpretation |
|-----------|-----|----------------|
| easy | 3.5 | Orchestration barely helps Sonnet on simple tasks |
| medium | 1.0 | Moderate compensation from critic-revision loop |
| hard | 0.5 | Strong compensation — 5 Critics + Revision nearly closes the gap |

### 3. Cost-effectiveness
| Model | Avg Score | Est. Cost/Run | Score per $ |
|-------|-----------|---------------|-------------|
| Opus | 6.5 | ~$0.40 | 16.3 |
| Sonnet | 4.8 | ~$0.04 | 120.0 |

Sonnet is 7x more cost-effective per point, but the absolute quality gap matters for critical tasks.

### 4. Latency
Sonnet is ~17% faster on average (160s vs 192s), as expected from smaller model inference.

## Hypothesis Evaluation

| Hypothesis | Result |
|-----------|--------|
| H1: Orchestration offsets model quality gap | **Partially true** — only for hard tasks (gap 0.5), not for easy (gap 3.5) |
| H2: Sonnet + free Aux is cost-effective vs Opus alone | **True for hard tasks** — 5.0 vs 5.5 at 1/10 cost is acceptable |
| H3: Core model affects internal behavior patterns | **Not enough data** — both converged in 2 rounds, need full mode experiment |

## Actions Taken

Based on these results, `apply_sonnet_compensation()` was added to the classifier:

1. **Sonnet + hard task** (refactor/security/architecture/MSA/production)
   - Auto-upgrade from standard/fast to **full mode**
   - Deeper orchestration (more Critics + Revision rounds) compensates for model gap

2. **Sonnet + easy/medium task**
   - Warning in routing reason: "Opus recommended for easy/medium tasks (+1.0~3.5 score)"
   - No mode change (user's choice respected)

3. **Opus** — no changes applied

## Limitations

- Sample size: 3 tasks x 2 models = 6 runs (directional, not statistically significant)
- Standard mode only (full mode may show different patterns)
- No token cost measurement (estimated from public pricing)
- Single execution per combination (no variance measurement)

## Future Experiments

- Full mode comparison (does deeper orchestration further close the gap?)
- Sonnet with full mode vs Opus with standard mode (is compensated Sonnet competitive?)
- Token consumption tracking per run
- 10+ tasks for statistical significance
