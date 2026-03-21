"""
Debate Chain Web Server v6
- Claude + Codex 2-AI debate (Gemini removed from review)
- Pair2/Pair3 parallel generation
- MCP server ready
"""
import json
import subprocess
import os
import re
import threading
import tempfile
import concurrent.futures
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# --- Gemini model fallback (for pair3 only) ---
GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-pro",
    "gemini-1.5-flash",
]
_gemini_current_model_idx = 0
_gemini_lock = threading.Lock()

# --- Prompts ---

GENERATOR_PROMPT = """Task: {task}

Reply JSON only:
{{"solution":"<complete code/text>","approach":"<1 sentence>","decisions":["d1","d2"]}}"""

GENERATOR_IMPROVE_PROMPT = """Task: {task}

Current solution:
{solution}

Fix these issues:
{issues}

Reply JSON only:
{{"solution":"<improved complete solution>","approach":"<1 sentence>","changes":["fix1","fix2"]}}"""

CRITIC_PROMPT = """Task: {task}

Solution:
{solution}

You are a ruthless code reviewer. Find EVERY flaw. Be strict. Score 1-10.
Review: bugs, edge cases, security, performance, code quality.
Reply JSON only:
{{"score":<1-10>,"summary":"<2 sentences>","issues":[{{"sev":"critical|major|minor","desc":"<issue>","fix":"<suggestion>"}}],"strengths":["s1"],"on_task":true/false}}"""

SYNTHESIZER_PROMPT = """Task: {task}

Solution:
{solution}

Issues to fix:
{issues}

Produce improved COMPLETE solution addressing every issue.
Reply JSON only:
{{"solution":"<complete improved solution>","approach":"<1 sentence>","fixed":["issue1->fix","issue2->fix"],"remaining":["concern1"]}}"""


# --- JSON parsing ---

def extract_json(text):
    if not text or "[ERROR]" in text:
        return None
    cleaned = re.sub(r'```(?:json)?\s*', '', text).replace('```', '').strip()
    try:
        return json.loads(cleaned)
    except:
        pass
    depth = 0
    start = -1
    for i, c in enumerate(cleaned):
        if c == '{':
            if depth == 0: start = i
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                try: return json.loads(cleaned[start:i+1])
                except: start = -1
    return None


def format_issues_compact(issues_list):
    if not issues_list:
        return "None."
    lines = []
    for i, iss in enumerate(issues_list, 1):
        if isinstance(iss, dict):
            s = iss.get("sev", iss.get("severity", "?"))
            d = iss.get("desc", iss.get("description", str(iss)))
            fx = iss.get("fix", iss.get("suggestion", ""))
            line = f"#{i}[{s}] {d}"
            if fx:
                line += f" -> {fx}"
            lines.append(line)
        else:
            lines.append(f"#{i} {iss}")
    return "\n".join(lines)


def extract_score(data, raw_text):
    if data and "score" in data:
        try:
            s = float(data["score"])
            if 0 < s <= 10: return s
        except: pass
    for p in [r'"score"\s*:\s*(\d+(?:\.\d+)?)', r'(\d+(?:\.\d+)?)\s*/\s*10']:
        m = re.search(p, raw_text)
        if m:
            s = float(m.group(1))
            if 0 < s <= 10: return s
    return 5.0


# --- AI callers ---

