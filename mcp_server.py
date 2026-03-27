"""Horcrux MCP Server v5.3 (legacy Python MCP — JS mcp_server.js v8.0 권장)

Note: adaptive/adaptive_classify 도구는 mcp_server.js v8.0의 run 도구로 통합됨.
"""
import json, sys, subprocess, os, re, tempfile, concurrent.futures, platform, shutil
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

_NPM = r"C:\Users\User\AppData\Roaming\npm"

# ── Claude 모델 ──
CLAUDE_MODELS = {
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
}

GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-2.5-pro", "gemini-1.5-flash"]
_gidx = 0


def _win(name: str) -> str:
    return f"{_NPM}\\{name}.cmd"


def _detect_model_from_text(text: str) -> str:
    lower = text.lower()
    if any(kw in lower for kw in ["sonnet", "소넷", "sonnet 4", "sonnet4"]):
        return CLAUDE_MODELS["sonnet"]
    if any(kw in lower for kw in ["opus", "오퍼스", "opus 4", "opus4"]):
        return CLAUDE_MODELS["opus"]
    return ""


def _strip_model_hint(text: str) -> str:
    patterns = [
        r'(sonnet|소넷|opus|오퍼스)\s*(으로|모델로|로|4\.6으로)?\s*(돌려줘|돌려|실행해|써줘|사용해)?',
        r'use\s+(sonnet|opus)\s*(4\.6)?',
        r'--model\s+(sonnet|opus)',
    ]
    result = text
    for p in patterns:
        result = re.sub(p, '', result, flags=re.IGNORECASE).strip()
    return result


# ─── AI Calls ───

def call_claude(prompt, timeout=900, model=""):
    try:
        if platform.system() == "Windows":
            cmd = ["cmd", "/c", _win("claude"), "-p"]
        else:
            exe = shutil.which("claude") or "claude"
            cmd = [exe, "-p"]
        if model:
            cmd.extend(["--model", model])
        r = subprocess.run(
            cmd, input=prompt,
            capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
            cwd=tempfile.gettempdir()
        )
        out = r.stdout.strip()
        if r.returncode != 0 and not out:
            return f"[ERROR] Claude (rc={r.returncode}): {r.stderr[:500]}"
        return out if out else f"[ERROR] Claude empty: {r.stderr[:300]}"
    except subprocess.TimeoutExpired:
        return "[ERROR] Claude timeout"
    except FileNotFoundError:
        return "[ERROR] Claude CLI not found"
    except Exception as e:
        return f"[ERROR] Claude: {str(e)[:500]}"


def _call_openai_sdk(prompt: str, timeout: int = 180) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return ""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        for model in ["gpt-4o-mini", "gpt-4o"]:
            try:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=8192, timeout=timeout,
                )
                text = resp.choices[0].message.content or ""
                if text.strip():
                    sys.stderr.write(f"[FALLBACK] Codex CLI → OpenAI SDK/{model}\n")
                    return text.strip()
            except Exception as e:
                err = str(e).lower()
                if "rate" in err or "quota" in err or "billing" in err:
                    continue
                raise
        return ""
    except ImportError:
        pass
    try:
        import requests as _req
        for model in ["gpt-4o-mini", "gpt-4o"]:
            try:
                r = _req.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 8192},
                    timeout=timeout,
                )
                if r.status_code == 200:
                    text = r.json()["choices"][0]["message"]["content"].strip()
                    if text:
                        sys.stderr.write(f"[FALLBACK] Codex CLI → OpenAI REST/{model}\n")
                        return text
                elif r.status_code in (429, 402):
                    continue
                else:
                    r.raise_for_status()
            except Exception:
                continue
    except ImportError:
        pass
    return ""


def _call_opensource_fallback(prompt: str, timeout: int = 120) -> str:
    import requests as _req
    FALLBACKS = [
        ("Groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile", {}),
        ("Cerebras", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "llama-3.3-70b", {}),
        ("OpenRouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "meta-llama/llama-3.3-70b-instruct:free",
         {"HTTP-Referer": "https://github.com/horcrux", "X-Title": "Horcrux"}),
    ]
    for name, base, env_key, model, extra_h in FALLBACKS:
        key = os.environ.get(env_key, "")
        if not key:
            continue
        try:
            h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            h.update(extra_h)
            r = _req.post(f"{base}/chat/completions", headers=h, json={
                "model": model, "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 8192, "temperature": 0.7,
            }, timeout=timeout)
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"].strip()
            if text:
                sys.stderr.write(f"[FALLBACK] → {name}/{model}\n")
                return text
        except Exception as e:
            sys.stderr.write(f"[FALLBACK] {name} failed: {str(e)[:200]}\n")
            continue
    return ""


