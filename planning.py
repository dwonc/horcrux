"""
Horcrux — Planning Pipeline v1.1
4-Phase Multi-Model Planning:
  Phase 1: Generator (3 models, parallel) → 독립적 기획안 생성
  Phase 2: Synthesizer (Opus) → 3개 안을 통합
  Phase 3: Critic (3 models, parallel) → 통합안 비판/검증
  Phase 4: Final Polish (Codex) → 피드백 반영 최종본

v1.1 변경: task의 출력 형식 요청을 우선 존중하도록 프롬프트 개선.
  - task가 "아이디어 20개 리스트" 같은 구체적 산출물을 요구하면 그 형식으로 출력
  - task가 별도 형식을 요구하지 않으면 기존 전략 문서 JSON으로 출력
"""

import json
import os
import re
import time
import threading
import concurrent.futures
from datetime import datetime
from pathlib import Path

# ─── 이 파일은 server.py에서 import 되므로, server.py의 함수를 직접 참조 ───
_call_claude = None
_call_codex = None
_call_gemini = None
_call_aux_critic = None
_AUX_CRITIC_ENDPOINTS = None
_extract_json = None
_extract_score = None
_LOG_DIR = None


def inject_callers(call_claude, call_codex, call_gemini,
                   call_aux_critic, aux_endpoints,
                   extract_json_fn, extract_score_fn, log_dir):
    """server.py에서 호출하여 의존성 주입"""
    global _call_claude, _call_codex, _call_gemini
    global _call_aux_critic, _AUX_CRITIC_ENDPOINTS
    global _extract_json, _extract_score, _LOG_DIR
    _call_claude = call_claude
    _call_codex = call_codex
    _call_gemini = call_gemini
    _call_aux_critic = call_aux_critic
    _AUX_CRITIC_ENDPOINTS = aux_endpoints
    _extract_json = extract_json_fn
    _extract_score = extract_score_fn
    _LOG_DIR = Path(log_dir)


# ═══════════════════════════════════════════
# PROMPTS (v1.1 — task의 출력 형식 우선)
# ═══════════════════════════════════════════

PLAN_GENERATOR_PROMPT = """You are a {role} expert.

Task:
{task}

CRITICAL INSTRUCTION: Read the task carefully. If the task specifies a desired output format (e.g. "list 20 ideas with these fields", "give me a table of X"), you MUST produce output in EXACTLY that format. The task's requested format overrides everything below.

If the task does NOT specify an output format, then think from your {role} perspective and reply JSON:
{{"plan_title":"<title>","executive_summary":"<2-3 sentences>","key_goals":["g1","g2","g3"],"approach":"<detailed approach>","milestones":[{{"name":"<n>","description":"<desc>","duration":"<time>"}}],"risks":[{{"risk":"<risk>","mitigation":"<mitigation>"}}],"trade_offs":["t1","t2"],"unique_insights":["insight from your {role} perspective"]}}

Remember: task's output format comes first. Be thorough and detailed. Write in the same language as the task."""

PLAN_SYNTHESIZER_PROMPT = """You are a master strategist. Three experts have independently created outputs for the same task.
Your job is to synthesize the BEST parts of all three into a single, superior result.

Task:
{task}

=== Plan A (Strategic/Business perspective) ===
{plan_a}

=== Plan B (Technical/Feasibility perspective) ===
{plan_b}

=== Plan C (User/Market perspective) ===
{plan_c}

CRITICAL INSTRUCTION: Read the original task carefully. Your synthesized output MUST follow the output format the task requests. If the task asks for "20 ideas as a list", produce exactly 20 ideas as a list — NOT a strategy document.

Instructions:
1. Identify the strongest elements from each plan
2. Resolve any contradictions between plans
3. Create a unified result that is better than any individual one
4. Keep all items/ideas that are strong from any plan, remove duplicates and weak ones
5. If the task asks for N items, produce exactly N items

Write in the same language as the task. Be thorough and complete — do NOT truncate or summarize."""

