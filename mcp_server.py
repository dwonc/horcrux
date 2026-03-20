"""Debate Chain MCP Server v5 — Claude Desktop 연동"""
import json, sys, subprocess, os, re, tempfile, concurrent.futures
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

GEMINI_MODELS = ["gemini-2.5-flash","gemini-2.0-flash","gemini-2.5-pro","gemini-1.5-flash"]
_gidx = 0

# ─── AI Calls ───

def call_codex(prompt, timeout=300):
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8', dir=str(LOG_DIR)) as f:
            f.write(prompt); tmp = f.name
        r = subprocess.run(
            f'type "{tmp}" | codex exec --skip-git-repo-check',
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace", shell=True
        )
        out = r.stdout.strip()
        return out if out else f"[ERROR] Codex: {(r.stderr or '')[:300]}"
    except Exception as e: return f"[ERROR] Codex: {e}"
    finally:
        if tmp:
            try: os.unlink(tmp)
            except: pass

def call_gemini(prompt, timeout=300):
    global _gidx
    for a in range(len(GEMINI_MODELS)):
        idx = (_gidx + a) % len(GEMINI_MODELS)
        model = GEMINI_MODELS[idx]; tmp = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8', dir=str(LOG_DIR)) as f:
                f.write(prompt); tmp = f.name
            r = subprocess.run(
                f'type "{tmp}" | gemini --model {model}',
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace", shell=True
            )
            se = r.stderr or ""
            if r.returncode != 0 and ("quota" in se.lower() or "exhausted" in se.lower()):
                _gidx = (idx + 1) % len(GEMINI_MODELS); continue
            out = r.stdout.strip()
            if out: _gidx = idx; return out
            return f"[ERROR] Gemini/{model}: {se[:300]}"
        except Exception as e: return f"[ERROR] Gemini: {e}"
        finally:
            if tmp:
                try: os.unlink(tmp)
                except: pass
    return "[ERROR] Gemini: all quotas exhausted"

def extract_json(text):
    if not text or "[ERROR]" in text: return None
    c = re.sub(r'```(?:json)?\s*', '', text).replace('```', '').strip()
    try: return json.loads(c)
    except: pass
    d = 0; s = -1
    for i, ch in enumerate(c):
        if ch == '{':
            if d == 0: s = i
            d += 1
        elif ch == '}':
            d -= 1
            if d == 0 and s >= 0:
                try: return json.loads(c[s:i+1])
                except: s = -1
    return None

def get_score(data, raw):
    if data and "score" in data:
        try:
            v = float(data["score"])
            if 0 < v <= 10: return v
        except: pass
    for p in [r'"score"\s*:\s*(\d+)', r'(\d+(?:\.\d+)?)\s*/\s*10']:
        m = re.search(p, raw)
        if m:
            v = float(m.group(1))
            if 0 < v <= 10: return v
    return 5.0

# ─── Debate Logic ───

def do_review(task, solution):
    cp = f"Task: {task}\n\nSolution:\n{solution}\n\nReview: bugs, security, edge cases.\nJSON only: {{\"score\":<1-10>,\"summary\":\"<2 sentences>\",\"issues\":[{{\"sev\":\"critical|major|minor\",\"desc\":\"<issue>\",\"fix\":\"<fix>\"}}],\"strengths\":[\"s1\"]}}"
    vp = f"Task: {task}\n\nSolution:\n{solution}\n\nVerify: correctness, completeness.\nJSON only: {{\"score\":<1-10>,\"summary\":\"<2 sentences>\",\"issues\":[{{\"sev\":\"critical|major|minor\",\"desc\":\"<issue>\",\"fix\":\"<fix>\"}}],\"correct\":true/false}}"
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        cf = pool.submit(call_codex, cp)
        vf = pool.submit(call_gemini, vp)
        cr = cf.result(); vr = vf.result()
    cd = extract_json(cr) or {}; vd = extract_json(vr) or {}
    cs = get_score(cd, cr); vs = get_score(vd, vr)
    return {"critic": {"score": cs, "data": cd, "raw": cr}, "verifier": {"score": vs, "data": vd, "raw": vr}, "avg": round((cs + vs) / 2, 1)}

def do_generate(task, prev_solution="", issues=""):
    if not prev_solution:
        gp = f"Solve thoroughly.\nTask: {task}\nJSON only: {{\"solution\":\"<complete>\",\"approach\":\"<1 sentence>\"}}"
    else:
        gp = f"Task: {task}\nCurrent:\n{prev_solution}\nFix:\n{issues}\nJSON only: {{\"solution\":\"<improved>\",\"changes\":[\"fix1\"]}}"
    gr = call_codex(gp); gj = extract_json(gr)
    if gj and "solution" in gj: return gj["solution"], gj.get("approach", gj.get("changes", ""))
    return gr, ""

def do_debate(task, max_r=3):
    sol = ""; hist = []
    for r in range(1, max_r + 1):
        issues_str = ""
        if hist:
            issues = []
            for src in ["critic", "verifier"]:
                for iss in (hist[-1][src].get("data", {}).get("issues") or []):
                    if isinstance(iss, dict): issues.append(f"[{iss.get('sev','?')}] {iss.get('desc','')}")
            issues_str = "\n".join(issues)
        sol, _ = do_generate(task, sol if r > 1 else "", issues_str)
        rv = do_review(task, sol); rv["round"] = r; hist.append(rv)
        if rv["avg"] >= 8.0:
            return {"status": "converged", "rounds": r, "score": rv["avg"], "solution": sol, "history": hist}
    return {"status": "max_rounds", "rounds": max_r, "score": hist[-1]["avg"] if hist else 0, "solution": sol, "history": hist}