def _codex_fallback(prompt: str) -> str:
    result = _call_openai_sdk(prompt)
    if result:
        return result
    result = _call_opensource_fallback(prompt)
    if result:
        return result
    return "[ERROR] Codex CLI failed. Set OPENAI_API_KEY (recommended) or GROQ_API_KEY in .env"


def call_codex(prompt, timeout=600):
    try:
        if platform.system() == "Windows":
            cmd = ["cmd", "/c", _win("codex"), "exec", "--skip-git-repo-check"]
        else:
            exe = shutil.which("codex") or "codex"
            cmd = [exe, "exec", "--skip-git-repo-check"]
        r = subprocess.run(
            cmd, input=prompt,
            capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace"
        )
        out = r.stdout.strip()
        if r.returncode == 0 and out and "[ERROR]" not in out:
            return out
        if r.returncode != 0 or not out:
            fb = _codex_fallback(prompt)
            if "[ERROR]" not in fb:
                return fb
        return out if out else f"[ERROR] Codex: {(r.stderr or '')[:300]}"
    except FileNotFoundError:
        return _codex_fallback(prompt)
    except subprocess.TimeoutExpired:
        return "[ERROR] Codex timeout"
    except Exception as e:
        fb = _codex_fallback(prompt)
        if "[ERROR]" not in fb:
            return fb
        return f"[ERROR] Codex: {e}"


def call_gemini(prompt, timeout=300):
    global _gidx
    for a in range(len(GEMINI_MODELS)):
        idx = (_gidx + a) % len(GEMINI_MODELS)
        model = GEMINI_MODELS[idx]
        try:
            if platform.system() == "Windows":
                cmd = ["cmd", "/c", _win("gemini"), "--model", model]
            else:
                exe = shutil.which("gemini") or "gemini"
                cmd = [exe, "--model", model]
            r = subprocess.run(
                cmd, input=prompt,
                capture_output=True, text=True, timeout=timeout,
                encoding="utf-8", errors="replace"
            )
            se = r.stderr or ""
            if r.returncode != 0 and ("quota" in se.lower() or "exhausted" in se.lower()):
                _gidx = (idx + 1) % len(GEMINI_MODELS)
                continue
            out = r.stdout.strip()
            if out:
                _gidx = idx
                return out
            return f"[ERROR] Gemini/{model}: {se[:300]}"
        except Exception as e:
            return f"[ERROR] Gemini: {e}"
    return "[ERROR] Gemini: all quotas exhausted"


def extract_json(text):
    if not text or "[ERROR]" in text:
        return None
    c = re.sub(r'```(?:json)?\s*', '', text).replace('```', '').strip()
    try:
        return json.loads(c)
    except:
        pass
    d = 0; s = -1
    for i, ch in enumerate(c):
        if ch == '{':
            if d == 0: s = i
            d += 1
        elif ch == '}':
            d -= 1
            if d == 0 and s >= 0:
                try:
                    return json.loads(c[s:i+1])
                except:
                    s = -1
    return None


def get_score(data, raw):
    if data and "score" in data:
        try:
            v = float(data["score"])
            if 0 < v <= 10:
                return v
        except:
            pass
    for p in [r'"score"\s*:\s*(\d+)', r'(\d+(?:\.\d+)?)\s*/\s*10']:
        m = re.search(p, raw)
        if m:
            v = float(m.group(1))
            if 0 < v <= 10:
                return v
    return 5.0


# ─── Aux Critic ───

AUX_CRITIC_ENDPOINTS = [
    ("Groq/Llama", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
     "llama-3.3-70b-versatile", {}),
    ("DS/DeepSeek", "https://api.deepseek.com/v1", "DEEPSEEK_API_KEY",
     "deepseek-chat", {}),
    ("OR/GPT-OSS", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
     "openai/gpt-oss-120b:free",
     {"HTTP-Referer": "https://github.com/horcrux", "X-Title": "Horcrux"}),
]

AUX_MAX_PROMPT_CHARS = 15000