PLAN_CRITIC_PROMPT = """You are a {role} critic. Critically evaluate this output.

Task:
{task}

Output to evaluate:
{merged_plan}

Analyze from your {role} perspective:
1. Does the output match what the task asked for? (format, quantity, content)
2. What gaps or blind spots exist?
3. What items are weak and should be replaced?
4. What would you add or change?
5. Rate the output overall.

Reply JSON only:
{{"overall_score":<1-10>,"scores":{{"relevance":<1-10>,"completeness":<1-10>,"quality":<1-10>,"diversity":<1-10>}},"gaps":["gap1","gap2"],"weak_items":["which items should be improved and why"],"suggested_additions":["addition1"],"suggested_changes":["change1"],"strengths":["strength1"],"critical_issues":["issue that MUST be addressed"]}}"""

PLAN_FINAL_POLISH_PROMPT = """You are a senior expert. Take this output and improve it based on multi-perspective critique feedback.

Task:
{task}

Current Output:
{merged_plan}

=== Critique Feedback ===
{critique_feedback}

CRITICAL INSTRUCTION:
1. Read the original task carefully — your final output MUST match the format and structure the task requests.
2. Address every critical issue raised by critics.
3. Replace weak items identified by critics with better ones.
4. Fill identified gaps.
5. Preserve the output's strengths.
6. Produce a COMPLETE final result — do NOT truncate, summarize, or cut short.
7. If the task asks for 20 items, output exactly 20 items in full.

Write in the same language as the task. Be thorough — this is the final deliverable."""


# ═══════════════════════════════════════════
# PLANNING STATE
# ═══════════════════════════════════════════

plannings = {}  # planning_id → state


# ═══════════════════════════════════════════
# CORE PIPELINE
# ═══════════════════════════════════════════

