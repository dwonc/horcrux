"""
Debate Chain Orchestrator
3-AI 디베이트 체인: Claude + Codex + Gemini
수렴 기반 루프 (max 10 rounds, threshold 8.0)
"""

import json
import subprocess
import os
import sys
import time
import re
from datetime import datetime
from pathlib import Path

# Gemini API
try:
    import google.generativeai as genai
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False
    print("[WARN] google-generativeai not installed. Run: pip install google-generativeai")


CONFIG_PATH = Path(__file__).parent / "config.json"
LOG_DIR = Path(__file__).parent / "logs"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def log_round(session_id: str, round_num: int, data: dict):
    """각 라운드 결과를 JSON 로그로 저장"""
    log_file = LOG_DIR / f"{session_id}.jsonl"
    entry = {"round": round_num, "timestamp": datetime.now().isoformat(), **data}
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ─── AI 호출 함수들 ───

def call_claude(prompt: str, timeout: int = 120) -> str:
    """Claude Code CLI 호출"""
    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace"
        )
        if result.returncode != 0:
            return f"[ERROR] Claude CLI failed: {result.stderr[:500]}"
        return result.stdout.strip()
    except FileNotFoundError:
        return "[ERROR] 'claude' CLI not found. Install: npm install -g @anthropic-ai/claude-code"
    except subprocess.TimeoutExpired:
        return "[ERROR] Claude CLI timed out"


def call_codex(prompt: str, timeout: int = 120) -> str:
    """Codex CLI 호출 (OpenAI)"""
    try:
        result = subprocess.run(
            ["codex", "-p", prompt],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace"
        )
        if result.returncode != 0:
            return f"[ERROR] Codex CLI failed: {result.stderr[:500]}"
        return result.stdout.strip()
    except FileNotFoundError:
        return "[ERROR] 'codex' CLI not found. Install: npm install -g @openai/codex"
    except subprocess.TimeoutExpired:
        return "[ERROR] Codex CLI timed out"


def call_gemini(prompt: str) -> str:
    """Gemini API 호출"""
    if not HAS_GEMINI:
        return "[ERROR] google-generativeai not installed"
    
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return "[ERROR] GEMINI_API_KEY not set"
    
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        return f"[ERROR] Gemini API failed: {str(e)[:500]}"


# ─── 디베이트 루프 ───

