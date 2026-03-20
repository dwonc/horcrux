#!/usr/bin/env node
const { McpServer } = require("@modelcontextprotocol/sdk/server/mcp.js");
const { StdioServerTransport } = require("@modelcontextprotocol/sdk/server/stdio.js");
const { z } = require("zod");
const http = require("http");

const FLASK = "http://localhost:5000";

function flask(path, method, body) {
  return new Promise((res, rej) => {
    const u = new URL(path, FLASK);
    const r = http.request({
      hostname: u.hostname, port: u.port, path: u.pathname,
      method: method || "GET",
      headers: { "Content-Type": "application/json" },
      timeout: 600000
    }, (resp) => {
      let d = "";
      resp.on("data", c => d += c);
      resp.on("end", () => { try { res(JSON.parse(d)); } catch { res({ raw: d }); } });
    });
    r.on("error", rej);
    if (body) r.write(JSON.stringify(body));
    r.end();
  });
}

const server = new McpServer({ name: "debate-chain", version: "5.0" });

server.tool("debate_start",
  "3-AI debate chain (Claude+Codex+Gemini). Run server.py on :5000 first.",
  { task: z.string(), max_rounds: z.number().optional(), threshold: z.number().optional() },
  async (args) => {
    try {
      const r = await flask("/api/start", "POST", {
        task: args.task, max_rounds: args.max_rounds || 5, threshold: args.threshold || 8.0
      });
      if (r.error) return { content: [{ type: "text", text: "Error: " + r.error }] };
      const did = r.debate_id;
      let st = "running", res = {};
      while (st === "running") {
        await new Promise(r => setTimeout(r, 3000));
        res = await flask("/api/status/" + did);
        st = res.status || "error";
        process.stderr.write("poll R" + res.round + " " + res.phase + "\n");
      }
      let t = "## " + (st === "converged" ? "Converged" : st) + "\n";
      t += "Rounds: " + res.round + " | Score: " + (res.avg_score || 0).toFixed(1) + "/10\n\n";
      for (const m of res.messages || []) {
        t += "### " + m.role;
        if (m.score !== undefined) t += " (" + m.score.toFixed(1) + ")";
        t += "\n" + m.content + "\n\n";
      }
      if (res.final_solution) t += "## Solution\n" + res.final_solution;
      return { content: [{ type: "text", text: t }] };
    } catch (e) {
      return { content: [{ type: "text", text: "server.py not running: " + e.message }] };
    }
  }
);

server.tool("debate_status",
  "Check debate progress",
  { debate_id: z.string() },
  async (args) => {
    try {
      const r = await flask("/api/status/" + args.debate_id);
      if (r.error) return { content: [{ type: "text", text: "Not found" }] };
      return { content: [{ type: "text", text: r.status + " R" + r.round + " " + (r.avg_score || 0).toFixed(1) + "/10" }] };
    } catch (e) {
      return { content: [{ type: "text", text: "Error: " + e.message }] };
    }
  }
);

server.tool("debate_test",
  "Test AI connections",
  {},
  async () => {
    try {
      const r = await flask("/api/test");
      let t = "Connection Test:\n";
      for (const [n, v] of Object.entries(r))
        t += (v.ok ? "OK" : "FAIL") + " " + n + " " + (v.json ? "JSON ok" : "no JSON") + "\n";
      return { content: [{ type: "text", text: t }] };
    } catch (e) {
      return { content: [{ type: "text", text: "server.py not running: " + e.message }] };
    }
  }
);

// ─── Pair 모드: 병렬 코드 생성 ───

async function pollPair(pid) {
  let st = "running", res = {};
  while (st === "running") {
    await new Promise(r => setTimeout(r, 3000));
    res = await flask("/api/pair/status/" + pid);
    st = res.status || "error";
    process.stderr.write("pair poll " + pid + " phase=" + res.phase + "\n");
  }
  return res;
}

function formatPairResult(res) {
  let t = "## Pair " + (res.mode || "").toUpperCase() + " " + (res.status === "completed" ? "Completed" : res.status) + "\n\n";
  t += "### API Spec (Architect - Claude Opus 4.6)\n" + (res.spec || "N/A") + "\n\n";
  for (const m of res.messages || []) {
    if (m.role === "architect") continue;
    t += "### " + m.role + " (" + m.model + ")\n" + m.content + "\n\n";
  }
  return t;
}

server.tool("pair2_start",
  "2-AI parallel gen: Claude + Codex split task into 2 parts and build simultaneously. Auto-splits by architecture (e.g. controller+service, backend+frontend, auth+crud).",
  { task: z.string(), context: z.string().optional() },
  async (args) => {
    try {
      const r = await flask("/api/pair", "POST", { task: args.task, mode: "pair2", context: args.context || "" });
      if (r.error) return { content: [{ type: "text", text: "Error: " + r.error }] };
      const res = await pollPair(r.pair_id);
      return { content: [{ type: "text", text: formatPairResult(res) }] };
    } catch (e) {
      return { content: [{ type: "text", text: "server.py not running: " + e.message }] };
    }
  }
);

server.tool("pair3_start",
  "3-AI parallel gen: Claude + Codex + Gemini split task into 3 parts and build simultaneously. Auto-splits any way needed.",
  { task: z.string(), context: z.string().optional() },
  async (args) => {
    try {
      const r = await flask("/api/pair", "POST", { task: args.task, mode: "pair3", context: args.context || "" });
      if (r.error) return { content: [{ type: "text", text: "Error: " + r.error }] };
      const res = await pollPair(r.pair_id);
      return { content: [{ type: "text", text: formatPairResult(res) }] };
    } catch (e) {
      return { content: [{ type: "text", text: "server.py not running: " + e.message }] };
    }
  }
);

server.tool("pair_status",
  "Check pair generation progress",
  { pair_id: z.string() },
  async (args) => {
    try {
      const r = await flask("/api/pair/status/" + args.pair_id);
      if (r.error) return { content: [{ type: "text", text: "Not found" }] };
      return { content: [{ type: "text", text: r.status + " phase=" + r.phase + " mode=" + r.mode }] };
    } catch (e) {
      return { content: [{ type: "text", text: "Error: " + e.message }] };
    }
  }
);

// ─── Main ───

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  process.stderr.write("MCP: debate-chain v5.1 connected (debate + pair2 + pair3)\n");
}

main().catch(e => { process.stderr.write("MCP fatal: " + e.stack + "\n"); process.exit(1); });
