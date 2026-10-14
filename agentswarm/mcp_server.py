"""MCP stdio server: lets Codex / Claude Code act as the foreman.

The host's (expensive) model plans, calls swarm_start, polls the compressed swarm_status,
audits via swarm_review, sends a cleanup team back with swarm_send_back, and closes with
swarm_finish. The cheap team runs in background threads inside this server process.
If this process restarts, swarm_resume picks the run back up from disk.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from pathlib import Path

from . import __version__
from .config import load_config
from .swarm import Swarm, find_run
from .state import RunState

PROTOCOL = "2025-06-18"

_TASKS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "deps": {"type": "array", "items": {"type": "string"}},
            "files": {"type": "array", "items": {"type": "string"}},
            "ui": {"type": "boolean"},
        },
        "required": ["title", "description"],
    },
}

TOOLS = [
    {
        "name": "swarm_start",
        "description": (
            "Hand work to a team of cheap worker agents in an isolated git worktree. You are the foreman: "
            "write a precise plan (approach, constraints, verification commands, acceptance criteria). "
            "Optionally pass tasks yourself; otherwise the team lead splits the plan. Returns a run_id. "
            "Then poll swarm_status sparingly, review with swarm_review, and close with swarm_finish."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "plan": {"type": "string"},
                "tasks": _TASKS_SCHEMA,
                "design": {"type": "boolean", "description": "UI work: force screenshot+critique loops at phone and desktop sizes"},
                "repo": {"type": "string", "description": "path inside the target git repo (default: server cwd)"},
                "workers": {"type": "integer"},
            },
            "required": ["goal", "plan"],
        },
    },
    {
        "name": "swarm_status",
        "description": (
            "Compressed status: board, one line per task, recent findings, mail for the foreman, cost line. "
            "Pass wait_ms (e.g. 600000) to block until the swarm needs you -- a finished task to review, "
            "a failure, mail for you, or the run stopping -- instead of polling. 'wake' says why it returned."
        ),
        "inputSchema": {"type": "object", "properties": {
            "run_id": {"type": "string"}, "repo": {"type": "string"}, "wait_ms": {"type": "integer"}}},
    },
    {
        "name": "swarm_task_review",
        "description": (
            "The end-of-task check. Returns ONE finished task: its description, the worker's summary, exactly what it "
            "changed, and the commands it ran. Then call swarm_verdict. Omit task_id for the next task waiting."
        ),
        "inputSchema": {"type": "object", "properties": {
            "run_id": {"type": "string"}, "task_id": {"type": "string"}, "repo": {"type": "string"}}},
    },
    {
        "name": "swarm_verdict",
        "description": "Approve a finished task, or send it back with numbered, concrete fixes.",
        "inputSchema": {"type": "object", "properties": {
            "run_id": {"type": "string"}, "task_id": {"type": "string"}, "approve": {"type": "boolean"},
            "feedback": {"type": "string"}, "repo": {"type": "string"}},
            "required": ["task_id", "approve"]},
    },
    {
        "name": "swarm_message",
        "description": "Send guidance to a worker ('worker-2') or 'all' while the team is running.",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}, "to": {"type": "string"}, "body": {"type": "string"}, "repo": {"type": "string"}},
            "required": ["to", "body"],
        },
    },
    {
        "name": "swarm_review",
        "description": "Team report plus the diff of every changed file, for your audit. Call when phase is awaiting_review.",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}, "repo": {"type": "string"}}},
    },
    {
        "name": "swarm_send_back",
        "description": "Send a cleanup team back in with fix tasks from your audit.",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}, "tasks": _TASKS_SCHEMA, "repo": {"type": "string"}},
            "required": ["tasks"],
        },
    },
    {
        "name": "swarm_finish",
        "description": "Close the run with your final report. Commits the worktree; returns the branch to merge.",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": {"type": "string"}, "report": {"type": "string"}, "repo": {"type": "string"}},
        },
    },
    {
        "name": "swarm_resume",
        "description": "Resume a run after a server restart (old board and findings ledger are kept).",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}, "repo": {"type": "string"}}},
    },
    {
        "name": "swarm_stop",
        "description": "Stop the team after their current step.",
        "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}, "repo": {"type": "string"}}},
    },
]


class Server:
    def __init__(self):
        self.live: dict[str, tuple[Swarm, threading.Thread]] = {}
        self.lock = threading.Lock()

    # ---------- plumbing ----------
    def _repo(self, args: dict) -> Path:
        return Path(args.get("repo") or ".").resolve()

    def _swarm(self, args: dict) -> Swarm:
        run_dir = find_run(self._repo(args), args.get("run_id"))
        with self.lock:
            if run_dir.name in self.live:
                return self.live[run_dir.name][0]
        cfg = load_config(self._repo(args))
        return Swarm(RunState.load(run_dir), cfg, log=_stderr, host_foreman=True)

    def _launch(self, swarm: Swarm) -> None:
        def body():
            try:
                swarm.run()
            except Exception:
                swarm.state.log("swarm", "crashed: " + traceback.format_exc()[-800:])

        th = threading.Thread(target=body, daemon=True)
        with self.lock:
            self.live[swarm.state.run_id] = (swarm, th)
        th.start()

    def _running(self, run_id: str) -> bool:
        with self.lock:
            entry = self.live.get(run_id)
        return bool(entry and entry[1].is_alive())

    # ---------- tools ----------
    def swarm_start(self, args: dict) -> str:
        repo = self._repo(args)
        cfg = load_config(repo)
        if args.get("workers"):
            cfg.workers = int(args["workers"])
        swarm = Swarm.start(
            repo, args["goal"], cfg, design=bool(args.get("design")), plan=args["plan"],
            tasks=args.get("tasks"), log=_stderr, host_foreman=True,
        )
        self._launch(swarm)
        s = swarm.state
        return f"run_id={s.run_id}\nworkdir={s.workdir}\nbranch={s.branch}\nThe team is working. Check swarm_status in a few minutes."

    def _wake_reason(self, sw: Swarm) -> str | None:
        s = sw.state
        if s.awaiting_review():
            return "task-review"
        if any(m.to == "foreman" and "foreman" not in m.read_by for m in s.mail):
            return "mail"
        if any(t.status == "failed" for t in s.tasks) and s.phase == "execute" and not self._running(s.run_id):
            return "failure"
        if s.phase in ("awaiting_review", "done"):
            return s.phase
        if not self._running(s.run_id):
            return "stopped"
        return None

    def swarm_status(self, args: dict) -> str:
        sw = self._swarm(args)
        deadline = time.time() + min(int(args.get("wait_ms") or 0), 900_000) / 1000
        failed_before = sum(1 for t in sw.state.tasks if t.status == "failed")
        wake = self._wake_reason(sw)
        while wake is None and time.time() < deadline:
            time.sleep(2)
            if sum(1 for t in sw.state.tasks if t.status == "failed") > failed_before:
                wake = "failure"
                break
            wake = self._wake_reason(sw)
        running = "running" if self._running(sw.state.run_id) else "not running (use swarm_resume if unfinished)"
        out = f"wake: {wake or 'timeout'}\n" + sw.status() + f"\nprocess: {running}"
        waiting = [t.id for t in sw.state.awaiting_review()]
        if waiting:
            out += f"\nwaiting for your review: {', '.join(waiting)} -> call swarm_task_review"
        for m in sw.state.mail:
            if m.to == "foreman" and "foreman" not in m.read_by:
                m.read_by.append("foreman")
        sw.state.save()
        return out

    def swarm_task_review(self, args: dict) -> str:
        sw = self._swarm(args)
        waiting = sw.state.awaiting_review()
        task_id = args.get("task_id") or (waiting[0].id if waiting else None)
        if not task_id:
            return "no finished tasks are waiting for review"
        p = sw.state.run_dir / "reviews" / f"{task_id}.md"
        if not p.is_file():
            return f"no review packet for {task_id}"
        more = len(waiting) - 1 if waiting else 0
        return p.read_text() + (f"\n\n({more} more task(s) waiting after this one)" if more > 0 else "")

    def swarm_verdict(self, args: dict) -> str:
        sw = self._swarm(args)
        task = sw.state.get_task(args["task_id"])
        if task is None or task.status != "review":
            return f"error: task {args['task_id']} is not waiting for review"
        approve = bool(args["approve"])
        feedback = args.get("feedback", "")
        if not approve and not feedback.strip():
            return "error: when sending a task back, say exactly what to fix"
        status = sw.state.apply_verdict(task.id, approve, feedback, sw.cfg.max_task_reviews)
        if approve:
            sw.reviewed_files.update(sw.state.baselines.get(task.id, {}).keys())
        sw.state.log("foreman", f"review {task.id}: {'approved' if approve else 'sent back'} -- {feedback[:200]}")
        if not self._running(sw.state.run_id) and sw.state.pending():
            sw.state.phase = "execute"
            sw.state.save()
            self._launch(sw)
        return f"{task.id} -> {status}"

    def swarm_message(self, args: dict) -> str:
        sw = self._swarm(args)
        sw.state.send("foreman", args["to"], args["body"])
        return "sent"

    def swarm_review(self, args: dict) -> str:
        sw = self._swarm(args)
        return (
            f"phase={sw.state.phase}\n\nPLAN:\n{sw.state.plan}\n\nTEAM REPORT:\n{sw.team_report()}\n\n"
            "FINDINGS:\n" + "\n".join(f"- {f.text[:300]}" for f in sw.state.findings[-40:])
            + "\n\n" + sw.audit_bundle() + "\n\n" + sw.ledger.line()
        )

    def swarm_send_back(self, args: dict) -> str:
        sw = self._swarm(args)
        if self._running(sw.state.run_id):
            sw.state.add_tasks(args["tasks"])
            return "team is still running; tasks were added to the live board"
        sw.send_back(args["tasks"])
        self._launch(sw)
        return f"cleanup round {sw.state.round} started with {len(args['tasks'])} task(s)"

    def swarm_finish(self, args: dict) -> str:
        sw = self._swarm(args)
        if self._running(sw.state.run_id):
            return "error: team is still running; swarm_stop first or wait"
        sw.finish(args.get("report", ""))
        s = sw.state
        return f"done. branch={s.branch} worktree={s.workdir}\nreview: git diff {s.base_ref}..{s.branch}\nmerge: git merge {s.branch}\n{sw.ledger.line()}"

    def swarm_resume(self, args: dict) -> str:
        sw = self._swarm(args)
        if self._running(sw.state.run_id):
            return "already running"
        if sw.state.phase in ("done",):
            return "run is already done"
        sw.state.reset_in_progress()
        if sw.state.phase == "awaiting_review":
            return "run is waiting for your review: call swarm_review"
        self._launch(sw)
        return f"resumed {sw.state.run_id} at phase {sw.state.phase}"

    def swarm_stop(self, args: dict) -> str:
        sw = self._swarm(args)
        sw.stop.set()
        return "stop requested"

    # ---------- JSON-RPC ----------
    def handle(self, req: dict) -> dict | None:
        method, rid = req.get("method"), req.get("id")
        if rid is None:
            return None  # notification
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": req.get("params", {}).get("protocolVersion", PROTOCOL),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "agentswarm", "version": __version__},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = req.get("params", {})
                name, args = params.get("name"), params.get("arguments") or {}
                fn = getattr(self, name, None) if name in {t["name"] for t in TOOLS} else None
                if fn is None:
                    return _err(rid, -32602, f"unknown tool {name}")
                try:
                    text, is_error = fn(args), False
                except Exception as e:
                    text, is_error = f"error: {e}", True
                result = {"content": [{"type": "text", "text": text}], "isError": is_error}
            else:
                return _err(rid, -32601, f"method not found: {method}")
            return {"jsonrpc": "2.0", "id": rid, "result": result}
        except Exception as e:
            return _err(rid, -32603, str(e))

    def serve(self) -> None:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                _write(_err(None, -32700, "parse error"))
                continue
            resp = self.handle(req)
            if resp is not None:
                _write(resp)


def _err(rid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


_out_lock = threading.Lock()


def _write(obj: dict) -> None:
    with _out_lock:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def _stderr(*parts) -> None:
    print(*parts, file=sys.stderr, flush=True)


def main() -> None:
    Server().serve()