def parse_score(text: str) -> float:
    """응답에서 점수 추출 (0-10)"""
    patterns = [
        r"(?:score|점수|rating)[:\s]*(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*/\s*10",
        r"\b(\d+(?:\.\d+)?)\b"
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            score = float(match.group(1))
            if 0 <= score <= 10:
                return score
    return 5.0  # 기본값


def run_debate(task: str, config: dict = None) -> dict:
    """
    메인 디베이트 루프
    1. Generator (Claude) → 초안 생성
    2. Critic (Codex) → 비판 + 점수
    3. Verifier (Gemini) → 검증 + 점수
    4. Synthesizer (Claude) → 종합 개선
    5. 수렴 확인 → 반복 or 종료
    """
    if config is None:
        config = load_config()
    
    threshold = config.get("threshold", 8.0)
    max_rounds = config.get("max_rounds", 10)
    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    print(f"\n{'='*60}")
    print(f"  Debate Chain Started")
    print(f"  Task: {task[:80]}...")
    print(f"  Threshold: {threshold} | Max Rounds: {max_rounds}")
    print(f"  Session: {session_id}")
    print(f"{'='*60}\n")
    
    current_solution = ""
    history = []
    
    for round_num in range(1, max_rounds + 1):
        print(f"\n--- Round {round_num}/{max_rounds} ---")
        round_data = {"task": task}
        
        # Step 1: Generator (Claude)
        if round_num == 1:
            gen_prompt = f"You are an expert developer. Solve this task thoroughly:\n\n{task}"
        else:
            gen_prompt = (
                f"Previous solution:\n{current_solution}\n\n"
                f"Critic feedback:\n{history[-1].get('critic', '')}\n\n"
                f"Verifier feedback:\n{history[-1].get('verifier', '')}\n\n"
                f"Improve the solution based on ALL feedback. Task:\n{task}"
            )
        
        print("  [1/4] Generator (Claude)...")
        gen_response = call_claude(gen_prompt)
        round_data["generator"] = gen_response
        print(f"         → {len(gen_response)} chars")
        
        # Step 2: Critic (Codex)
        critic_prompt = (
            f"You are a senior code reviewer. Critically review this solution.\n"
            f"Find bugs, edge cases, performance issues, and improvements.\n"
            f"Give a score out of 10.\n\n"
            f"Task: {task}\n\nSolution:\n{gen_response}"
        )
        
        print("  [2/4] Critic (Codex)...")
        critic_response = call_codex(critic_prompt)
        critic_score = parse_score(critic_response)
        round_data["critic"] = critic_response
        round_data["critic_score"] = critic_score
        print(f"         → Score: {critic_score}/10")
        
        # Step 3: Verifier (Gemini)
        verifier_prompt = (
            f"You are a QA engineer. Verify this solution for correctness.\n"
            f"Check logic, completeness, and real-world applicability.\n"
            f"Give a score out of 10.\n\n"
            f"Task: {task}\n\nSolution:\n{gen_response}\n\nCritic says:\n{critic_response}"
        )
        
        print("  [3/4] Verifier (Gemini)...")
        verifier_response = call_gemini(verifier_prompt)
        verifier_score = parse_score(verifier_response)
        round_data["verifier"] = verifier_response
        round_data["verifier_score"] = verifier_score
        print(f"         → Score: {verifier_score}/10")
        
        # 가중 평균 점수
        weights = config.get("scoring", {}).get("weights", [0.3, 0.25, 0.2, 0.25])
        avg_score = (critic_score + verifier_score) / 2
        round_data["avg_score"] = avg_score
        print(f"         → Avg Score: {avg_score}/10")
        
        # Step 4: Synthesizer (Claude) — 수렴 안 됐을 때만
        if avg_score >= threshold:
            print(f"\n  ✅ Converged! Score {avg_score} >= {threshold}")
            current_solution = gen_response
            round_data["converged"] = True
            log_round(session_id, round_num, round_data)
            history.append(round_data)
            break
        
        synth_prompt = (
            f"Synthesize the best solution from all feedback.\n\n"
            f"Original task: {task}\n\n"
            f"Current solution:\n{gen_response}\n\n"
            f"Critic ({critic_score}/10):\n{critic_response}\n\n"
            f"Verifier ({verifier_score}/10):\n{verifier_response}\n\n"
            f"Create an improved, final solution addressing ALL issues."
        )
        
        print("  [4/4] Synthesizer (Claude)...")
        synth_response = call_claude(synth_prompt)
        current_solution = synth_response
        round_data["synthesizer"] = synth_response
        round_data["converged"] = False
        print(f"         → {len(synth_response)} chars")
        
        log_round(session_id, round_num, round_data)
        history.append(round_data)
    
    # 최종 결과
    result = {
        "session_id": session_id,
        "task": task,
        "rounds": len(history),
        "final_score": history[-1].get("avg_score", 0),
        "converged": history[-1].get("converged", False),
        "final_solution": current_solution,
        "history": history
    }
    
    # 최종 결과 저장
    result_file = LOG_DIR / f"{session_id}_result.json"
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    
    print(f"\n{'='*60}")
    print(f"  Result: {'CONVERGED ✅' if result['converged'] else 'MAX ROUNDS ⚠️'}")
    print(f"  Rounds: {result['rounds']} | Final Score: {result['final_score']}")
    print(f"  Log: {result_file}")
    print(f"{'='*60}\n")
    
    return result


# ─── CLI ───

def main():
    if len(sys.argv) < 2:
        print("Usage: python orchestrator.py <task>")
        print("  or:  python orchestrator.py --file <task_file.txt>")
        sys.exit(1)
    
    if sys.argv[1] == "--file":
        with open(sys.argv[2], "r", encoding="utf-8") as f:
            task = f.read().strip()
    else:
        task = " ".join(sys.argv[1:])
    
    result = run_debate(task)
    
    if result["converged"]:
        print("\n📋 Final Solution:\n")
        print(result["final_solution"])


if __name__ == "__main__":
    main()
