"""The orchestrator: foreman plans -> lead splits -> team grinds -> foreman audits -> cleanup -> final pass."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from . import prompts, workspace
from .config import SwarmConfig
from .llm import LLM, BudgetExceeded, CostLedger
from .state import DONE, FAILED, RunState
from .worker import worker_loop

PHASES = ("plan", "split", "execute", "audit", "final", "done")
HOST_REVIEW = "awaiting_review"  # MCP mode: the host model (Codex/Claude) is the foreman


def repo_snapshot(workdir: Path, max_files: int = 300) -> str:
    files = workspace.git(workdir, "ls-files", check=False).splitlines()
    shown = files[:max_files]
    more = f"\n... and {len(files) - max_files} more files" if len(files) > max_files else ""
    readme = ""
    for name in ("README.md", "README", "readme.md"):
        p = workdir / name
        if p.is_file():
            readme = p.read_text(errors="replace")[:3000]
            break
    return "FILES:\n" + "\n".join(shown) + more + ("\n\nREADME (head):\n" + readme if readme else "")


class Swarm:
    def __init__(self, state: RunState, cfg: SwarmConfig, log=print, host_foreman: bool = False):
        self.state = state
        self.cfg = cfg
        self.print = log
        self.ledger = CostLedger(budget_usd=cfg.budget_usd)
        self.ledger.load(state.cost)
        self.host_foreman = host_foreman or not cfg.foreman.api_key
        self.foreman = None if self.host_foreman else LLM(cfg.foreman, "foreman", self.ledger)
        self.lead = LLM(cfg.lead, "lead", self.ledger)
        self.worker_llm = LLM(cfg.worker, "worker", self.ledger)
        vcfg = cfg.vision or cfg.foreman
        self.vision = LLM(vcfg, "vision", self.ledger) if vcfg and vcfg.api_key else None
        self.stop = threading.Event()
        self._interventions = 0
        self._intervene_lock = threading.Lock()

    # ---------- construction ----------
    @classmethod
    def start(cls, repo: Path, goal: str, cfg: SwarmConfig, design: bool = False, plan: str = "",
              tasks: list | None = None, log=print, host_foreman: bool = False) -> "Swarm":
        repo = workspace.repo_root(repo)
        root = workspace.ensure_state_dir(repo)
        state = RunState.create(root / "runs", goal=goal, repo=str(repo), workdir=str(repo), design=design)
        if cfg.use_worktree:
            path, branch, base = workspace.create_worktree(repo, state.run_id)
            state.workdir, state.branch, state.base_ref = str(path), branch, base
        else:
            state.base_ref = workspace.git(repo, "rev-parse", "HEAD", check=False) or None
        if plan:
            state.plan = plan
            state.phase = "split"
        if tasks:
            state.add_tasks(tasks)
            state.phase = "execute"
        state.save()
        state.log("swarm", f"started in {state.workdir}" + (f" on branch {state.branch}" if state.branch else ""))
        return cls(state, cfg, log=log, host_foreman=host_foreman)

    @classmethod
    def resume(cls, run_dir: Path, cfg: SwarmConfig, log=print, host_foreman: bool = False) -> "Swarm":
        state = RunState.load(run_dir)
        n = state.reset_in_progress()
        state.log("swarm", f"resumed at phase={state.phase}; {n} in-flight task(s) returned to the board")
        return cls(state, cfg, log=log, host_foreman=host_foreman)

    # ---------- helpers ----------
    def _save_cost(self) -> None:
        self.state.cost = self.ledger.to_dict()
        self.state.save()
        (self.state.run_dir / "status.md").write_text(self.status())

    def status(self) -> str:
        return self.state.compressed_summary(cost_line=self.ledger.line())

    def _commit(self, message: str) -> None:
        # Only auto-commit on the swarm's own branch, never on the user's checkout.
        if self.state.branch:
            workspace.commit_all(Path(self.state.workdir), message)

    def _phase(self, phase: str) -> None:
        self.state.phase = phase
        self.state.log("swarm", f"phase -> {phase}")
        self._save_cost()
        self.print(f"== {phase} ==  {self.ledger.line()}")

    # ---------- main loop ----------
    def run(self) -> str:
        try:
            while self.state.phase not in ("done", HOST_REVIEW):
                step = getattr(self, "phase_" + self.state.phase)
                step()
                self._save_cost()
        except BudgetExceeded as e:
            self.state.log("swarm", f"STOPPED: {e}")
            self.print(f"!! {e}. Resume later with a higher budget.")
        finally:
            self._save_cost()
        return self.state.phase

    def phase_plan(self) -> None:
        if self.foreman is None:
            raise RuntimeError("no foreman model configured and no plan given; set the foreman API key or pass a plan")
        snap = repo_snapshot(Path(self.state.workdir))
        out = self.foreman.ask_json(prompts.FOREMAN_PLAN, f"GOAL:\n{self.state.goal}\n\nREPO:\n{snap}")
        acceptance = "\n".join(f"- {a}" for a in out.get("acceptance", []))
        self.state.plan = str(out.get("plan", "")) + ("\n\nACCEPTANCE CRITERIA:\n" + acceptance if acceptance else "")
        if out.get("ui") and not self.state.design:
            self.state.log("foreman", "plan involves UI; consider --design")
        (self.state.run_dir / "plan.md").write_text(self.state.plan)
        self._phase("split")

    def phase_split(self) -> None:
        system = prompts.LEAD_SPLIT.format(workers=self.cfg.workers)
        snap = repo_snapshot(Path(self.state.workdir), max_files=150)
        out = self.lead.ask_json(system, f"GOAL:\n{self.state.goal}\n\nPLAN:\n{self.state.plan}\n\nREPO:\n{snap}")
        tasks = out.get("tasks") or []
        if not tasks:
            tasks = [{"id": "t1", "title": "Implement the plan", "description": self.state.plan}]
        added = self.state.add_tasks(tasks)
        self.state.log("lead", f"split into {len(added)} tasks")
        self._phase("execute")

    def phase_execute(self) -> None:
        self.stop.clear()
        n = max(1, min(self.cfg.workers, sum(1 for t in self.state.tasks if t.status == "todo") or 1))
        threads = [
            threading.Thread(
                target=worker_loop,
                args=(self.state, f"worker-{i + 1}", self.worker_llm, self.cfg, self.vision, self.stop),
                kwargs={"on_task_end": self._on_task_end},
                daemon=True,
            )
            for i in range(n)
        ]
        for t in threads:
            t.start()
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(timeout=5)
            self._save_cost()
        self.ledger.check_budget()
        self._commit(f"swarm {self.state.run_id}: round {self.state.round} work")
        self._phase("audit" if not self.host_foreman else HOST_REVIEW)

    def _on_task_end(self, task, ok: bool) -> None:
        self._save_cost()
        latest = self.state.get_task(task.id)
        self.print(f"  {'+' if ok else 'x'} {task.id} {task.title}  |  {self.ledger.line()}")
        if ok or latest is None or latest.status != FAILED or self.foreman is None:
            return
        with self._intervene_lock:
            if self._interventions >= 3:
                return
            self._interventions += 1
            self.intervene()

    def intervene(self) -> None:
        """Foreman steps in with only the compressed summary."""
        out = self.foreman.ask_json(prompts.FOREMAN_INTERVENE, f"GOAL:\n{self.state.goal}\n\n{self.status()}")
        self.state.log("foreman", "intervened: " + str(out.get("notes", ""))[:300])
        for m in out.get("messages", []) or []:
            self.state.send("foreman", str(m.get("to", "all")), str(m.get("body", "")))
        for tid in out.get("retry", []) or []:
            t = self.state.get_task(str(tid))
            if t and t.status == FAILED:
                t.status, t.attempts, t.assignee = "todo", 0, None
        if out.get("add_tasks"):
            self.state.add_tasks(out["add_tasks"])
        for m in self.state.mail:
            if m.to == "foreman" and "foreman" not in m.read_by:
                m.read_by.append("foreman")
        if out.get("abort"):
            self.stop.set()
        self.state.save()

    def audit_bundle(self, per_file: int = 6000, total: int = 120000) -> str:
        wd = Path(self.state.workdir)
        files = workspace.changed_files(wd, self.state.base_ref)
        parts, used = [], 0
        for f in files:
            d = workspace.file_diff(wd, self.state.base_ref, f, limit=per_file)
            if used + len(d) > total:
                parts.append(f"### {f}\n(omitted: audit size limit; inspect manually)")
                continue
            used += len(d)
            parts.append(f"### {f}\n```diff\n{d}\n```")
        return f"CHANGED FILES ({len(files)}):\n" + "\n\n".join(parts)

    def team_report(self) -> str:
        return "\n".join(
            f"- [{t.status}] {t.id} {t.title}: {(t.summary or t.error)[:600]}" for t in self.state.tasks
        )

    def phase_audit(self) -> None:
        user = (
            f"GOAL:\n{self.state.goal}\n\nPLAN:\n{self.state.plan}\n\nTEAM REPORT:\n{self.team_report()}\n\n"
            f"FINDINGS:\n" + "\n".join(f"- {f.text[:300]}" for f in self.state.findings[-40:]) + "\n\n" + self.audit_bundle()
        )
        out = self.foreman.ask_json(prompts.FOREMAN_AUDIT, user)
        (self.state.run_dir / f"audit-round{self.state.round}.json").write_text(json.dumps(out, indent=2))
        self.state.verdict = str(out.get("verdict", ""))
        self.state.log("foreman", f"audit round {self.state.round}: {self.state.verdict} -- {str(out.get('summary', ''))[:300]}")
        tasks = out.get("cleanup_tasks") or []
        if self.state.verdict != "pass" and tasks and self.state.round <= self.cfg.review_rounds:
            self.state.round += 1
            self.state.add_tasks(tasks, round_no=self.state.round)
            self.print(f"  foreman sent a cleanup team in with {len(tasks)} task(s)")
            self._phase("execute")
        else:
            self._phase("final")

    def phase_final(self) -> None:
        user = (
            f"GOAL:\n{self.state.goal}\n\nPLAN:\n{self.state.plan}\n\nTEAM REPORT:\n{self.team_report()}\n\n"
            f"LAST AUDIT VERDICT: {self.state.verdict}\n\n" + self.audit_bundle(per_file=3000, total=60000)
        )
        out = self.foreman.ask_json(prompts.FOREMAN_FINAL, user)
        rec = out.get("recommendation", "?")
        merge_hint = (
            f"\n\n---\nBranch: `{self.state.branch}`  \nWorktree: `{self.state.workdir}`  \n"
            f"Review: `git diff {self.state.base_ref[:12]}..{self.state.branch}`  \nMerge: `git merge {self.state.branch}`"
            if self.state.branch and self.state.base_ref else ""
        )
        self.state.report = f"# Swarm report {self.state.run_id}\n\n**Recommendation: {rec}**\n\n{out.get('report', '')}{merge_hint}\n\n{self.ledger.line()}\n"
        (self.state.run_dir / "report.md").write_text(self.state.report)
        self._commit(f"swarm {self.state.run_id}: final")
        self._phase("done")

    # ---------- host-foreman (MCP) operations ----------
    def send_back(self, tasks: list) -> None:
        """Host foreman reviewed and wants another cleanup round."""
        self.state.round += 1
        self.state.add_tasks(tasks, round_no=self.state.round)
        self.state.phase = "execute"
        self.state.save()

    def finish(self, report: str = "") -> None:
        self.state.report = report or self.team_report()
        (self.state.run_dir / "report.md").write_text(self.state.report)
        self._commit(f"swarm {self.state.run_id}: final")
        self._phase("done")


def find_run(repo: Path, run_id: str | None) -> Path:
    runs = workspace.repo_root(repo) / ".agentswarm" / "runs"
    if run_id:
        p = runs / run_id
        if not (p / "state.json").is_file():
            raise FileNotFoundError(f"no run {run_id} in {runs}")
        return p
    candidates = sorted(p for p in runs.glob("*") if (p / "state.json").is_file())
    if not candidates:
        raise FileNotFoundError(f"no runs in {runs}")
    return candidates[-1]