def call_claude(prompt, timeout=300):
    try:
        r = subprocess.run('claude -p', input=prompt, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace", shell=True)
        out = r.stdout.strip()
        if r.returncode != 0 and not out:
            return f"[ERROR] Claude (rc={r.returncode}): {r.stderr[:500]}"
        return out if out else f"[ERROR] Claude empty: {r.stderr[:300]}"
    except subprocess.TimeoutExpired: return "[ERROR] Claude timeout"
    except Exception as e: return f"[ERROR] Claude: {str(e)[:500]}"


def call_codex(prompt, timeout=300):
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False,
                                          encoding='utf-8', dir=str(LOG_DIR)) as f:
            f.write(prompt); tmp = f.name
        r = subprocess.run(f'type "{tmp}" | codex exec --skip-git-repo-check',
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", shell=True)
        out = r.stdout.strip()
        if r.returncode != 0 and not out:
            return f"[ERROR] Codex (rc={r.returncode}): {r.stderr[:500]}"
        return out if out else f"[ERROR] Codex empty: {r.stderr[:300]}"
    except subprocess.TimeoutExpired: return "[ERROR] Codex timeout"
    except Exception as e: return f"[ERROR] Codex: {str(e)[:500]}"
    finally:
        if tmp:
            try: os.unlink(tmp)
            except: pass


def _call_gemini_with_model(prompt, model, timeout=300):
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False,
                                          encoding='utf-8', dir=str(LOG_DIR)) as f:
            f.write(prompt); tmp = f.name
        r = subprocess.run(f'type "{tmp}" | gemini --model {model}',
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", shell=True)
        out = r.stdout.strip()
        stderr = r.stderr or ""
        if r.returncode != 0 and ("quota" in stderr.lower() or "exhausted" in stderr.lower()):
            return None, "quota"
        if r.returncode != 0 and not out:
            return f"[ERROR] Gemini/{model}: {stderr[:500]}", "error"
        return (out if out else f"[ERROR] Gemini/{model} empty"), "ok"
    except subprocess.TimeoutExpired: return "[ERROR] Gemini timeout", "error"
    except Exception as e: return f"[ERROR] Gemini: {str(e)[:500]}", "error"
    finally:
        if tmp:
            try: os.unlink(tmp)
            except: pass


def call_gemini(prompt, timeout=300):
    global _gemini_current_model_idx
    for attempt in range(len(GEMINI_MODELS)):
        with _gemini_lock:
            idx = (_gemini_current_model_idx + attempt) % len(GEMINI_MODELS)
            model = GEMINI_MODELS[idx]
        result, status = _call_gemini_with_model(prompt, model, timeout)
        if status == "quota":
            with _gemini_lock:
                _gemini_current_model_idx = (idx + 1) % len(GEMINI_MODELS)
            continue
        if status == "ok":
            with _gemini_lock:
                _gemini_current_model_idx = idx
        return result
    return "[ERROR] Gemini: all models exhausted"


# --- Debate engine v6 (Claude + Codex only) ---
debates = {}


def run_debate(debate_id, task, threshold, max_rounds):
    state = debates[debate_id]
    solution = ""
    critic_data = {}

    try:
        for r in range(1, max_rounds + 1):
            if state.get("abort"): break
            state["round"] = r

            # Generator (Claude)
            state["phase"] = "generator"
            if r == 1:
                prompt = GENERATOR_PROMPT.format(task=task)
            else:
                all_issues = format_issues_compact(critic_data.get("issues", []))
                prompt = GENERATOR_IMPROVE_PROMPT.format(task=task, solution=solution, issues=all_issues)

            raw = call_claude(prompt)
            if state.get("abort"): break

            jd = extract_json(raw)
            if jd and "solution" in jd:
                solution = jd["solution"]
                disp = jd.get("approach", "") + "\n\n" + solution
                if jd.get("changes"):
                    disp += "\n\nChanges: " + " | ".join(jd["changes"])
            else:
                solution = raw
                disp = raw
            state["messages"].append({"role": "generator", "content": disp})

            # Critic (Codex GPT-5.4 only, no Gemini)
            state["phase"] = "critic"
            critic_raw = call_codex(CRITIC_PROMPT.format(task=task, solution=solution))
            if state.get("abort"): break

            critic_data = extract_json(critic_raw) or {}
            c_score = extract_score(critic_data, critic_raw)

            if critic_data:
                disp = f"{c_score}/10 - {critic_data.get('summary', '')}\n"
                if critic_data.get("issues"):
                    disp += "\nIssues:\n"
                    for iss in critic_data["issues"]:
                        if isinstance(iss, dict):
                            sev = iss.get("sev", iss.get("severity", ""))
                            ic = {"critical": "[!!]", "major": "[!]", "minor": "[.]"}.get(sev, "[?]")
                            disp += f"  {ic} {iss.get('desc', iss.get('description', ''))}\n"
                            fx = iss.get("fix", iss.get("suggestion", ""))
                            if fx:
                                disp += f"     -> {fx}\n"
                if critic_data.get("strengths"):
                    disp += "\nStrengths: " + " | ".join(critic_data["strengths"])
            else:
                disp = critic_raw
            state["messages"].append({"role": "critic", "content": disp, "score": c_score})

            state["avg_score"] = c_score

            if c_score >= threshold:
                state["status"] = "converged"
                state["final_solution"] = solution
                break

            # Synthesizer (Claude)
            if r < max_rounds:
                state["phase"] = "synthesizer"
                all_issues = format_issues_compact(critic_data.get("issues", []))
                raw = call_claude(SYNTHESIZER_PROMPT.format(task=task, solution=solution, issues=all_issues))
                if state.get("abort"): break

                jd = extract_json(raw)
                if jd and "solution" in jd:
                    solution = jd["solution"]
                    disp = jd.get("approach", "") + "\n"
                    if jd.get("fixed"):
                        disp += "\nFixed: " + "\nFixed: ".join(jd["fixed"])
                    if jd.get("remaining"):
                        disp += "\n\nRemaining: " + " | ".join(jd["remaining"])
                    disp += "\n\n" + solution
                else:
                    solution = raw
                    disp = raw
                state["messages"].append({"role": "synthesizer", "content": disp})

        if state["status"] == "running":
            state["status"] = "max_rounds"
            state["final_solution"] = solution

    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)

    if state.get("abort"):
        state["status"] = "aborted"

    state["finished_at"] = datetime.now().isoformat()
    log_file = LOG_DIR / f"{debate_id}.json"
    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# --- Pair mode: parallel code generation ---

