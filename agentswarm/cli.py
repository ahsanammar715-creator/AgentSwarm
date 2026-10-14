"""agentswarm CLI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, workspace
from .config import load_config
from .state import RunState
from .swarm import Swarm, find_run

EXAMPLE_TOML = """# agentswarm.toml -- put in your repo root or ~/.agentswarm.toml
# Prices are USD per 1M tokens; set them to your provider's real prices so the cost line is honest.

[foreman]            # the expensive model: plans, intervenes, audits, final pass
model = "gpt-6.1"
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
reasoning_effort = "high"
input_price = 5.0
cached_input_price = 0.5
output_price = 20.0

[lead]               # splits the plan into tasks (cheap)
model = "deepseek-chat"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"
input_price = 0.27
cached_input_price = 0.07
output_price = 1.10

[worker]             # does all the grinding (cheap)
model = "deepseek-chat"
base_url = "https://api.deepseek.com/v1"
api_key_env = "DEEPSEEK_API_KEY"
input_price = 0.27
cached_input_price = 0.07
output_price = 1.10

# [vision]           # optional: critiques screenshots in --design mode (defaults to the foreman)
# model = "gpt-6.1"

[swarm]
workers = 4
max_steps_per_task = 40
review_rounds = 2
use_worktree = true
# budget_usd = 3.0
"""


def _repo(args) -> Path:
    return workspace.repo_root(Path(args.repo).resolve())


def cmd_run(args) -> int:
    repo = _repo(args)
    cfg = load_config(repo)
    if args.workers:
        cfg.workers = args.workers
    if args.budget is not None:
        cfg.budget_usd = args.budget
    if args.no_worktree:
        cfg.use_worktree = False
    goal = args.goal if args.goal != "-" else sys.stdin.read()
    plan = Path(args.plan_file).read_text() if args.plan_file else ""
    swarm = Swarm.start(repo, goal, cfg, design=args.design, plan=plan)
    print(f"run {swarm.state.run_id}  workdir={swarm.state.workdir}")
    phase = swarm.run()
    _finish_message(swarm, phase)
    return 0 if phase == "done" else 1


def cmd_resume(args) -> int:
    repo = _repo(args)
    cfg = load_config(repo)
    if args.budget is not None:
        cfg.budget_usd = args.budget
    swarm = Swarm.resume(find_run(repo, args.run_id), cfg)
    if swarm.state.phase == "awaiting_review" and swarm.foreman is not None:
        swarm.state.phase = "audit"  # CLI has its own foreman
    phase = swarm.run()
    _finish_message(swarm, phase)
    return 0 if phase == "done" else 1


def _finish_message(swarm: Swarm, phase: str) -> None:
    print()
    if phase == "done":
        print(swarm.state.report)
        print(f"report: {swarm.state.run_dir / 'report.md'}")
    else:
        print(swarm.status())
        print(f"\nstopped at phase={phase}. resume with: agentswarm resume {swarm.state.run_id}")


def cmd_status(args) -> int:
    repo = _repo(args)
    run_dir = find_run(repo, args.run_id)
    swarm = Swarm(RunState.load(run_dir), load_config(repo), host_foreman=True)
    print(swarm.status())
    return 0


def cmd_report(args) -> int:
    run_dir = find_run(_repo(args), args.run_id)
    p = run_dir / "report.md"
    print(p.read_text() if p.exists() else "no report yet (run not finished)")
    return 0


def cmd_list(args) -> int:
    runs = _repo(args) / ".agentswarm" / "runs"
    for p in sorted(runs.glob("*/state.json")):
        s = RunState.load(p.parent)
        print(f"{s.run_id}  phase={s.phase:<16} branch={s.branch or '-':<28} {s.goal[:60]!r}")
    return 0


def cmd_merge(args) -> int:
    repo = _repo(args)
    s = RunState.load(find_run(repo, args.run_id))
    if not s.branch:
        print("this run worked directly in your checkout; nothing to merge")
        return 0
    if s.phase != "done" and not args.force:
        print(f"run is at phase {s.phase}, not done. Use --force to merge anyway.")
        return 1
    print(workspace.git(repo, "merge", "--no-ff", s.branch, "-m", f"Merge swarm run {s.run_id}"))
    if not args.keep:
        workspace.remove_worktree(repo, Path(s.workdir))
        print(f"removed worktree {s.workdir} (branch {s.branch} kept)")
    return 0


def cmd_clean(args) -> int:
    repo = _repo(args)
    s = RunState.load(find_run(repo, args.run_id))
    if s.branch:
        workspace.remove_worktree(repo, Path(s.workdir))
        print(f"removed worktree {s.workdir}; branch {s.branch} kept (delete with: git branch -D {s.branch})")
    return 0


def cmd_init(args) -> int:
    p = _repo(args) / "agentswarm.toml"
    if p.exists():
        print(f"{p} already exists")
        return 1
    p.write_text(EXAMPLE_TOML)
    print(f"wrote {p}")
    return 0


def cmd_doctor(args) -> int:
    cfg = load_config(_repo(args))
    ok = True
    for role in ("foreman", "lead", "worker", "vision"):
        m = getattr(cfg, role)
        if m is None:
            print(f"{role:8} (unset; falls back to foreman)")
            continue
        has = "ok" if m.api_key else f"MISSING ${m.api_key_env}"
        ok &= bool(m.api_key) or role == "foreman"
        print(f"{role:8} {m.model:<22} {m.base_url:<34} key: {has}")
    try:
        import playwright  # noqa: F401
        print("design   playwright installed")
    except ImportError:
        print("design   playwright not installed (needed only for --design)")
    print(f"swarm    workers={cfg.workers} worktree={cfg.use_worktree} budget={cfg.budget_usd}")
    if not cfg.foreman.api_key:
        print("note: no foreman key -> CLI runs need --plan-file and skip the audit; MCP mode uses your host model as foreman")
    return 0 if ok else 1


def cmd_mcp(args) -> int:
    from .mcp_server import main

    main()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="agentswarm", description="Expensive foreman, cheap swarm.")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--repo", default=".", help="path inside the target git repo")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="start a swarm on a goal")
    p.add_argument("goal", help="what to build/fix ('-' reads stdin)")
    p.add_argument("--design", action="store_true", help="UI mode: screenshot + critique loops at phone and desktop sizes")
    p.add_argument("--plan-file", help="skip foreman planning and use this plan")
    p.add_argument("--workers", type=int)
    p.add_argument("--budget", type=float, help="hard stop at this many USD")
    p.add_argument("--no-worktree", action="store_true", help="work directly in your checkout (not recommended)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("resume", help="resume an interrupted run")
    p.add_argument("run_id", nargs="?")
    p.add_argument("--budget", type=float)
    p.set_defaults(fn=cmd_resume)

    for name, fn, helptext in (
        ("status", cmd_status, "compressed status + cost line"),
        ("report", cmd_report, "print the final report"),
        ("clean", cmd_clean, "remove a run's worktree (keeps the branch)"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("run_id", nargs="?")
        p.set_defaults(fn=fn)

    p = sub.add_parser("merge", help="merge a finished run's branch into your current branch")
    p.add_argument("run_id", nargs="?")
    p.add_argument("--force", action="store_true")
    p.add_argument("--keep", action="store_true", help="keep the worktree")
    p.set_defaults(fn=cmd_merge)

    sub.add_parser("list", help="list runs").set_defaults(fn=cmd_list)
    sub.add_parser("init", help="write an example agentswarm.toml").set_defaults(fn=cmd_init)
    sub.add_parser("doctor", help="check keys and config").set_defaults(fn=cmd_doctor)
    sub.add_parser("mcp", help="run as an MCP stdio server (Codex / Claude Code)").set_defaults(fn=cmd_mcp)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except (FileNotFoundError, workspace.GitError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
