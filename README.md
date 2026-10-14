# AgentSwarm

An expensive model (GPT‑5.6 Sol by default) works as the **foreman**: it plans, supervises, audits and
signs off, and it never touches the code. A team of **cheap agents** (DeepSeek by default, or any
OpenAI-compatible model) does the grinding: reading files, editing, running tests, fixing.

Your expensive tokens go to thinking and checking. The cheap tokens go to the grinding.

```
            GPT-5.6 Sol (foreman)
   plan ──► intervene on failures ──► audit every changed file ──► final pass
     │                ▲                      │
     ▼                │ compressed summary   ▼ cleanup tasks
  team lead ──► task board ◄──► worker-1..N ──► mailbox · findings ledger
  (cheap)        (deps)          (cheap, in an isolated git worktree)
```

There are no runtime dependencies beyond Python 3.11+ and git. Playwright is optional and only needed
for design mode.

## What you get

- **Foreman/worker split.** The foreman writes the plan and the acceptance criteria. A cheap lead splits
  them into tasks with dependencies, and N workers pull those tasks off a shared board in parallel.
- **Team plumbing.** Workers share a task board, a mailbox (to each other, to `all`, or to the
  foreman), and a findings ledger. They record what they learn there, such as the test command or a
  convention, so nobody has to rediscover it.
- **Compressed supervision.** The foreman only ever sees a short summary: one line per task, recent
  findings, mail addressed to it, and the cost line. It steps in when a task fails for good.
- **Audit and cleanup rounds.** The foreman audits the diff of every changed file and sends a cleanup
  team back in with fix tasks. That repeats up to `review_rounds` times, then the foreman does a final
  pass and writes a report with a merge or don't-merge recommendation.
- **Git worktree isolation (on by default).** The team works on branch `swarm/<run_id>` in
  `.agentswarm/worktrees/`, so your checkout is never touched. You review the branch and merge it.
- **Design mode (`--design`).** UI tasks have to screenshot every screen at phone (390×844) and
  desktop (1440×900) sizes and get a vision critique of what's actually on screen. They need at least 2
  rounds per viewport, each one after an edit. `complete_task` refuses to finish without that evidence,
  so a worker can't just declare "looks great!".
- **Resume.** All state is persisted in `.agentswarm/runs/<id>/state.json`. After a crash or restart,
  `agentswarm resume` puts in-flight tasks back on the board and keeps the findings ledger.
- **Cost line.** Spend is tracked per role (foreman, lead, worker, vision), including cached-token
  pricing. An optional `budget_usd` sets a hard stop.
- **Guard rails.** File tools can't leave the project or touch `.git`. Shell commands block
  `git push`, `reset --hard`, `sudo` and similar, and API keys are stripped from worker shells. This
  is a seatbelt, not a sandbox, so run untrusted goals in a container.

## Quick start (standalone CLI, GPT-5.6 Sol as foreman)

```bash
pip install -e .                 # or: pip install -e '.[design]' && playwright install chromium
export OPENAI_API_KEY=...        # foreman
export DEEPSEEK_API_KEY=...      # lead + workers
cd your-repo
agentswarm init                  # writes agentswarm.toml; check the model names and prices
agentswarm doctor
agentswarm run "Add rate limiting to the /api/login route with tests" --budget 3
agentswarm run "Build a landing page for the pricing tiers in docs/pricing.md" --design
```

While it runs:

```bash
agentswarm status                # compressed board + cost line
agentswarm list
agentswarm resume [run_id]       # after a crash / Ctrl-C / budget stop (e.g. --budget 5)
agentswarm report [run_id]
agentswarm merge [run_id]        # merges swarm/<id> into your current branch, removes the worktree
agentswarm clean [run_id]        # drop the worktree, keep the branch
```

## Daily driver inside Codex or Claude Code (the host model is the foreman)

In MCP mode **your host session's model is the foreman**. It plans, polls the compressed status,
audits with `swarm_review`, sends cleanup rounds back with `swarm_send_back`, and closes with
`swarm_finish`. You only need a key for the cheap team.

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.agentswarm]
command = "python3"
args = ["-m", "agentswarm", "mcp"]
env = { DEEPSEEK_API_KEY = "sk-..." }
```

Copy `skills/swarm/SKILL.md` into your `AGENTS.md`, or into your skills folder, so the host knows the
foreman workflow.

**Claude Code:** this repo is a plugin. It ships `.claude-plugin/plugin.json`, `.mcp.json` and the
`swarm` skill. Alternatively, run `claude mcp add agentswarm -- python3 -m agentswarm mcp`.

MCP tools: `swarm_start`, `swarm_status`, `swarm_message`, `swarm_review`, `swarm_send_back`,
`swarm_finish`, `swarm_resume`, `swarm_stop`.

## Configuration

Settings are read in this order, with later sources winning: `~/.agentswarm.toml`, then
`<repo>/agentswarm.toml`, then environment variables (`SWARM_FOREMAN_MODEL`, `SWARM_WORKER_MODEL`,
`SWARM_<ROLE>_BASE_URL`, `SWARM_<ROLE>_API_KEY_ENV`, `SWARM_WORKERS`, `SWARM_BUDGET_USD`,
`SWARM_NO_WORKTREE`). Run `agentswarm init` to get a commented template.

Every role takes any OpenAI-compatible endpoint, such as OpenAI, DeepSeek, OpenRouter, Together, vLLM
or Ollama. **Check the model IDs and set real prices.** The defaults (`gpt-5.6-sol`, `deepseek-chat`,
and the per-million-token prices) are placeholders, and the cost line is only as accurate as the
prices you put in.

## How a run flows

1. **plan:** the foreman sees the goal plus a cheap repo snapshot (file list and README head). It
   writes the plan and the acceptance criteria.
2. **split:** the lead turns the plan into tasks with `deps`, so tasks touching the same file never
   run in parallel.
3. **execute:** workers claim ready tasks and run a tool loop (`search`, `read_file`, `edit_file`,
   `run`, `add_finding`, `send_message`, `complete_task`, …). A failed task is retried once with the
   error attached. If it fails for good, the foreman steps in with the compressed summary: it can
   message workers, re-queue tasks, add tasks or abort.
4. **audit:** the foreman reviews the team report, the findings, and the diff of every changed file.
   If anything needs work, it creates cleanup tasks and the run goes back to step 3.
5. **final:** the foreman writes `report.md` with a recommendation. Everything is committed on the
   swarm branch.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests drive full runs against a scripted fake model, so they need no network and spend nothing.
They cover the full run with a cleanup round, crash and resume, the MCP host-foreman flow, design-mode
enforcement, and the guard rails.

## Caveats

- Workers run real shell commands in the worktree. Treat it like letting a junior engineer loose on a
  branch.
- Design mode needs a vision-capable model (`[vision]`, which defaults to the foreman's settings) and
  Playwright.
- Cheap models sometimes stall or loop. The step cap, retry limit, foreman intervention and budget
  are there to stop that from burning money.

Inspired by [DotSwarm](https://github.com/Druidia-Bot/DotSwarm).
