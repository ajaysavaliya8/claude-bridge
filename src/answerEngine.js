// Answering engine: runs the logged-in `claude` CLI read-only in the project
// (subscription auth; no API key). Bounded subprocess with kill-on-timeout, an
// is_error guard, and a narrow session self-heal (only a non-zero exit on a
// RESUMED run clears the accumulating session — a transient blip never wipes it).

import { spawn } from "node:child_process";
import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

import { RESPONDER_SYSTEM_PROMPT, buildNotePrompt, buildQuestionPrompt } from "./prompts.js";

export class ClaudeError extends Error {}        // does NOT justify clearing the session
export class ClaudeExitError extends ClaudeError {} // non-zero exit: the one case that does

export class AnswerEngine {
  constructor({ projectDir, claudeBin, allowedTools, maxTurns, timeoutSec, sessionFile }) {
    this.projectDir = projectDir;
    this.claudeBin = claudeBin;
    this.allowedTools = allowedTools;
    this.maxTurns = maxTurns;
    this.timeoutSec = timeoutSec;
    this.sessionFile = sessionFile;
    this._lock = Promise.resolve(); // serialize claude runs (no concurrent resume of one session)
  }

  _loadSession() {
    try {
      return readFileSync(this.sessionFile, "utf8").trim() || null;
    } catch {
      return null;
    }
  }

  _saveSession(id) {
    if (!id) return;
    mkdirSync(dirname(this.sessionFile), { recursive: true });
    const tmp = `${this.sessionFile}.tmp`;
    writeFileSync(tmp, id, "utf8");
    renameSync(tmp, this.sessionFile); // atomic — no truncated id on a crash
  }

  _clearSession() {
    try { rmSync(this.sessionFile); } catch { /* already gone */ }
  }

  _runClaude(prompt, resume) {
    return new Promise((resolve, reject) => {
      const argv = [
        "-p",
        "--output-format", "json",
        "--allowedTools", this.allowedTools,
        "--max-turns", String(this.maxTurns),
        "--append-system-prompt", RESPONDER_SYSTEM_PROMPT,
      ];
      if (resume) argv.push("--resume", resume);

      const child = spawn(this.claudeBin, argv, { cwd: this.projectDir });
      let out = "", err = "", killed = false;
      const timer = setTimeout(() => { killed = true; child.kill("SIGKILL"); }, this.timeoutSec * 1000);

      child.stdout.on("data", (d) => { out += d; });
      child.stderr.on("data", (d) => { err += d; });
      child.on("error", (e) => { clearTimeout(timer); reject(new ClaudeError(`could not spawn claude: ${e.message}`)); });
      child.on("close", (code) => {
        clearTimeout(timer);
        if (killed) return reject(new ClaudeError(`claude timed out after ${this.timeoutSec}s and was killed`));
        if (code !== 0) return reject(new ClaudeExitError(`claude exited ${code}: ${err.slice(0, 2000)}`));
        let data;
        try { data = JSON.parse(out); }
        catch { return reject(new ClaudeError(`could not parse claude JSON output: ${out.slice(0, 2000)}`)); }
        if (data.is_error === true || String(data.subtype || "").startsWith("error")) {
          return reject(new ClaudeError(`claude returned an error result (subtype=${data.subtype}): ${String(data.result || "").slice(0, 500)}`));
        }
        resolve(data);
      });
      child.stdin.write(prompt);
      child.stdin.end();
    });
  }

  async _sessionTurn(prompt) {
    // simple async mutex
    const prev = this._lock;
    let release;
    this._lock = new Promise((r) => { release = r; });
    await prev;
    try {
      const resume = this._loadSession();
      let result;
      try {
        result = await this._runClaude(prompt, resume);
      } catch (e) {
        if (!(e instanceof ClaudeExitError) || !resume) throw e;
        console.error("resumed claude run exited non-zero; clearing session, retrying fresh");
        this._clearSession();
        result = await this._runClaude(prompt, null);
      }
      if (typeof result.session_id === "string") this._saveSession(result.session_id);
      return result;
    } finally {
      release();
    }
  }

  async answer(sender, question, imagePaths = []) {
    let prompt = buildQuestionPrompt(sender, question);
    if (imagePaths && imagePaths.length) {
      prompt += `\n\nThe peer attached image file(s) at these local paths — use the Read tool to view them, then factor them into your answer:\n${imagePaths.map((p) => `- ${p}`).join("\n")}`;
    }
    try {
      const data = await this._sessionTurn(prompt);
      return {
        answer: String(data.result || "").trim() || "(empty answer)",
        is_error: false,
        cost_usd: data.total_cost_usd ?? null,
        meta: { session_id: data.session_id, num_turns: data.num_turns, duration_ms: data.duration_ms },
      };
    } catch (e) {
      console.error("answer failed:", e.message); // full detail (incl. stderr) stays LOCAL
      return { answer: "(the peer could not answer — its claude run failed; check that peer's own logs)", is_error: true, cost_usd: null, meta: {} };
    }
  }

  async note(sender, message) {
    try { await this._sessionTurn(buildNotePrompt(sender, message)); }
    catch (e) { console.error("note injection failed:", e.message); }
  }
}