def _truncate_for_aux(prompt: str) -> str:
    if len(prompt) <= AUX_MAX_PROMPT_CHARS:
        return prompt
    keep = AUX_MAX_PROMPT_CHARS // 2 - 50
    cut = len(prompt) - AUX_MAX_PROMPT_CHARS
    return prompt[:keep] + f"\n\n...[AUX TRUNCATED {cut} chars]...\n\n" + prompt[-keep:]

def _call_aux_critic(name, base_url, env_key, model, extra_headers, prompt, timeout=180):
    api_key = os.environ.get(env_key, "")
    if not api_key:
        return name, ""
    try:
        import requests as _req
        short_prompt = _truncate_for_aux(prompt)
        h = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        h.update(extra_headers)
        r = _req.post(f"{base_url}/chat/completions", headers=h, json={
            "model": model, "messages": [{"role": "user", "content": short_prompt}],
            "max_tokens": 8192, "temperature": 0.7,
        }, timeout=timeout)
        if r.status_code == 429:
            sys.stderr.write(f"  [AUX] {name} rate limited (429), skipped\n")
            return name, ""
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"].strip()
        sys.stderr.write(f"  [AUX] {name}/{model} responded ({len(text)} chars)\n")
        return name, text
    except Exception as e:
        sys.stderr.write(f"  [AUX] {name} failed: {str(e)[:150]}\n")
        return name, ""


# ─── Debate Logic ───

def do_review(task, solution):
    cp = f"Task: {task}\n\nSolution:\n{solution}\n\nReview: bugs, security, edge cases.\nJSON only: {{\"score\":<1-10>,\"summary\":\"<2 sentences>\",\"issues\":[{{\"sev\":\"critical|major|minor\",\"desc\":\"<issue>\",\"fix\":\"<fix>\"}}],\"strengths\":[\"s1\"]}}"
    vp = f"Task: {task}\n\nSolution:\n{solution}\n\nVerify: correctness, completeness.\nJSON only: {{\"score\":<1-10>,\"summary\":\"<2 sentences>\",\"issues\":[{{\"sev\":\"critical|major|minor\",\"desc\":\"<issue>\",\"fix\":\"<fix>\"}}],\"correct\":true/false}}"

    available_aux = [ep for ep in AUX_CRITIC_ENDPOINTS if os.environ.get(ep[2])]
    total_workers = 2 + len(available_aux)

    with concurrent.futures.ThreadPoolExecutor(max(total_workers, 2)) as pool:
        cf = pool.submit(call_codex, cp)
        vf = pool.submit(call_gemini, vp)
        aux_futures = [
            pool.submit(_call_aux_critic, name, base, env_key, model, extra_h, cp)
            for name, base, env_key, model, extra_h in available_aux
        ]
        cr = cf.result()
        vr = vf.result()
        aux_results = [f.result() for f in aux_futures]

    cd = extract_json(cr) or {}
    vd = extract_json(vr) or {}
    cs = get_score(cd, cr)
    vs = get_score(vd, vr)

    aux_scores = {}
    aux_parsed = []
    for name, raw in aux_results:
        if not raw:
            continue
        data = extract_json(raw) or {}
        score = get_score(data, raw)
        aux_scores[name] = score
        aux_parsed.append((data, name))

    core_min = min(cs, vs)
    if aux_scores:
        aux_avg = sum(aux_scores.values()) / len(aux_scores)
        overall = core_min * 0.8 + aux_avg * 0.2
    else:
        overall = core_min

    all_issues = []
    seen = set()
    all_critics = [(cd, "Codex"), (vd, "Gemini")] + aux_parsed
    for data, src in all_critics:
        for iss in data.get("issues", []):
            if isinstance(iss, dict):
                key = iss.get("desc", "")[:40]
                if key not in seen:
                    seen.add(key)
                    iss["source"] = src
                    all_issues.append(iss)

    critic_scores = {"Codex": cs, "Gemini": vs}
    critic_scores.update(aux_scores)

    return {
        "critic": {"score": cs, "data": cd, "raw": cr},
        "verifier": {"score": vs, "data": vd, "raw": vr},
        "aux_scores": aux_scores,
        "critic_scores": critic_scores,
        "all_issues": all_issues,
        "avg": round(overall, 1),
        "aux_count": len(aux_scores),
    }


