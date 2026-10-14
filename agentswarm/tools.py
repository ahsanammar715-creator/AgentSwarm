"""Tools exposed to the cheap worker agents. All file access is confined to the workdir."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .state import RunState, Task

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", ".agentswarm", "target", ".mypy_cache", ".pytest_cache"}
MAX_READ_LINES = 400
MAX_OUTPUT = 12000

# Not a sandbox -- a seatbelt against the most common foot-guns.
DENY_PATTERNS = [
    r"\bgit\s+push\b",
    r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f|checkout\s+--\s|worktree|branch\s+-D|rebase)\b",
    r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME)(\s|$)",
    r"\bsudo\b",
    r"\bmkfs\b|\bdd\s+if=",
    r":\(\)\s*\{",
]


def _fn(name: str, desc: str, props: dict, required: list | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required or []},
        },
    }


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}

BASE_TOOLS = [
    _fn("list_files", "List files under a directory (skips node_modules, .git, build dirs).", {"path": S, "max_depth": I}),
    _fn("read_file", f"Read a text file, optionally a line range (max {MAX_READ_LINES} lines per call).", {"path": S, "start_line": I, "end_line": I}, ["path"]),
    _fn("search", "Regex search across files. Returns path:line: text. Use this instead of reading many files.", {"pattern": S, "path": S, "glob": S}, ["pattern"]),
    _fn("write_file", "Create or overwrite a file with the full content.", {"path": S, "content": S}, ["path", "content"]),
    _fn("edit_file", "Replace an exact snippet in a file. old must match exactly once unless replace_all.", {"path": S, "old": S, "new": S, "replace_all": B}, ["path", "old", "new"]),
    _fn("run", "Run a shell command in the project root (tests, linters, builds). Non-interactive only.", {"command": S, "timeout": I}, ["command"]),
    _fn("view_board", "See every task on the shared board with status and owner.", {}),
    _fn("read_mail", "Read unread messages sent to you or to all.", {}),
    _fn("send_message", "Message a teammate by name, 'all', or 'foreman' (only for real blockers / decisions).", {"to": S, "body": S}, ["to", "body"]),
    _fn("add_finding", "Record something the team should know (conventions, gotchas, where things live, test commands).", {"text": S}, ["text"]),
    _fn("complete_task", "Finish your task. Summary: what changed, which files, how you verified it.", {"summary": S}, ["summary"]),
    _fn("fail_task", "Give up on the task with a precise reason so someone else can pick it up.", {"reason": S}, ["reason"]),
]

DESIGN_TOOLS = [
    _fn(
        "screenshot",
        "Render a page (URL or HTML file path) at the 'mobile' or 'desktop' viewport, then get a strict visual "
        "critique of what is actually on screen. Required for UI tasks: every viewport, and again after each fix.",
        {"target": S, "viewport": {"type": "string", "enum": ["mobile", "desktop"]}, "intent": S},
        ["target", "viewport"],
    ),
]


class TaskFinished(Exception):
    def __init__(self, ok: bool, text: str):
        self.ok, self.text = ok, text


class Toolbox:
    def __init__(self, state: RunState, worker: str, task: Task, cfg, vision=None):
        self.state = state
        self.worker = worker
        self.task = task
        self.cfg = cfg
        self.vision = vision
        self.root = Path(state.workdir).resolve()
        self.design = state.design and task.ui

    def schemas(self) -> list:
        return BASE_TOOLS + (DESIGN_TOOLS if self.design else [])

    # ---------- dispatch ----------
    def call(self, name: str, raw_args: str) -> str:
        try:
            args = json.loads(raw_args or "{}")
            if not isinstance(args, dict):
                raise ValueError
        except ValueError:
            return "error: arguments were not a JSON object"
        fn = getattr(self, "t_" + name, None)
        if fn is None or name not in {s["function"]["name"] for s in self.schemas()}:
            return f"error: unknown tool {name}"
        try:
            out = fn(**args)
        except TaskFinished:
            raise
        except TypeError as e:
            return f"error: bad arguments: {e}"
        except Exception as e:  # tool errors go back to the model, never crash the worker
            return f"error: {e}"
        out = str(out)
        if len(out) > MAX_OUTPUT:
            out = out[:MAX_OUTPUT] + f"\n... [truncated {len(out) - MAX_OUTPUT} chars]"
        return out

    def _path(self, rel: str) -> Path:
        p = (self.root / (rel or ".")).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(f"path escapes the project: {rel}")
        if ".git" in p.relative_to(self.root).parts:
            raise ValueError("the .git directory is off limits")
        return p

    def _touched(self) -> None:
        ev = self.state.ui_evidence.setdefault(self.task.id, {"edits": 0, "shots": {}})
        ev["edits"] += 1

    # ---------- files ----------
    def t_list_files(self, path: str = ".", max_depth: int = 3) -> str:
        base = self._path(path)
        out = []
        base_depth = len(base.parts)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            depth = len(Path(dirpath).parts) - base_depth
            if depth >= max_depth:
                dirnames[:] = []
            for f in sorted(filenames):
                out.append(str(Path(dirpath, f).relative_to(self.root)))
            if len(out) > 500:
                out.append("... (more files, narrow the path)")
                break
        return "\n".join(out) or "(empty)"

    def t_read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        lines = self._path(path).read_text(errors="replace").splitlines()
        start = max(start_line, 1)
        end = min(end_line or len(lines), start + MAX_READ_LINES - 1, len(lines))
        body = "\n".join(f"{i}\t{lines[i - 1]}" for i in range(start, end + 1))
        more = f"\n... ({len(lines) - end} more lines; use start_line={end + 1})" if end < len(lines) else ""
        return (body or "(empty file)") + more

    def t_search(self, pattern: str, path: str = ".", glob: str | None = None) -> str:
        base = self._path(path)
        if shutil.which("rg"):
            cmd = ["rg", "-n", "--no-heading", "-M", "300", pattern, str(base)]
            for d in SKIP_DIRS:
                cmd[1:1] = ["-g", f"!{d}"]
            if glob:
                cmd[1:1] = ["-g", glob]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            out = proc.stdout.replace(str(self.root) + os.sep, "")
            return "\n".join(out.splitlines()[:200]) or "no matches"
        rx = re.compile(pattern)
        hits = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for f in filenames:
                if glob and not fnmatch.fnmatch(f, glob):
                    continue
                p = Path(dirpath, f)
                try:
                    for n, line in enumerate(p.read_text(errors="strict").splitlines(), 1):
                        if rx.search(line):
                            hits.append(f"{p.relative_to(self.root)}:{n}: {line[:300]}")
                except (UnicodeDecodeError, OSError):
                    continue
                if len(hits) >= 200:
                    return "\n".join(hits) + "\n... (truncated)"
        return "\n".join(hits) or "no matches"

    def t_write_file(self, path: str, content: str) -> str:
        p = self._path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        self._touched()
        return f"wrote {path} ({len(content.splitlines())} lines)"

    def t_edit_file(self, path: str, old: str, new: str, replace_all: bool = False) -> str:
        p = self._path(path)
        text = p.read_text()
        count = text.count(old)
        if count == 0:
            return "error: old snippet not found (read the file again; whitespace must match)"
        if count > 1 and not replace_all:
            return f"error: old snippet matches {count} times; add context or set replace_all"
        p.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1))
        self._touched()
        return f"edited {path} ({count if replace_all else 1} replacement)"

    def t_run(self, command: str, timeout: int | None = None) -> str:
        for pat in DENY_PATTERNS:
            if re.search(pat, command):
                return f"error: command blocked by swarm policy ({pat})"
        limit = min(timeout or self.cfg.shell_timeout, self.cfg.shell_timeout * 3)
        env = {**os.environ, "CI": "1", "GIT_TERMINAL_PROMPT": "0"}
        for key in list(env):  # workers never get the model keys
            if key.endswith("_API_KEY"):
                env.pop(key)
        try:
            proc = subprocess.run(
                command, shell=True, cwd=self.root, capture_output=True, text=True,
                timeout=limit, stdin=subprocess.DEVNULL, env=env,
            )
        except subprocess.TimeoutExpired:
            return f"error: timed out after {limit}s"
        out = (proc.stdout + ("\n[stderr]\n" + proc.stderr if proc.stderr.strip() else "")).strip()
        if len(out) > MAX_OUTPUT:  # keep the tail: that's where test failures are
            out = f"... [{len(out) - MAX_OUTPUT} chars cut]\n" + out[-MAX_OUTPUT:]
        return f"exit={proc.returncode}\n{out}"

    # ---------- team ----------
    def t_view_board(self) -> str:
        return "\n".join(
            f"[{t.status}] {t.id} ({t.assignee or '-'}) {t.title}" + (f" deps={t.deps}" if t.deps else "")
            for t in self.state.tasks
        )

    def t_read_mail(self) -> str:
        msgs = self.state.inbox(self.worker)
        return "\n".join(f"from {m.sender}: {m.body}" for m in msgs) or "no new mail"

    def t_send_message(self, to: str, body: str) -> str:
        self.state.send(self.worker, to, body)
        self.state.log(self.worker, f"mail -> {to}: {body[:120]}")
        return "sent"

    def t_add_finding(self, text: str) -> str:
        self.state.add_finding(self.worker, text, self.task.id)
        return "recorded"

    def t_complete_task(self, summary: str) -> str:
        if self.design:
            missing = self._design_gaps()
            if missing:
                return "error: design evidence missing -- " + missing
        raise TaskFinished(True, summary)

    def t_fail_task(self, reason: str) -> str:
        raise TaskFinished(False, reason)

    # ---------- design ----------
    def _design_gaps(self) -> str:
        ev = self.state.ui_evidence.get(self.task.id, {"shots": {}})
        need = self.cfg.design_min_iterations
        gaps = []
        for label in ("mobile", "desktop"):
            rounds = len(set(ev["shots"].get(label, [])))
            if rounds < need:
                gaps.append(f"{label}: {rounds}/{need} screenshot rounds (each round must follow an edit)")
        return "; ".join(gaps)

    def t_screenshot(self, target: str, viewport: str, intent: str = "") -> str:
        from .design import critique, take_screenshot

        (mw, mh), (dw, dh) = self.cfg.design_viewports
        width, height = (mw, mh) if viewport == "mobile" else (dw, dh)
        shot = take_screenshot(target, self.root, self.state.run_dir / "screens" / self.task.id, width, height)
        ev = self.state.ui_evidence.setdefault(self.task.id, {"edits": 0, "shots": {}})
        ev["shots"].setdefault(viewport, []).append(ev["edits"])
        self.state.save()
        if self.vision is None:
            return f"saved {shot.name}; no vision model configured -- critique unavailable"
        text = critique(self.vision, shot, width, height, viewport, intent or self.task.title)
        self.state.log(self.worker, f"screenshot {viewport} {target}")
        left = self._design_gaps()
        return f"screenshot {shot.name} ({width}x{height})\nCRITIQUE:\n{text}\n\n" + (
            f"Still required before complete_task: {left}" if left else "Design evidence requirement met."
        )