def run_planning(planning_id: str, task: str, claude_model: str = ""):
    """
    4-Phase Planning Pipeline:
      Phase 1: 3 models generate plans in parallel
      Phase 2: Opus synthesizes into one plan
      Phase 3: 3 models critique in parallel
      Phase 4: Codex produces final polished plan
    """
    state = plannings[planning_id]
    GEN_TIMEOUT = 900    # 15min for generation
    SYNTH_TIMEOUT = 900
    CRITIC_TIMEOUT = 600
    POLISH_TIMEOUT = 900

    try:
        # ═══════════════════════════════════════
        # Phase 1: Generator (3 models, parallel)
        # ═══════════════════════════════════════
        state["phase"] = "generating"
        state["phase_detail"] = "3 models generating plans in parallel"

        roles = {
            "claude": "business strategist and system architect",
            "codex": "senior developer and technical lead",
            "gemini": "product manager and user advocate",
        }

        prompts = {
            name: PLAN_GENERATOR_PROMPT.format(task=task, role=role)
            for name, role in roles.items()
        }

        # 병렬 실행
        role_label = {
            "claude": "Strategic (Claude Opus)",
            "codex": "Technical (Codex)",
            "gemini": "User/Market (Gemini)",
        }
        parsed_plans = {}
        plan_displays = {}

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            future_to_name = {
                pool.submit(_call_claude, prompts["claude"], GEN_TIMEOUT, claude_model): "claude",
                pool.submit(_call_codex, prompts["codex"], GEN_TIMEOUT): "codex",
                pool.submit(_call_gemini, prompts["gemini"], GEN_TIMEOUT): "gemini",
            }

            for future in concurrent.futures.as_completed(future_to_name):
                if state.get("abort"): break
                name = future_to_name[future]
                raw = future.result()
                parsed = _extract_json(raw)
                parsed_plans[name] = parsed
                if parsed:
                    plan_displays[name] = json.dumps(parsed, indent=2, ensure_ascii=False)
                else:
                    plan_displays[name] = raw  # 전체 보존

                state["messages"].append({
                    "role": "generator",
                    "model": name,
                    "label": role_label[name],
                    "content": plan_displays[name],
                    "ts": datetime.now().isoformat(),
                })
                state["phase_detail"] = f"{len(plan_displays)}/3 plans generated ({name} done)"
                _log(planning_id, "generator_done", {"model": name, "done": len(plan_displays)})

        if state.get("abort"):
            state["status"] = "aborted"; return

        state["plans_raw"] = plan_displays
        _log(planning_id, "phase1_complete", {
            "plans_generated": len(parsed_plans),
            "models": list(plan_displays.keys()),
        })

        # ═══════════════════════════════════════
        # Phase 2: Synthesizer (Opus)
        # ═══════════════════════════════════════
        if state.get("abort"):
            state["status"] = "aborted"; return

        state["phase"] = "synthesizing"
        state["phase_detail"] = "Opus merging 3 plans into 1"

        synth_prompt = PLAN_SYNTHESIZER_PROMPT.format(
            task=task,
            plan_a=plan_displays.get("claude", "N/A"),
            plan_b=plan_displays.get("codex", "N/A"),
            plan_c=plan_displays.get("gemini", "N/A"),
        )

        synth_raw = _call_claude(synth_prompt, SYNTH_TIMEOUT, claude_model)
        synth_parsed = _extract_json(synth_raw)
        synth_display = json.dumps(synth_parsed, indent=2, ensure_ascii=False) if synth_parsed else synth_raw

        state["messages"].append({
            "role": "synthesizer",
            "model": "Claude Opus",
            "content": synth_display,
            "ts": datetime.now().isoformat(),
        })
        state["merged_plan"] = synth_display

        _log(planning_id, "phase2_complete", {"merged": bool(synth_parsed)})

        # ═══════════════════════════════════════
        # Phase 3: Critic (3 models, parallel)
        # ═══════════════════════════════════════
        if state.get("abort"):
            state["status"] = "aborted"; return

        state["phase"] = "critiquing"
        state["phase_detail"] = "3 models critiquing merged plan"

        critic_roles = {
            "claude": "strategic and business viability",
            "codex": "technical feasibility and implementation",
            "gemini": "user experience and market fit",
        }

        critic_prompts = {
            name: PLAN_CRITIC_PROMPT.format(
                task=task, merged_plan=synth_display, role=role
            )
            for name, role in critic_roles.items()
        }

        critic_role_label = {
            "claude": "Strategic Critic (Claude)",
            "codex": "Technical Critic (Codex)",
            "gemini": "UX/Market Critic (Gemini)",
        }
        all_critiques = {}
        all_scores = []

        available_aux = [
            ep for ep in (_AUX_CRITIC_ENDPOINTS or [])
            if os.environ.get(ep[2])
        ] if _AUX_CRITIC_ENDPOINTS and _call_aux_critic else []

        aux_critic_prompt = PLAN_CRITIC_PROMPT.format(
            task=task, merged_plan=synth_display,
            role="independent reviewer with fresh eyes",
        )

        total_critics = 3 + len(available_aux)
        done_critics = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(total_critics, 3)) as pool:
            future_to_name = {
                pool.submit(_call_claude, critic_prompts["claude"], CRITIC_TIMEOUT, claude_model): ("claude", "core"),
                pool.submit(_call_codex, critic_prompts["codex"], CRITIC_TIMEOUT): ("codex", "core"),
                pool.submit(_call_gemini, critic_prompts["gemini"], CRITIC_TIMEOUT): ("gemini", "core"),
            }
            for ep_name, base, env_key, model, extra_h in available_aux:
                f = pool.submit(_call_aux_critic, ep_name, base, env_key, model, extra_h, aux_critic_prompt)
                future_to_name[f] = (ep_name, "aux")

            for future in concurrent.futures.as_completed(future_to_name):
                if state.get("abort"): break
                name, tier = future_to_name[future]
                done_critics += 1

                if tier == "aux":
                    result_name, raw = future.result()
                    if not raw:
                        continue
                else:
                    raw = future.result()

                parsed = _extract_json(raw)
                score = 5.0
                if parsed:
                    score = parsed.get("overall_score", parsed.get("overall", 5.0))
                    try: score = float(score)
                    except: score = 5.0
                all_scores.append(score)
                all_critiques[name] = {
                    "parsed": parsed,
                    "score": score,
                    "display": json.dumps(parsed, indent=2, ensure_ascii=False) if parsed else (raw if isinstance(raw, str) else str(raw)),
                }

                label = critic_role_label.get(name, f"Aux Critic ({name})")
                state["messages"].append({
                    "role": "critic",
                    "model": name,
                    "label": label,
                    "score": score,
                    "content": all_critiques[name]["display"],
                    "ts": datetime.now().isoformat(),
                })
                state["phase_detail"] = f"{done_critics}/{total_critics} critics done ({name}: {score}/10)"

        if state.get("abort"):
            state["status"] = "aborted"; return

        avg_score = sum(all_scores) / len(all_scores) if all_scores else 5.0
        state["avg_score"] = round(avg_score, 1)

        _log(planning_id, "phase3_complete", {
            "critics": len(all_critiques),
            "scores": {k: v["score"] for k, v in all_critiques.items()},
            "avg_score": avg_score,
        })

        # ═══════════════════════════════════════
        # Phase 4: Final Polish (Codex)
        # ═══════════════════════════════════════
        if state.get("abort"):
            state["status"] = "aborted"; return

        state["phase"] = "polishing"
        state["phase_detail"] = "Codex producing final output with critique feedback"

        critique_feedback_parts = []
        for name, data in all_critiques.items():
            parsed = data.get("parsed", {})
            if parsed:
                fb = f"[{name}] Score: {data['score']}/10\n"
                fb += f"  Gaps: {parsed.get('gaps', [])}\n"
                fb += f"  Weak items: {parsed.get('weak_items', [])}\n"
                fb += f"  Additions: {parsed.get('suggested_additions', [])}\n"
                fb += f"  Changes: {parsed.get('suggested_changes', [])}\n"
                fb += f"  Critical: {parsed.get('critical_issues', [])}\n"
                critique_feedback_parts.append(fb)
            else:
                critique_feedback_parts.append(f"[{name}] {data['display'][:3000]}")

        critique_feedback = "\n---\n".join(critique_feedback_parts)

        polish_prompt = PLAN_FINAL_POLISH_PROMPT.format(
            task=task,
            merged_plan=synth_display,
            critique_feedback=critique_feedback,
        )

        polish_raw = _call_codex(polish_prompt, POLISH_TIMEOUT)
        polish_parsed = _extract_json(polish_raw)
        polish_display = json.dumps(polish_parsed, indent=2, ensure_ascii=False) if polish_parsed else polish_raw

        state["messages"].append({
            "role": "final",
            "model": "Codex",
            "content": polish_display,
            "ts": datetime.now().isoformat(),
        })

        state["final_plan"] = polish_display
        state["final_solution"] = polish_display
        state["status"] = "completed"

        _log(planning_id, "phase4_complete", {
            "final_parsed": bool(polish_parsed),
            "avg_critic_score": avg_score,
        })

    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)
        import traceback
        traceback.print_exc()

    if state.get("abort"):
        state["status"] = "aborted"

    state["finished_at"] = datetime.now().isoformat()

    if _LOG_DIR:
        log_file = _LOG_DIR / f"{planning_id}.json"
        with open(log_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)


def _run_aux_critics(task: str, merged_plan: str) -> dict:
    """Available Aux critics를 병렬로 실행"""
    if not _AUX_CRITIC_ENDPOINTS or not _call_aux_critic:
        return {}

    prompt = PLAN_CRITIC_PROMPT.format(
        task=task,
        merged_plan=merged_plan,
        role="independent reviewer with fresh eyes",
    )

    available = [
        ep for ep in _AUX_CRITIC_ENDPOINTS
        if os.environ.get(ep[2])
    ]

    if not available:
        return {}

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(available)) as pool:
        futures = {}
        for name, base, env_key, model, extra_h in available:
            f = pool.submit(_call_aux_critic, name, base, env_key, model, extra_h, prompt)
            futures[f] = name

        for f in concurrent.futures.as_completed(futures):
            name = futures[f]
            try:
                result_name, raw = f.result()
                if raw:
                    results[result_name] = raw
            except Exception as e:
                print(f"[PLANNING] Aux critic {name} error: {e}")

    return results


def _log(planning_id: str, event: str, data: dict):
    """내부 로깅"""
    print(f"  [PLANNING:{planning_id}] {event}: {json.dumps(data, ensure_ascii=False)[:200]}")