def do_generate(task, prev_solution="", issues="", model=""):
    if not prev_solution:
        gp = f"Solve thoroughly.\nTask: {task}\nJSON only: {{\"solution\":\"<complete>\",\"approach\":\"<1 sentence>\"}}"
    else:
        gp = f"Task: {task}\nCurrent:\n{prev_solution}\nFix:\n{issues}\nJSON only: {{\"solution\":\"<improved>\",\"changes\":[\"fix1\"]}}"
    gr = call_claude(gp, model=model)
    gj = extract_json(gr)
    if gj and "solution" in gj:
        return gj["solution"], gj.get("approach", gj.get("changes", ""))
    return gr, ""


def do_debate(task, max_r=3, model=""):
    sol = ""
    hist = []
    for r in range(1, max_r + 1):
        issues_str = ""
        if hist:
            issues = []
            for src in ["critic", "verifier"]:
                for iss in (hist[-1][src].get("data", {}).get("issues") or []):
                    if isinstance(iss, dict):
                        issues.append(f"[{iss.get('sev','?')}] {iss.get('desc','')}")
            issues_str = "\n".join(issues)
        sol, _ = do_generate(task, sol if r > 1 else "", issues_str, model=model)
        rv = do_review(task, sol)
        rv["round"] = r
        hist.append(rv)
        if rv["avg"] >= 8.0:
            return {
                "status": "converged", "rounds": r, "score": rv["avg"],
                "solution": sol, "history": hist, "model": model or "default",
            }
    return {
        "status": "max_rounds", "rounds": max_r,
        "score": hist[-1]["avg"] if hist else 0,
        "solution": sol, "history": hist, "model": model or "default",
    }


# ─── MCP Protocol ───

TOOLS = [
    {
        "name": "debate",
        "description": "Multi-AI Horcrux debate. Claude generates (Opus or Sonnet), then Codex+Gemini+Aux(Groq/Together/OpenRouter) review in parallel (up to 5 critics). Score = Core*0.8 + Aux*0.2. Repeats until >= 8.0. Say 'sonnet으로 돌려줘' to use Sonnet 4.6.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task to debate"},
                "max_rounds": {"type": "integer", "description": "Max rounds (default 3)", "default": 3},
                "claude_model": {
                    "type": "string",
                    "description": "Claude model: 'opus' (default, Max sub) or 'sonnet' (Pro sub). Auto-detected from task text.",
                    "enum": ["opus", "sonnet", ""],
                    "default": "",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "review",
        "description": "Parallel review by Codex(Critic) + Gemini(Verifier). Send your solution to get scored.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Original task"},
                "solution": {"type": "string", "description": "Solution to review"},
            },
            "required": ["task", "solution"],
        },
    },
    {
        "name": "planning",
        "description": "Layer 3 Planning Pipeline: content_profile(3AI→synth→critic→converge) + artifact_profile(spec→critic→render). task_type: brainstorm(ideas), portfolio(restructure), hybrid(content→artifact), artifact_only(render existing content). artifact_type: ppt, pdf, doc, readme.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Planning task"},
                "task_type": {
                    "type": "string",
                    "description": "brainstorm(ideas), portfolio(restructure), hybrid(content→artifact, default), artifact_only(render)",
                    "enum": ["brainstorm", "portfolio", "hybrid", "artifact_only"],
                    "default": "hybrid",
                },
                "artifact_type": {
                    "type": "string",
                    "description": "Output format: ppt, pdf, doc, readme",
                    "enum": ["ppt", "pdf", "doc", "readme"],
                    "default": "doc",
                },
                "audience": {"type": "string", "description": "Target audience", "default": "general"},
                "tone": {"type": "string", "description": "Tone: professional, casual, technical", "default": "professional"},
                "claude_model": {
                    "type": "string",
                    "description": "Claude model: 'opus' (default) or 'sonnet'",
                    "enum": ["opus", "sonnet", ""],
                    "default": "",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "generate",
        "description": "Generate solution using Claude. Supports model selection (Opus/Sonnet).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task"},
                "prev_solution": {"type": "string", "description": "Previous solution", "default": ""},
                "issues": {"type": "string", "description": "Issues to fix", "default": ""},
                "claude_model": {
                    "type": "string",
                    "description": "'opus' or 'sonnet'",
                    "enum": ["opus", "sonnet", ""],
                    "default": "",
                },
            },
            "required": ["task"],
        },
    },
]


def read_message():
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
    body = json.dumps(msg).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    sys.stdout.buffer.write(header + body)
    sys.stdout.buffer.flush()


def send(id, result):
    write_message({"jsonrpc": "2.0", "id": id, "result": result})


