"""Shared, persisted run state: task board, mailbox, findings ledger, event log.

Everything lives in <repo>/.agentswarm/runs/<run_id>/state.json so a crashed or
restarted process can resume a swarm exactly where it stopped.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

TODO, IN_PROGRESS, DONE, FAILED, BLOCKED = "todo", "in_progress", "done", "failed", "blocked"


@dataclass
class Task:
    id: str
    title: str
    description: str
    deps: list = field(default_factory=list)
    files: list = field(default_factory=list)
    ui: bool = False
    round: int = 1
    status: str = TODO
    assignee: str | None = None
    attempts: int = 0
    summary: str = ""
    error: str = ""


@dataclass
class Message:
    sender: str
    to: str  # worker name, "all", or "foreman"
    body: str
    ts: float = field(default_factory=time.time)
    read_by: list = field(default_factory=list)


@dataclass
class Finding:
    author: str
    task_id: str | None
    text: str
    ts: float = field(default_factory=time.time)


class RunState:
    def __init__(self, run_dir: Path, data: dict):
        self.run_dir = Path(run_dir)
        self._lock = threading.RLock()
        self.run_id: str = data["run_id"]
        self.goal: str = data["goal"]
        self.repo: str = data["repo"]
        self.workdir: str = data["workdir"]
        self.branch: str | None = data.get("branch")
        self.base_ref: str | None = data.get("base_ref")
        self.design: bool = data.get("design", False)
        self.phase: str = data.get("phase", "plan")
        self.round: int = data.get("round", 1)
        self.plan: str = data.get("plan", "")
        self.report: str = data.get("report", "")
        self.verdict: str = data.get("verdict", "")
        self.cost: dict = data.get("cost", {})
        self.tasks: list[Task] = [Task(**t) for t in data.get("tasks", [])]
        self.mail: list[Message] = [Message(**m) for m in data.get("mail", [])]
        self.findings: list[Finding] = [Finding(**f) for f in data.get("findings", [])]
        self.events: list[dict] = data.get("events", [])
        self.ui_evidence: dict = data.get("ui_evidence", {})

    # ---------- persistence ----------
    @classmethod
    def create(cls, runs_root: Path, goal: str, repo: str, workdir: str, **extra) -> "RunState":
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        run_dir = Path(runs_root) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        state = cls(run_dir, {"run_id": run_id, "goal": goal, "repo": repo, "workdir": workdir, **extra})
        state.save()
        return state

    @classmethod
    def load(cls, run_dir: Path) -> "RunState":
        data = json.loads((Path(run_dir) / "state.json").read_text())
        return cls(run_dir, data)

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "run_id": self.run_id,
                "goal": self.goal,
                "repo": self.repo,
                "workdir": self.workdir,
                "branch": self.branch,
                "base_ref": self.base_ref,
                "design": self.design,
                "phase": self.phase,
                "round": self.round,
                "plan": self.plan,
                "report": self.report,
                "verdict": self.verdict,
                "cost": self.cost,
                "tasks": [asdict(t) for t in self.tasks],
                "mail": [asdict(m) for m in self.mail],
                "findings": [asdict(f) for f in self.findings],
                "events": self.events[-500:],
                "ui_evidence": self.ui_evidence,
            }

    def save(self) -> None:
        with self._lock:
            tmp = self.run_dir / "state.json.tmp"
            tmp.write_text(json.dumps(self.to_dict(), indent=2))
            tmp.replace(self.run_dir / "state.json")

    def log(self, who: str, what: str) -> None:
        with self._lock:
            self.events.append({"ts": time.time(), "who": who, "what": what[:500]})
            with (self.run_dir / "events.log").open("a") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} [{who}] {what}\n")
            self.save()

    # ---------- board ----------
    def add_tasks(self, specs: list[dict], round_no: int | None = None) -> list[Task]:
        with self._lock:
            existing = {t.id for t in self.tasks}
            added = []
            for i, spec in enumerate(specs, 1):
                tid = str(spec.get("id") or f"r{round_no or self.round}-t{len(self.tasks) + 1}")
                while tid in existing:
                    tid += "x"
                existing.add(tid)
                task = Task(
                    id=tid,
                    title=str(spec.get("title", f"task {i}"))[:200],
                    description=str(spec.get("description", "")),
                    deps=[str(d) for d in spec.get("deps", [])],
                    files=[str(f) for f in spec.get("files", [])],
                    ui=bool(spec.get("ui", False)),
                    round=round_no or self.round,
                )
                self.tasks.append(task)
                added.append(task)
            # Drop deps that point at nothing so a typo can't deadlock the board.
            for task in added:
                task.deps = [d for d in task.deps if d in existing and d != task.id]
            self.save()
            return added

    def get_task(self, task_id: str) -> Task | None:
        with self._lock:
            return next((t for t in self.tasks if t.id == task_id), None)

    def claim_next(self, worker: str) -> Task | None:
        with self._lock:
            done = {t.id for t in self.tasks if t.status == DONE}
            dead = {t.id for t in self.tasks if t.status in (FAILED, BLOCKED)}
            for task in self.tasks:
                if task.status != TODO:
                    continue
                if any(d in dead for d in task.deps):
                    task.status = BLOCKED
                    task.error = "a dependency failed"
                    continue
                if all(d in done for d in task.deps):
                    task.status = IN_PROGRESS
                    task.assignee = worker
                    task.attempts += 1
                    self.save()
                    return task
            self.save()
            return None

    def finish_task(self, task_id: str, ok: bool, summary: str = "", error: str = "", retry_limit: int = 2) -> None:
        with self._lock:
            task = self.get_task(task_id)
            if task is None:
                return
            if ok:
                task.status, task.summary, task.error = DONE, summary, ""
            elif task.attempts < retry_limit:
                task.status, task.error, task.assignee = TODO, error, None
            else:
                task.status, task.error = FAILED, error
            self.save()

    def pending(self) -> bool:
        with self._lock:
            return any(t.status in (TODO, IN_PROGRESS) for t in self.tasks)

    def reset_in_progress(self) -> int:
        """Used on resume: anything that was mid-flight goes back to the board."""
        with self._lock:
            n = 0
            for t in self.tasks:
                if t.status == IN_PROGRESS:
                    t.status, t.assignee = TODO, None
                    t.attempts = max(t.attempts - 1, 0)
                    n += 1
            self.save()
            return n

    # ---------- mailbox ----------
    def send(self, sender: str, to: str, body: str) -> None:
        with self._lock:
            self.mail.append(Message(sender=sender, to=to, body=body[:4000]))
            self.save()

    def inbox(self, reader: str, mark_read: bool = True) -> list[Message]:
        with self._lock:
            out = [m for m in self.mail if m.to in (reader, "all") and reader not in m.read_by and m.sender != reader]
            if mark_read and out:
                for m in out:
                    m.read_by.append(reader)
                self.save()
            return out

    # ---------- findings ----------
    def add_finding(self, author: str, text: str, task_id: str | None = None) -> None:
        with self._lock:
            self.findings.append(Finding(author=author, task_id=task_id, text=text[:2000]))
            self.save()

    # ---------- summaries for the expensive model ----------
    def board_counts(self) -> dict:
        with self._lock:
            counts: dict = {}
            for t in self.tasks:
                counts[t.status] = counts.get(t.status, 0) + 1
            return counts

    def compressed_summary(self, max_findings: int = 15, cost_line: str = "") -> str:
        """What the foreman sees. Deliberately small: one line per task, recent findings only."""
        with self._lock:
            lines = [
                f"run {self.run_id} | phase={self.phase} round={self.round} | board={self.board_counts()}",
            ]
            if cost_line:
                lines.append(cost_line)
            lines.append("tasks:")
            for t in self.tasks:
                note = t.summary or t.error
                note = (" -- " + note.replace("\n", " ")[:160]) if note else ""
                lines.append(f"  [{t.status}] {t.id} {t.title}{note}")
            if self.findings:
                lines.append("recent findings:")
                for f in self.findings[-max_findings:]:
                    lines.append(f"  - ({f.author}{'/' + f.task_id if f.task_id else ''}) {f.text[:200]}")
            to_foreman = [m for m in self.mail if m.to == "foreman" and "foreman" not in m.read_by]
            if to_foreman:
                lines.append("mail for foreman:")
                for m in to_foreman[-10:]:
                    lines.append(f"  - {m.sender}: {m.body[:300]}")
            return "\n".join(lines)