# ─── MCP Protocol (byte-level stdio) ───

TOOLS = [
    {"name": "debate", "description": "3-AI debate chain. Codex generates, then Codex(Critic)+Gemini(Verifier) review in parallel. Repeats until score >= 8.0. You (Claude) can further improve the result.", "inputSchema": {"type": "object", "properties": {"task": {"type": "string", "description": "Task to debate"}, "max_rounds": {"type": "integer", "description": "Max rounds (default 3)", "default": 3}}, "required": ["task"]}},
    {"name": "review", "description": "Parallel review by Codex(Critic, GPT-5.4) + Gemini(Verifier). Send your solution to get scored and get issues. Improve and call again for iterative refinement.", "inputSchema": {"type": "object", "properties": {"task": {"type": "string", "description": "Original task"}, "solution": {"type": "string", "description": "Solution to review"}}, "required": ["task", "solution"]}},
    {"name": "generate", "description": "Generate solution using Codex (GPT-5.4). Can create new or improve existing solution based on issues.", "inputSchema": {"type": "object", "properties": {"task": {"type": "string", "description": "Task"}, "prev_solution": {"type": "string", "description": "Previous solution (for improvement)", "default": ""}, "issues": {"type": "string", "description": "Issues to fix", "default": ""}}, "required": ["task"]}}
]

def read_message():
    """Read one JSON-RPC message from stdin (byte-level)"""
    buf = b""
    while True:
        byte = sys.stdin.buffer.read(1)
        if not byte:
            return None
        buf += byte
        if buf.endswith(b"\r\n\r\n"):
            break
    
    headers = buf.decode("utf-8")
    length = 0
    for line in headers.split("\r\n"):
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1].strip())
            break
    
    if length == 0:
        return None
    
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))

def write_message(msg):
    """Write one JSON-RPC message to stdout (byte-level)"""
    body = json.dumps(msg).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    sys.stdout.buffer.write(header + body)
    sys.stdout.buffer.flush()

def send(id, result):
    write_message({"jsonrpc": "2.0", "id": id, "result": result})

def send_err(id, code, msg):
    write_message({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": msg}})

def handle_call(id, params):
    name = params.get("name"); args = params.get("arguments", {})
    if name == "debate":
        task = args.get("task", "")
        if not task: send_err(id, -32602, "task required"); return
        res = do_debate(task, args.get("max_rounds", 3))
        txt = f"## Debate: {res['status']} | R{res['rounds']} | {res['score']}/10\n\n"
        for h in res.get("history", []):
            txt += f"### Round {h['round']} (avg {h['avg']})\n"
            txt += f"- Critic(GPT-5.4): {h['critic']['score']}/10 {h['critic'].get('data',{}).get('summary','')}\n"
            txt += f"- Verifier(Gemini): {h['verifier']['score']}/10 {h['verifier'].get('data',{}).get('summary','')}\n"
            for src in ["critic", "verifier"]:
                for iss in (h[src].get("data", {}).get("issues") or []):
                    if isinstance(iss, dict): txt += f"  - [{iss.get('sev','')}] {iss.get('desc','')} -> {iss.get('fix','')}\n"
            txt += "\n"
        txt += f"## Solution\n```\n{res['solution']}\n```"
        send(id, {"content": [{"type": "text", "text": txt}]})
    elif name == "review":
        task = args.get("task", ""); sol = args.get("solution", "")
        if not task or not sol: send_err(id, -32602, "task and solution required"); return
        r = do_review(task, sol)
        txt = f"## Review: {r['avg']}/10\n\n"
        txt += f"### Critic (GPT-5.4): {r['critic']['score']}/10\n{r['critic'].get('data',{}).get('summary','')}\n"
        for iss in (r['critic'].get('data', {}).get('issues') or []):
            if isinstance(iss, dict): txt += f"- [{iss.get('sev','')}] {iss.get('desc','')} -> {iss.get('fix','')}\n"
        txt += f"\n### Verifier (Gemini): {r['verifier']['score']}/10\n{r['verifier'].get('data',{}).get('summary','')}\n"
        for iss in (r['verifier'].get('data', {}).get('issues') or []):
            if isinstance(iss, dict): txt += f"- [{iss.get('sev','')}] {iss.get('desc','')} -> {iss.get('fix','')}\n"
        if r['avg'] >= 8.0:
            txt += f"\n---\n**CONVERGED** ({r['avg']}/10 >= 8.0)"
        else:
            txt += f"\n---\n**Below threshold** ({r['avg']}/10 < 8.0) - improve and review again"
        send(id, {"content": [{"type": "text", "text": txt}]})
    elif name == "generate":
        task = args.get("task", "")
        if not task: send_err(id, -32602, "task required"); return
        sol, info = do_generate(task, args.get("prev_solution", ""), args.get("issues", ""))
        txt = f"## Generated by GPT-5.4\n"
        if info: txt += f"**Info:** {info}\n\n"
        txt += f"```\n{sol}\n```"
        send(id, {"content": [{"type": "text", "text": txt}]})
    else:
        send_err(id, -32601, f"Unknown: {name}")

def main():
    while True:
        try:
            msg = read_message()
            if msg is None: break
            method = msg.get("method", ""); id = msg.get("id"); params = msg.get("params", {})
            if method == "initialize":
                send(id, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "debate-chain", "version": "5.0"}})
            elif method == "notifications/initialized": pass
            elif method == "tools/list":
                send(id, {"tools": TOOLS})
            elif method == "tools/call":
                handle_call(id, params)
            elif id is not None:
                send_err(id, -32601, f"Unknown: {method}")
        except Exception as e:
            sys.stderr.write(f"MCP Error: {e}\n"); sys.stderr.flush()

if __name__ == "__main__":
    main()
