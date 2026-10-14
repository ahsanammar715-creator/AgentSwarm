"""A cheap worker agent: claims tasks off the board and runs a tool loop until done."""

from __future__ import annotations

import threading

from . import prompts
from .llm import LLM, BudgetExceeded
from .state import RunState, Task
from .tools import Toolbox, TaskFinished


def _findings_text(state: RunState, limit: int = 30) -> str:
    items = state.findings[-limit:]
    return "\n".join(f"- {f.text}" for f in items) or "(none yet)"


def run_task(state: RunState, name: str, task: Task, llm: LLM, cfg, vision: LLM | None = None,
             stop: threading.Event | None = None) -> tuple[bool, str, dict]:
    """Returns (ok, summary-or-reason, evidence for the end-of-task review)."""
    tools = Toolbox(state, name, task, cfg, vision)
    system = prompts.WORKER.format(
        name=name,
        goal=state.goal,
        plan=state.plan or "(no plan)",
        task_id=task.id,
        title=task.title,
        description=task.description,
        files=("Likely files: " + ", ".join(task.files) + "\n") if task.files else "",
        findings=_findings_text(state),
        design=prompts.DESIGN_RULES.format(n=cfg.design_min_iterations) if tools.design else "",
    )
    if task.error.startswith("FOREMAN SENT THIS BACK"):
        prior = ("\nThe foreman reviewed the finished work and sent it back. Your earlier changes are still on disk."
                 f"\n{task.error}\nFix every point, re-verify, then complete_task.")
    elif task.error:
        prior = f"\nA previous attempt failed: {task.error}\nAvoid repeating that."
    else:
        prior = ""
    messages: list = [
        {"role": "system", "content": system},
        {"role": "user", "content": "Start your task now." + prior},
    ]
    nudged = False
    for step in range(cfg.max_steps_per_task):
        if stop is not None and stop.is_set():
            return False, "stopped by foreman", tools.evidence()
        if step and step % 8 == 0:
            mail = state.inbox(name)
            if mail:
                messages.append({"role": "user", "content": "New mail:\n" + "\n".join(f"from {m.sender}: {m.body}" for m in mail)})
        msg = llm.chat(messages, tools=tools.schemas())
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            if nudged:
                return False, "worker stopped calling tools without complete_task: " + (msg.get("content") or "")[:300], tools.evidence()
            nudged = True
            messages.append({"role": "user", "content": "Keep working with tools. When finished call complete_task (or fail_task)."})
            continue
        for call in calls:
            fn = call.get("function") or {}
            try:
                result = tools.call(fn.get("name", ""), fn.get("arguments", "{}"))
            except TaskFinished as fin:
                return fin.ok, fin.text, tools.evidence()
            messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": result})
        if step == cfg.max_steps_per_task - 3:
            messages.append({"role": "user", "content": "You have 2 steps left. Wrap up: verify and call complete_task or fail_task."})
    return False, f"ran out of steps ({cfg.max_steps_per_task})", tools.evidence()


def worker_loop(state: RunState, name: str, llm: LLM, cfg, vision: LLM | None, stop: threading.Event,
                on_task_end=None, review_fn=None) -> None:
    """review_fn(task, summary, evidence) -> (approved, feedback), or None to queue the task
    for a host-side review (MCP mode). Without review_fn, finished tasks are done at once."""
    while not stop.is_set():
        task = state.claim_next(name)
        if task is None:
            if not state.pending():
                return
            stop.wait(1.0)  # others are still working on deps
            continue
        state.log(name, f"claimed {task.id}: {task.title}")
        try:
            ok, text, evidence = run_task(state, name, task, llm, cfg, vision, stop)
        except BudgetExceeded as e:
            state.finish_task(task.id, False, error=str(e), retry_limit=0)
            state.log(name, str(e))
            stop.set()
            return
        except Exception as e:  # network errors etc: give the task back
            ok, text, evidence = False, f"crashed: {e}", {}
        if ok and review_fn is not None:
            state.submit_for_review(task.id, text)
            try:
                verdict = review_fn(task, text, evidence)
            except BudgetExceeded as e:
                state.log(name, str(e))
                stop.set()
                return
            except Exception as e:  # never strand a task in review because the reviewer call broke
                verdict = (True, f"review unavailable ({e}); accepted unreviewed")
                state.add_finding("swarm", f"WARNING: {task.id} was accepted without a foreman review: {e}", task.id)
            if verdict is None:
                state.log(name, f"finished {task.id}; waiting for the foreman's review")
                continue
            approved, feedback = verdict
            status = state.apply_verdict(task.id, approved, feedback, cfg.max_task_reviews)
            state.log("foreman", f"review {task.id}: {'approved' if approved else 'sent back'} -- {feedback[:200]}")
            if on_task_end:
                on_task_end(task, status == "done")
            continue
        state.finish_task(task.id, ok, summary=text if ok else "", error="" if ok else text,
                          retry_limit=cfg.max_task_attempts)
        state.log(name, f"{'done' if ok else 'failed'} {task.id}: {text[:200]}")
        if on_task_end:
            on_task_end(task, ok)
