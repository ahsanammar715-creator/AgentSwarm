"""Git worktree isolation: the swarm works on its own branch, never your checkout."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(cwd: Path | str, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def repo_root(path: Path | str) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel"))


def ensure_state_dir(repo: Path) -> Path:
    """<repo>/.agentswarm, kept out of git via .git/info/exclude (no .gitignore edits)."""
    root = repo / ".agentswarm"
    (root / "runs").mkdir(parents=True, exist_ok=True)
    (root / "worktrees").mkdir(parents=True, exist_ok=True)
    exclude = Path(git(repo, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = repo / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ""
    if ".agentswarm/" not in existing.splitlines():
        with exclude.open("a") as fh:
            fh.write(("\n" if existing and not existing.endswith("\n") else "") + ".agentswarm/\n")
    return root


def head_ref(repo: Path) -> str:
    out = git(repo, "rev-parse", "HEAD", check=False)
    if not out or "HEAD" in out:
        raise GitError("repository has no commits yet; make an initial commit first")
    return out


def create_worktree(repo: Path, run_id: str) -> tuple[Path, str, str]:
    base = head_ref(repo)
    branch = f"swarm/{run_id}"
    path = ensure_state_dir(repo) / "worktrees" / run_id
    git(repo, "worktree", "add", "-b", branch, str(path), base)
    return path, branch, base


def commit_all(workdir: Path, message: str) -> bool:
    git(workdir, "add", "-A")
    if not git(workdir, "status", "--porcelain"):
        return False
    git(
        workdir,
        "-c", "user.name=agentswarm",
        "-c", "user.email=agentswarm@localhost",
        "commit", "-q", "-m", message,
    )
    return True


def changed_files(workdir: Path, base: str | None) -> list[str]:
    files: set[str] = set()
    if base:
        files.update(filter(None, git(workdir, "diff", "--name-only", base).splitlines()))
    for line in git(workdir, "status", "--porcelain").splitlines():
        files.add(line[3:].split(" -> ")[-1].strip('"'))
    return sorted(f for f in files if not f.startswith(".agentswarm/"))


def file_diff(workdir: Path, base: str | None, path: str, limit: int = 6000) -> str:
    if base:
        out = git(workdir, "diff", base, "--", path, check=False)
    else:
        out = git(workdir, "diff", "HEAD", "--", path, check=False)
    if not out:
        p = Path(workdir) / path
        if p.is_file():
            out = "(new or untracked file)\n" + p.read_text(errors="replace")
        else:
            out = "(deleted)"
    if len(out) > limit:
        out = out[:limit] + f"\n... [{len(out) - limit} more chars truncated]"
    return out


def remove_worktree(repo: Path, path: Path) -> None:
    git(repo, "worktree", "remove", "--force", str(path), check=False)