SPLIT_PROMPT = """You are a senior architect. Split the task into {num_parts} independent parts that can be built in parallel by different developers.

Task: {task}
{extra_context}

Rules:
- Each part must be buildable independently
- Define shared interfaces/types between parts
- Parts should be roughly equal in complexity

Reply JSON only:
{{"project_name":"<n>","shared_spec":{{"types":[{{"name":"T","fields":{{"f":"type"}}}}],"interfaces":"<shared contracts>","notes":"<conventions>"}},"parts":[{{"id":"part1","title":"<short title>","description":"<what to build>"}}]}}"""

PART_PROMPT = """You are an expert developer. Build this part of a larger project.

Overall task: {task}
Your part: {part_title}
Details: {part_description}

Shared spec:
{shared_spec}

{extra_context}

Write production-quality, complete code. Reply JSON only:
{{"files":[{{"path":"<file path>","code":"<complete code>"}}],"setup":"<install/run instructions>","notes":"<integration notes>"}}"""

pairs = {}

AI_CALLERS = [
    ("Claude Opus 4.6", call_claude),
    ("Codex GPT-5.4", call_codex),
    ("Gemini", call_gemini),
]


def run_pair(pair_id, task, mode, extra_context=""):
    state = pairs[pair_id]
    num_parts = 3 if mode == "pair3" else 2

    try:
        state["phase"] = "splitting"
        ctx = ""
        if extra_context:
            ctx = f"\nAdditional context:\n{extra_context}"
        split_raw = call_claude(SPLIT_PROMPT.format(task=task, num_parts=num_parts, extra_context=ctx))
        split_json = extract_json(split_raw)

        if not split_json or "parts" not in split_json:
            state["messages"].append({"role": "architect", "model": "Claude Opus 4.6", "content": split_raw})
            state["status"] = "error"
            state["error"] = "Failed to split task"
            return

        shared_spec = json.dumps(split_json.get("shared_spec", {}), indent=2)
        parts = split_json["parts"][:num_parts]
        state["spec"] = json.dumps(split_json, indent=2)
        state["messages"].append({
            "role": "architect", "model": "Claude Opus 4.6",
            "content": json.dumps(split_json, indent=2)
        })

        if state.get("abort"):
            state["status"] = "aborted"; return

        state["phase"] = "parallel_gen"
        prompts = []
        for part in parts:
            prompts.append(PART_PROMPT.format(
                task=task,
                part_title=part.get("title", part.get("id", "")),
                part_description=part.get("description", ""),
                shared_spec=shared_spec,
                extra_context=ctx,
            ))

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_parts) as pool:
            futures = []
            for i, prompt in enumerate(prompts):
                ai_name, ai_fn = AI_CALLERS[i % len(AI_CALLERS)]
                futures.append((parts[i], ai_name, pool.submit(ai_fn, prompt)))

            for part, ai_name, future in futures:
                raw = future.result()
                pj = extract_json(raw)
                part_id = part.get("id", part.get("title", "unknown"))
                state["messages"].append({
                    "role": part_id,
                    "model": ai_name,
                    "title": part.get("title", ""),
                    "content": json.dumps(pj, indent=2) if pj else raw
                })
                state["results"][part_id] = pj or {"raw": raw}

        if state.get("abort"):
            state["status"] = "aborted"; return
        state["status"] = "completed"
    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)
    if state.get("abort"):
        state["status"] = "aborted"
    state["finished_at"] = datetime.now().isoformat()
    log_file = LOG_DIR / f"{pair_id}.json"
    with open(log_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# --- API routes ---

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/start", methods=["POST"])
def start_debate():
    data = request.json
    task = data.get("task", "").strip()
    if not task: return jsonify({"error": "task required"}), 400
    debate_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    threshold = data.get("threshold", 8.0)
    max_rounds = data.get("max_rounds", 5)
    debates[debate_id] = {
        "id": debate_id, "task": task, "status": "running",
        "round": 0, "phase": "", "messages": [],
        "avg_score": 0, "final_solution": "",
        "error": None, "abort": False,
        "created_at": datetime.now().isoformat(), "finished_at": None,
    }
    t = threading.Thread(target=run_debate, args=(debate_id, task, threshold, max_rounds), daemon=True)
    t.start()
    return jsonify({"debate_id": debate_id})

@app.route("/api/status/<debate_id>")
def get_status(debate_id):
    state = debates.get(debate_id)
    if state: return jsonify(state)
    log_file = LOG_DIR / f"{debate_id}.json"
    if log_file.exists():
        with open(log_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        debates[debate_id] = data
        return jsonify(data)
    return jsonify({"error": "not found"}), 404

@app.route("/api/stop/<debate_id>", methods=["POST"])
def stop_debate(debate_id):
    state = debates.get(debate_id)
    if state: state["abort"] = True
    return jsonify({"ok": True})

@app.route("/api/threads")
def list_threads():
    threads = {}
    for f in sorted(LOG_DIR.glob("*.json"), reverse=True):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            tid = d.get("id", f.stem)
            threads[tid] = {"id": tid, "task": d.get("task","")[:80], "status": d.get("status","unknown"),
                           "avg_score": d.get("avg_score",0), "round": d.get("round",0),
                           "created_at": d.get("created_at","")}
        except: pass
    for tid, d in debates.items():
        threads[tid] = {"id": tid, "task": d.get("task","")[:80], "status": d.get("status","unknown"),
                       "avg_score": d.get("avg_score",0), "round": d.get("round",0),
                       "created_at": d.get("created_at","")}
    return jsonify(sorted(threads.values(), key=lambda x: x.get("created_at",""), reverse=True))

@app.route("/api/delete/<debate_id>", methods=["DELETE"])
def delete_thread(debate_id):
    debates.pop(debate_id, None)
    log_file = LOG_DIR / f"{debate_id}.json"
    if log_file.exists(): log_file.unlink()
    return jsonify({"ok": True})

@app.route("/api/test")
def test_connections():
    results = {}
    for name, fn in [("claude", call_claude), ("codex", call_codex)]:
        res = fn('Reply JSON only: {"status":"ok","model":"your_name"}')
        parsed = extract_json(res)
        results[name] = {
            "ok": "[ERROR]" not in res,
            "response": (json.dumps(parsed) if parsed else res[:200]),
            "json": parsed is not None
        }
    return jsonify(results)

@app.route("/api/pair", methods=["POST"])
def start_pair():
    data = request.json
    task = data.get("task", "").strip()
    if not task: return jsonify({"error": "task required"}), 400
    mode = data.get("mode", "pair2")
    extra_context = data.get("context", "")
    pair_id = "pair_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    pairs[pair_id] = {
        "id": pair_id, "task": task, "mode": mode, "status": "running",
        "phase": "", "messages": [], "results": {}, "spec": "",
        "error": None, "abort": False,
        "created_at": datetime.now().isoformat(), "finished_at": None,
    }
    t = threading.Thread(target=run_pair, args=(pair_id, task, mode, extra_context), daemon=True)
    t.start()
    return jsonify({"pair_id": pair_id, "mode": mode})

@app.route("/api/pair/status/<pair_id>")
def pair_status(pair_id):
    state = pairs.get(pair_id)
    if state: return jsonify(state)
    log_file = LOG_DIR / f"{pair_id}.json"
    if log_file.exists():
        with open(log_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        pairs[pair_id] = data
        return jsonify(data)
    return jsonify({"error": "not found"}), 404

@app.route("/api/pair/stop/<pair_id>", methods=["POST"])
def pair_stop(pair_id):
    state = pairs.get(pair_id)
    if state: state["abort"] = True
    return jsonify({"ok": True})


# --- HTML ---

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Debate Chain v6</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;600;700&family=JetBrains+Mono:wght@400;700&family=Noto+Sans+KR:wght@400;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0d1a;color:#e0e0e0;font-family:'IBM Plex Sans','Noto Sans KR',sans-serif;height:100vh;overflow:hidden;display:flex}
.app{display:flex;flex:1;overflow:hidden}
.sidebar{width:280px;background:#0a0a18;border-right:1px solid #1a1a3a;display:flex;flex-direction:column;flex-shrink:0}
.sidebar-header{padding:16px;border-bottom:1px solid #1a1a3a;display:flex;align-items:center;gap:10px}
.sidebar-header h2{font-size:14px;font-weight:700;background:linear-gradient(135deg,#00e5ff,#da77f2);-webkit-background-clip:text;-webkit-text-fill-color:transparent;flex:1}
.btn-new{padding:6px 14px;background:linear-gradient(135deg,#00e5ff,#0099cc);border:none;border-radius:6px;color:#000;font-size:12px;font-weight:700;cursor:pointer}
.thread-list{flex:1;overflow-y:auto;padding:8px}.thread-list::-webkit-scrollbar{width:4px}.thread-list::-webkit-scrollbar-thumb{background:#333;border-radius:2px}
.thread-item{padding:10px 12px;border-radius:8px;cursor:pointer;margin-bottom:4px;border:1px solid transparent;transition:all .15s}
.thread-item:hover{background:#1a1a2e;border-color:#2a2a4a}.thread-item.active{background:#1a1a3a;border-color:#00e5ff44}
.thread-task{font-size:12px;color:#ccc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:4px}
.thread-meta{display:flex;align-items:center;gap:6px;font-size:10px;color:#666}
.thread-status{display:inline-block;width:6px;height:6px;border-radius:50%}
.thread-status.running{background:#00e5ff;animation:pulse 1s infinite}.thread-status.converged{background:#69db7c}.thread-status.max_rounds{background:#ffd43b}.thread-status.error{background:#ff6b6b}.thread-status.completed{background:#69db7c}
.thread-score{font-family:'JetBrains Mono',monospace;font-weight:700}
.thread-delete{margin-left:auto;opacity:0;color:#ff6b6b;cursor:pointer;font-size:11px;padding:2px 6px;border-radius:4px}
.thread-item:hover .thread-delete{opacity:.6}.thread-delete:hover{opacity:1!important;background:#ff6b6b22}
.main{flex:1;display:flex;flex-direction:column;overflow:hidden}
.header{border-bottom:1px solid #1a1a3a;padding:14px 24px;display:flex;align-items:center;gap:14px;flex-shrink:0}
.header-icon{font-size:24px;filter:drop-shadow(0 0 8px #00e5ff88)}
.header h1{font-size:18px;font-weight:700;background:linear-gradient(135deg,#00e5ff,#da77f2);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.header p{font-size:11px;color:#555;letter-spacing:1px;text-transform:uppercase}
.roles{margin-left:auto;display:flex;gap:14px}.roles span{font-size:10px;font-weight:600;opacity:.6}
.content{flex:1;overflow-y:auto;padding:20px 24px}.content::-webkit-scrollbar{width:6px}.content::-webkit-scrollbar-thumb{background:#333;border-radius:3px}
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:#444;gap:12px}
.empty-icon{font-size:48px;opacity:.3}.empty-text{font-size:14px}.empty-sub{font-size:11px;color:#333;text-align:center;line-height:1.6}
.input-area{flex-shrink:0;border-top:1px solid #1a1a3a;padding:16px 24px;background:#0a0a16}
.input-row{display:flex;gap:10px;align-items:flex-end}
textarea{flex:1;background:#12122a;border:1px solid #2a2a4a;border-radius:8px;color:#e0e0e0;font-size:13px;padding:10px 12px;resize:none;font-family:'IBM Plex Sans','Noto Sans KR',sans-serif;line-height:1.5;min-height:44px;max-height:120px}
textarea:focus{outline:none;border-color:#00e5ff;box-shadow:0 0 0 2px #00e5ff33}
.btn{padding:8px 20px;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;letter-spacing:.5px;white-space:nowrap}
.btn-run{background:linear-gradient(135deg,#00e5ff,#0099cc);color:#000;height:44px}.btn-run:disabled{background:#333;color:#666;cursor:not-allowed}
.btn-stop{background:#ff6b6b22;border:1px solid #ff6b6b55;color:#ff6b6b;height:44px}
.progress{margin-bottom:16px}.progress-info{display:flex;justify-content:space-between;margin-bottom:6px;font-size:11px;font-family:'JetBrains Mono',monospace}
.progress-label{color:#888}.progress-score{font-weight:700}
.progress-bar{height:3px;background:#2a2a4a;border-radius:2px;overflow:hidden}
.progress-fill{height:100%;background:linear-gradient(90deg,#00e5ff,#da77f2);border-radius:2px;transition:width .5s ease}
.msg{margin-bottom:14px;padding-left:14px;animation:fadeSlide .3s ease}
.msg-generator{border-left:3px solid #00e5ff}.msg-critic{border-left:3px solid #ff6b6b}.msg-synthesizer{border-left:3px solid #da77f2}
.msg-header{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.role-tag{display:inline-flex;align-items:center;gap:4px;border-radius:5px;padding:2px 8px;font-size:11px;font-weight:700;letter-spacing:.3px}
.role-generator{background:#00e5ff18;border:1px solid #00e5ff44;color:#00e5ff}.role-critic{background:#ff6b6b18;border:1px solid #ff6b6b44;color:#ff6b6b}
.role-synthesizer{background:#da77f218;border:1px solid #da77f244;color:#da77f2}
.score{display:inline-flex;border-radius:5px;padding:2px 8px;font-size:12px;font-weight:800;font-family:'JetBrains Mono',monospace}
.score-pass{background:#69db7c22;border:1px solid #69db7c55;color:#69db7c}.score-fail{background:#ff6b6b22;border:1px solid #ff6b6b55;color:#ff6b6b}
.msg pre{margin:0;white-space:pre-wrap;word-break:break-word;font-size:12px;line-height:1.6;color:#d4d4d4;font-family:'JetBrains Mono',monospace;background:#1a1a2e;border-radius:8px;padding:14px;max-height:400px;overflow:auto;border:1px solid #2a2a4a}
.round-divider{display:flex;align-items:center;gap:12px;margin:20px 0 16px;color:#555;font-size:11px;font-family:'JetBrains Mono',monospace}
.round-divider::before,.round-divider::after{content:'';flex:1;height:1px;background:#1a1a3a}
.result{margin-top:16px;padding:16px;border-radius:10px;animation:fadeSlide .4s ease}
.result-ok{background:#69db7c0a;border:1px solid #69db7c33}.result-fail{background:#ff6b6b0a;border:1px solid #ff6b6b33}
.result-header{display:flex;align-items:center;gap:10px}.result-icon{font-size:24px}.result-title{font-size:15px;font-weight:700}.result-sub{font-size:11px;color:#888}
.btn-copy{margin-left:auto;padding:5px 14px;background:#2a2a4a;border:1px solid #3a3a5a;border-radius:6px;color:#aaa;font-size:11px;cursor:pointer}.btn-copy:hover{background:#3a3a5a;color:#ddd}
.test-btn{margin-top:12px;padding:8px 20px;background:#2a2a4a;border:1px solid #3a3a5a;border-radius:8px;color:#aaa;font-size:12px;cursor:pointer}.test-btn:hover{background:#3a3a5a;color:#ddd}
@keyframes fadeSlide{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
</style>
</head>
<body>
<div class="app">
<div class="sidebar">
  <div class="sidebar-header"><h2>Debate Chain</h2><button class="btn-new" onclick="newThread()">+ New</button></div>
  <div class="thread-list" id="threadList"></div>
</div>
<div class="main">
  <div class="header">
    <div><h1>Debate Chain v6</h1><p>Claude + Codex &middot; 2-AI Debate</p></div>
    <div class="roles"><span style="color:#00e5ff">Claude Opus 4.6</span><span style="color:#ff6b6b">GPT-5.4 (Critic)</span></div>
  </div>
  <div class="content" id="content">
    <div class="empty" id="emptyState">
      <div class="empty-text">New debate</div>
      <div class="empty-sub">Claude generates, GPT-5.4 reviews. Converge at 8.0/10.</div>
      <button class="test-btn" onclick="testConnections()">Test connections</button>
      <div id="testResult" style="margin-top:12px;font-size:12px;font-family:'JetBrains Mono',monospace;max-width:500px"></div>
    </div>
    <div id="progressArea" style="display:none" class="progress">
      <div class="progress-info"><span id="progressLabel" class="progress-label"></span><span id="progressScore" class="progress-score"></span></div>
      <div class="progress-bar"><div id="progressFill" class="progress-fill" style="width:0%"></div></div>
    </div>
    <div id="messages"></div>
    <div id="resultArea"></div>
  </div>
  <div class="input-area">
    <div class="input-row">
      <textarea id="taskInput" rows="1" placeholder="Enter task... (Enter to run, Shift+Enter for newline)" oninput="autoGrow(this)"></textarea>
      <button id="btnStop" class="btn btn-stop" style="display:none" onclick="stopDebate()">Stop</button>
      <button id="btnRun" class="btn btn-run" onclick="startDebate()">Run</button>
    </div>
  </div>
</div>
</div>
<script>
const ROLES={generator:{name:"Generator",icon:"",cls:"generator"},critic:{name:"Critic",icon:"",cls:"critic"},synthesizer:{name:"Synthesizer",icon:"",cls:"synthesizer"}};
const PHASES=["generator","critic","synthesizer"];const THRESHOLD=8.0,MAX_ROUNDS=5;
let cid=null,pt=null,lmc=0,run=false;
function autoGrow(el){el.style.height='auto';el.style.height=Math.min(el.scrollHeight,120)+'px'}
document.getElementById("taskInput").addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();startDebate()}});
async function testConnections(){const el=$('testResult');el.innerHTML='Testing...';try{const r=await fetch("/api/test");const d=await r.json();el.innerHTML=Object.entries(d).map(([k,v])=>{const c=v.ok?'#69db7c':'#ff6b6b';return `<div style="color:${c};margin:6px 0;padding:8px;background:${c}11;border:1px solid ${c}33;border-radius:6px"><b>${v.ok?'OK':'FAIL'} ${k} ${v.json?'JSON ok':'no JSON'}</b></div>`}).join('')}catch(e){el.innerHTML=`<span style="color:#ff6b6b">${e.message}</span>`}}
async function loadThreads(){const r=await fetch("/api/threads");const t=await r.json();const el=$('threadList');if(!t.length){el.innerHTML='<div style="padding:20px;text-align:center;color:#444;font-size:12px">No debates yet</div>';return}el.innerHTML=t.map(t=>{const a=t.id===cid?'active':'';const sc=t.avg_score>=THRESHOLD?'#69db7c':t.avg_score>0?'#ff6b6b':'#666';const tm=t.created_at?new Date(t.created_at).toLocaleString('ko-KR',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}):'';return `<div class="thread-item ${a}" onclick="selectThread('${t.id}')"><div class="thread-task">${esc(t.task)}</div><div class="thread-meta"><span class="thread-status ${t.status}"></span><span>${t.status}</span><span>R${t.round}</span><span class="thread-score" style="color:${sc}">${t.avg_score>0?t.avg_score.toFixed(1):'-'}</span><span style="margin-left:auto;color:#555">${tm}</span><span class="thread-delete" onclick="event.stopPropagation();deleteThread('${t.id}')">x</span></div></div>`}).join('')}
async function selectThread(id){if(pt)clearInterval(pt);cid=id;lmc=0;$('messages').innerHTML='';$('resultArea').innerHTML='';$('emptyState').style.display='none';const r=await fetch(`/api/status/${id}`);const s=await r.json();if(s.error==='not found')return;$('taskInput').value=s.task||'';renderAll(s);if(s.status==='running'){run=true;$('btnRun').disabled=true;$('btnStop').style.display='inline-block';$('progressArea').style.display='block';pt=setInterval(poll,1500)}else{run=false;$('btnRun').disabled=false;$('btnStop').style.display='none';$('progressArea').style.display='none';renderResult(s)}loadThreads()}
function renderAll(s){const c=$('messages');c.innerHTML='';let cr=0;s.messages.forEach(m=>{if(m.role==='generator'){cr++;c.innerHTML+=`<div class="round-divider">Round ${cr}</div>`}const r=ROLES[m.role]||{name:m.role,cls:"generator"};let sh='';if(m.score!==undefined){const p=m.score>=THRESHOLD;sh=`<span class="score ${p?'score-pass':'score-fail'}">${m.score.toFixed(1)}/10</span>`}c.innerHTML+=`<div class="msg msg-${r.cls}"><div class="msg-header"><span class="role-tag role-${r.cls}">${r.name}</span>${sh}</div><pre>${esc(m.content)}</pre></div>`});lmc=s.messages.length;sb()}
function renderResult(s){if(s.status!=='converged'&&s.status!=='max_rounds'&&s.status!=='completed')return;const ok=s.status==='converged'||s.status==='completed';$('resultArea').innerHTML=`<div class="result ${ok?'result-ok':'result-fail'}"><div class="result-header"><span class="result-icon">${ok?'OK':'!!'}</span><div><div class="result-title" style="color:${ok?'#69db7c':'#ff6b6b'}">${s.status}</div><div class="result-sub">${s.round||0} rounds - Score: ${(s.avg_score||0).toFixed(1)}/10</div></div><button class="btn-copy" onclick="copyResult()">Copy</button></div></div>`}
async function deleteThread(id){await fetch(`/api/delete/${id}`,{method:'DELETE'});if(cid===id){cid=null;$('messages').innerHTML='';$('resultArea').innerHTML='';$('emptyState').style.display='flex';$('taskInput').value='';$('progressArea').style.display='none'}loadThreads()}
function newThread(){if(pt)clearInterval(pt);cid=null;lmc=0;run=false;$('messages').innerHTML='';$('resultArea').innerHTML='';$('emptyState').style.display='flex';$('taskInput').value='';$('taskInput').focus();$('progressArea').style.display='none';$('btnRun').disabled=false;$('btnStop').style.display='none';loadThreads()}
async function startDebate(){const task=$('taskInput').value.trim();if(!task||run)return;run=true;$('btnRun').disabled=true;$('btnStop').style.display='inline-block';$('progressArea').style.display='block';$('messages').innerHTML='';$('resultArea').innerHTML='';$('emptyState').style.display='none';lmc=0;const r=await fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({task,threshold:THRESHOLD,max_rounds:MAX_ROUNDS})});const d=await r.json();cid=d.debate_id;loadThreads();pt=setInterval(poll,1500)}
async function poll(){if(!cid)return;const r=await fetch(`/api/status/${cid}`);const s=await r.json();$('progressLabel').textContent=`Round ${s.round}/${MAX_ROUNDS} - ${s.phase||'...'}`;$('progressFill').style.width=Math.min((s.round/MAX_ROUNDS)*100,100)+"%";if(s.avg_score>0){$('progressScore').textContent=`Score: ${s.avg_score.toFixed(1)} / ${THRESHOLD}`;$('progressScore').style.color=s.avg_score>=THRESHOLD?'#69db7c':'#ff6b6b'}if(s.messages.length>lmc){const c=$('messages');for(let i=lmc;i<s.messages.length;i++){const m=s.messages[i];if(m.role==='generator'){c.innerHTML+=`<div class="round-divider">Round ${Math.floor(i/2)+1}</div>`}const r=ROLES[m.role]||{name:m.role,cls:"generator"};let sh='';if(m.score!==undefined){const p=m.score>=THRESHOLD;sh=`<span class="score ${p?'score-pass':'score-fail'}">${m.score.toFixed(1)}/10</span>`}c.innerHTML+=`<div class="msg msg-${r.cls}"><div class="msg-header"><span class="role-tag role-${r.cls}">${r.name}</span>${sh}</div><pre>${esc(m.content)}</pre></div>`}lmc=s.messages.length;sb()}if(s.status!=='running'){clearInterval(pt);run=false;$('btnRun').disabled=false;$('btnStop').style.display='none';$('progressArea').style.display='none';renderResult(s);loadThreads()}}
async function stopDebate(){if(cid)await fetch(`/api/stop/${cid}`,{method:"POST"})}
function copyResult(){fetch(`/api/status/${cid}`).then(r=>r.json()).then(s=>{navigator.clipboard.writeText(s.final_solution||'');const b=document.querySelector('.btn-copy');if(b){b.textContent='Copied!';setTimeout(()=>b.textContent='Copy',1500)}})}
function sb(){$('content').scrollTop=$('content').scrollHeight}
function $(id){return document.getElementById(id)}
function esc(t){return String(t).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")}
loadThreads();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    print("\nDebate Chain v6")
    print("  Claude Opus 4.6 (Generator) + GPT-5.4 (Critic)")
    print("  2-AI Debate | Pair2 | Pair3")
    print(f"  http://localhost:5000\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