def send_err(id, code, msg):
    write_message({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": msg}})


def _resolve_model(args: dict, task: str) -> str:
    explicit = args.get("claude_model", "")
    if explicit:
        return CLAUDE_MODELS.get(explicit, "")
    return _detect_model_from_text(task)


def handle_call(id, params):
    name = params.get("name")
    args = params.get("arguments", {})

    if name == "debate":
        task = args.get("task", "")
        if not task:
            send_err(id, -32602, "task required")
            return
        model = _resolve_model(args, task)
        clean_task = _strip_model_hint(task) if model else task
        model_label = "Sonnet 4.6" if "sonnet" in model else "Opus 4.6 (default)" if not model else model
        res = do_debate(clean_task, args.get("max_rounds", 3), model=model)

        txt = f"## Debate: {res['status']} | R{res['rounds']} | {res['score']}/10\n"
        txt += f"**Claude Model: {model_label}**\n\n"
        for h in res.get("history", []):
            aux_n = h.get('aux_count', 0)
            scores_str = ' | '.join(f"{k}:{v:.1f}" for k, v in h.get('critic_scores', {}).items()) if h.get('critic_scores') else f"Codex:{h['critic']['score']:.1f} | Gemini:{h['verifier']['score']:.1f}"
            scoring_label = f"Core*0.8+Aux({aux_n})*0.2" if aux_n else "avg(Codex+Gemini)"
            txt += f"### Round {h['round']} — {h['avg']}/10 ({scoring_label})\n"
            txt += f"Scores: [{scores_str}]\n\n"
            txt += f"- Critic(Codex): {h['critic']['score']}/10 {h['critic'].get('data',{}).get('summary','')}\n"
            txt += f"- Verifier(Gemini): {h['verifier']['score']}/10 {h['verifier'].get('data',{}).get('summary','')}\n"
            for aname, ascore in h.get('aux_scores', {}).items():
                txt += f"- Aux({aname}): {ascore:.1f}/10\n"
            for iss in h.get('all_issues', []):
                if isinstance(iss, dict):
                    src = iss.get('source', '')
                    txt += f"  - [{iss.get('sev','')}][{src}] {iss.get('desc','')} -> {iss.get('fix','')}\n"
            if not h.get('all_issues'):
                for src in ["critic", "verifier"]:
                    for iss in (h[src].get("data", {}).get("issues") or []):
                        if isinstance(iss, dict):
                            txt += f"  - [{iss.get('sev','')}] {iss.get('desc','')} -> {iss.get('fix','')}\n"
            txt += "\n"
        txt += f"## Solution\n```\n{res['solution']}\n```"
        send(id, {"content": [{"type": "text", "text": txt}]})

    elif name == "review":
        task = args.get("task", "")
        sol = args.get("solution", "")
        if not task or not sol:
            send_err(id, -32602, "task and solution required")
            return
        r = do_review(task, sol)
        txt = f"## Review: {r['avg']}/10\n\n"
        txt += f"### Critic (Codex): {r['critic']['score']}/10\n{r['critic'].get('data',{}).get('summary','')}\n"
        for iss in (r['critic'].get('data', {}).get('issues') or []):
            if isinstance(iss, dict):
                txt += f"- [{iss.get('sev','')}] {iss.get('desc','')} -> {iss.get('fix','')}\n"
        txt += f"\n### Verifier (Gemini): {r['verifier']['score']}/10\n{r['verifier'].get('data',{}).get('summary','')}\n"
        for iss in (r['verifier'].get('data', {}).get('issues') or []):
            if isinstance(iss, dict):
                txt += f"- [{iss.get('sev','')}] {iss.get('desc','')} -> {iss.get('fix','')}\n"
        if r['avg'] >= 8.0:
            txt += f"\n---\n**CONVERGED** ({r['avg']}/10 >= 8.0)"
        else:
            txt += f"\n---\n**Below threshold** ({r['avg']}/10 < 8.0) - improve and review again"
        send(id, {"content": [{"type": "text", "text": txt}]})

    elif name == "generate":
        task = args.get("task", "")
        if not task:
            send_err(id, -32602, "task required")
            return
        model = _resolve_model(args, task)
        clean_task = _strip_model_hint(task) if model else task
        model_label = "Sonnet 4.6" if "sonnet" in model else "Opus 4.6" if "opus" in model else "default"
        sol, info = do_generate(
            clean_task,
            args.get("prev_solution", ""),
            args.get("issues", ""),
            model=model,
        )
        txt = f"## Generated by Claude ({model_label})\n"
        if info:
            txt += f"**Info:** {info}\n\n"
        txt += f"```\n{sol}\n```"
        send(id, {"content": [{"type": "text", "text": txt}]})

    elif name == "planning":
        task = args.get("task", "")
        if not task:
            send_err(id, -32602, "task required")
            return
        model = _resolve_model(args, task)
        clean_task = _strip_model_hint(task) if model else task
        model_label = "Sonnet 4.6" if "sonnet" in model else "Opus 4.6" if "opus" in model else "default"

        task_type = args.get("task_type", "hybrid")
        artifact_type = args.get("artifact_type", "doc")
        audience = args.get("audience", "general")
        tone = args.get("tone", "professional")

        import requests as _req
        try:
            resp = _req.post("http://localhost:5000/api/planning", json={
                "task": clean_task,
                "claude_model": args.get("claude_model", ""),
                "task_type": task_type,
                "artifact_type": artifact_type,
                "audience": audience,
                "tone": tone,
            }, timeout=10)
            result = resp.json()
            planning_id = result.get("planning_id", "")
            if not planning_id:
                send(id, {"content": [{"type": "text", "text": f"Planning start failed: {result}"}]})
                return

            import time
            for _ in range(720):
                time.sleep(5)
                sr = _req.get(f"http://localhost:5000/api/planning/status/{planning_id}", timeout=10).json()
                status = sr.get("status", "")
                phase = sr.get("phase", "")
                sys.stderr.write(f"  [PLANNING] {planning_id}: {status} / {phase}\n")
                sys.stderr.flush()
                if status != "running":
                    break

            fr = _req.get(f"http://localhost:5000/api/planning/result/{planning_id}", timeout=10).json()
            final_status = fr.get("status", "unknown")
            avg_score = fr.get("avg_score", 0)
            final_solution = fr.get("final_solution", fr.get("final_plan", ""))

            txt = f"## Planning Pipeline (Layer 3): {final_status}\n"
            txt += f"**Model: {model_label}** | **Type: {task_type}** | **Artifact: {artifact_type}** | **Score: {avg_score}/10**\n\n"

            msgs = fr.get("messages", [])
            generators = [m for m in msgs if m.get("role") == "generator"]
            synthesizers = [m for m in msgs if m.get("role") == "synthesizer"]
            critics = [m for m in msgs if m.get("role") == "critic"]
            revisions = [m for m in msgs if m.get("role") == "revision"]
            artifact_specs = [m for m in msgs if m.get("role") == "artifact_spec"]
            artifact_rendered = [m for m in msgs if m.get("role") == "artifact_rendered"]

            if generators:
                txt += f"### Content: {len(generators)} generators\n"
                for g in generators:
                    txt += f"- **{g.get('model', '?')}**\n"
                txt += "\n"
            if synthesizers:
                txt += f"### Synthesized into unified content\n\n"
            if critics:
                txt += f"### {len(critics)} critics reviewed\n"
                for c in critics:
                    score = c.get('score', '?')
                    txt += f"- {c.get('model', '?')}: {score}/10\n"
                txt += "\n"
            if revisions:
                txt += f"### {len(revisions)} revision rounds\n\n"
            if artifact_specs:
                txt += f"### Artifact spec built ({artifact_type})\n\n"
            if artifact_rendered:
                txt += f"### Final {artifact_type} rendered\n\n"

            txt += f"## Final Output\n```\n{final_solution[:6000]}\n```"
            if len(final_solution) > 6000:
                txt += f"\n\n(truncated, full: {len(final_solution)} chars)"

            send(id, {"content": [{"type": "text", "text": txt}]})

        except Exception as e:
            send(id, {"content": [{"type": "text", "text": f"Planning error: {e}"}]})

    else:
        send_err(id, -32601, f"Unknown: {name}")


def main():
    while True:
        try:
            msg = read_message()
            if msg is None:
                break
            method = msg.get("method", "")
            id = msg.get("id")
            params = msg.get("params", {})

            if method == "initialize":
                send(id, {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "horcrux", "version": "5.3"},
                })
            elif method == "notifications/initialized":
                pass
            elif method == "tools/list":
                send(id, {"tools": TOOLS})
            elif method == "tools/call":
                handle_call(id, params)
            elif id is not None:
                send_err(id, -32601, f"Unknown: {method}")
        except Exception as e:
            sys.stderr.write(f"MCP Error: {e}\n")
            sys.stderr.flush()


if __name__ == "__main__":
    main()
