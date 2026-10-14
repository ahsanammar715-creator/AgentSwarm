"""End-to-end tests with a scripted fake model (no network, no keys spent)."""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentswarm import workspace  # noqa: E402
from agentswarm.config import SwarmConfig  # noqa: E402
from agentswarm.llm import LLM, CostLedger, parse_json  # noqa: E402
from agentswarm.mcp_server import Server  # noqa: E402
from agentswarm.state import DONE, RunState  # noqa: E402
from agentswarm.swarm import Swarm, find_run  # noqa: E402
from agentswarm.tools import Toolbox, TaskFinished  # noqa: E402


def _resp(content=None, tool_calls=None, usage=(1000, 200)):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [
            {"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
            for i, (n, a) in enumerate(tool_calls)
        ]
    return {"choices": [{"message": msg}], "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}}


REVIEWS = []


def fake_post(self, body):
    msgs = body["messages"]
    system = msgs[0]["content"] if isinstance(msgs[0]["content"], str) else ""
    if "checking ONE task" in system:
        task_id = re.search(r"TASK \[(.+?)\]", msgs[-1]["content"]).group(1)
        REVIEWS.append((task_id, msgs[-1]["content"]))
        # send t1 back once, approve everything else
        if task_id == "t1" and sum(1 for t, _ in REVIEWS if t == "t1") == 1:
            return _resp(json.dumps({"verdict": "redo", "feedback": "1. add a test run"}))
        return _resp(json.dumps({"verdict": "approve", "feedback": "ok"}))
    if "write the plan" in system:
        return _resp(json.dumps({"plan": "Add greet.py and test it.", "acceptance": ["greet() works"], "ui": False}))
    if "TEAM LEAD" in system:
        return _resp(json.dumps({"tasks": [
            {"id": "t1", "title": "write greet", "description": "create greet.py", "files": ["greet.py"]},
            {"id": "t2", "title": "verify", "description": "run it", "deps": ["t1"]},
        ]}))
    if "doing the audit" in system:
        if "fix-1" in msgs[-1]["content"]:
            return _resp(json.dumps({"verdict": "pass", "summary": "good", "issues": [], "cleanup_tasks": []}))
        return _resp(json.dumps({"verdict": "needs_work", "summary": "needs a docstring", "issues": [],
                                 "cleanup_tasks": [{"id": "fix-1", "title": "docstring", "description": "add a docstring"}]}))
    if "final pass" in system:
        return _resp(json.dumps({"recommendation": "merge", "report": "Built greet."}))
    # worker
    task_id = re.search(r"YOUR TASK \[(.+?)\]", system).group(1)
    n_tool = sum(1 for m in msgs if m["role"] == "tool")
    if task_id == "t1":
        script = [
            [("write_file", {"path": "greet.py", "content": "def greet(n):\n    return 'hi ' + n\n"})],
            [("add_finding", {"text": "run python files with python3"})],
            [("complete_task", {"summary": "wrote greet.py"})],
        ]
    elif task_id == "t2":
        script = [
            [("run", {"command": "python3 -c \"import greet; print(greet.greet('x'))\""})],
            [("complete_task", {"summary": "verified: hi x"})],
        ]
    else:
        script = [
            [("edit_file", {"path": "greet.py", "old": "def greet(n):\n", "new": "def greet(n):\n    \"\"\"Say hi.\"\"\"\n"})],
            [("complete_task", {"summary": "docstring added"})],
        ]
    return _resp(tool_calls=script[min(n_tool, len(script) - 1)], usage=(500, 50))


def make_repo(tmp: Path) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "README.md").write_text("demo\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


ENV = {"OPENAI_API_KEY": "x", "DEEPSEEK_API_KEY": "y"}


class SwarmTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = make_repo(Path(self.tmp.name))
        self.cfg = SwarmConfig()
        self.cfg.workers = 2

    def tearDown(self):
        self.tmp.cleanup()

    @mock.patch.dict(os.environ, ENV)
    @mock.patch.object(LLM, "_post", fake_post)
    def test_full_run_with_cleanup_round(self):
        REVIEWS.clear()
        sw = Swarm.start(self.repo, "add a greet function", self.cfg, log=lambda *a: None)
        self.assertEqual(sw.run(), "done")
        s = sw.state
        self.assertEqual([t.status for t in s.tasks], [DONE, DONE, DONE])
        self.assertEqual(s.round, 2)
        self.assertIn("Say hi", (Path(s.workdir) / "greet.py").read_text())
        # user's checkout untouched; work lives on the swarm branch
        self.assertFalse((self.repo / "greet.py").exists())
        self.assertIn("greet.py", workspace.git(self.repo, "show", "--stat", s.branch))
        self.assertIn("Recommendation: merge", s.report)
        self.assertIn("worker", sw.ledger.to_dict())
        self.assertGreater(sw.ledger.total, 0)
        self.assertTrue(s.findings)
        # every finished task got exactly one end-of-task look, t1 twice (sent back once)
        reviewed = [t for t, _ in REVIEWS]
        self.assertEqual(reviewed.count("t1"), 2)
        self.assertIn("t2", reviewed)
        self.assertIn("fix-1", reviewed)
        self.assertEqual(s.get_task("t1").reviews, 1)
        t1_packet = next(p for t, p in REVIEWS if t == "t1")
        self.assertIn("+def greet(n):", t1_packet)  # reviewer sees the task's own diff
        self.assertIn("reviewer", sw.ledger.to_dict())
    @mock.patch.dict(os.environ, ENV)
    @mock.patch.object(LLM, "_post", fake_post)
    def test_resume_after_crash(self):
        sw = Swarm.start(self.repo, "add a greet function", self.cfg, log=lambda *a: None)
        sw.phase_plan()
        sw.phase_split()
        # simulate a crash mid-task
        t = sw.state.claim_next("worker-1")
        self.assertEqual(t.status, "in_progress")
        sw.state.add_finding("worker-1", "remember me")
        resumed = Swarm.resume(find_run(self.repo, sw.state.run_id), self.cfg, log=lambda *a: None)
        self.assertEqual(resumed.state.get_task("t1").status, "todo")
        self.assertEqual(resumed.run(), "done")
        self.assertIn("remember me", [f.text for f in resumed.state.findings])

    @mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "y"}, clear=False)
    @mock.patch.object(LLM, "_post", fake_post)
    def test_mcp_host_foreman_flow(self):
        os.environ.pop("OPENAI_API_KEY", None)
        srv = Server()
        out = srv.swarm_start({"repo": str(self.repo), "goal": "greet", "plan": "Add greet.py"})
        run_id = re.search(r"run_id=(\S+)", out).group(1)
        r = {"repo": str(self.repo), "run_id": run_id}
        seen = []
        for _ in range(10):
            status = srv.swarm_status({**r, "wait_ms": 20000})
            if "wake: task-review" not in status:
                break
            packet = srv.swarm_task_review(r)
            tid = re.search(r"TASK \[(.+?)\]", packet).group(1)
            seen.append(tid)
            if tid == "t1" and seen.count("t1") == 1:
                self.assertIn("error", srv.swarm_verdict({**r, "task_id": tid, "approve": False}))
                srv.swarm_verdict({**r, "task_id": tid, "approve": False, "feedback": "1. add a test"})
            else:
                srv.swarm_verdict({**r, "task_id": tid, "approve": True})
        srv.live[run_id][1].join(30)
        self.assertEqual(seen, ["t1", "t1", "t2"])
        self.assertIn("phase=awaiting_review", srv.swarm_status(r))
        review = srv.swarm_review({"repo": str(self.repo), "run_id": run_id})
        self.assertIn("greet.py", review)
        srv.swarm_send_back({"repo": str(self.repo), "run_id": run_id,
                             "tasks": [{"id": "fix-1", "title": "docstring", "description": "add docstring"}]})
        for _ in range(5):
            if "wake: task-review" not in srv.swarm_status({**r, "wait_ms": 20000}):
                break
            srv.swarm_verdict({**r, "task_id": "fix-1", "approve": True})
        srv.live[run_id][1].join(30)
        done = srv.swarm_finish({"repo": str(self.repo), "run_id": run_id, "report": "ok"})
        self.assertIn("branch=swarm/", done)
        self.assertNotIn("foreman", srv._swarm({"repo": str(self.repo), "run_id": run_id}).ledger.to_dict())

    def test_mcp_jsonrpc(self):
        srv = Server()
        init = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "agentswarm")
        tools = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertIn("swarm_start", [t["name"] for t in tools["result"]["tools"]])
        self.assertIsNone(srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "runs").mkdir()
        self.state = RunState.create(root / "runs", goal="g", repo=str(root), workdir=str(root), design=True)
        self.task = self.state.add_tasks([{"id": "u1", "title": "page", "description": "d", "ui": True}])[0]
        self.tb = Toolbox(self.state, "worker-1", self.task, SwarmConfig())

    def tearDown(self):
        self.tmp.cleanup()

    def test_path_escape_blocked(self):
        self.assertIn("escapes", self.tb.call("read_file", json.dumps({"path": "../../etc/passwd"})))

    def test_dangerous_command_blocked(self):
        self.assertIn("blocked", self.tb.call("run", json.dumps({"command": "git push origin main"})))

    def test_run_hides_api_keys(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "secret"}):
            out = self.tb.call("run", json.dumps({"command": "echo ${OPENAI_API_KEY:-none}"}))
        self.assertIn("none", out)

    def test_design_mode_requires_iterated_screenshots(self):
        out = self.tb.call("complete_task", json.dumps({"summary": "looks great!"}))
        self.assertIn("design evidence missing", out)
        ev = self.state.ui_evidence.setdefault("u1", {"edits": 0, "shots": {}})
        ev["shots"] = {"mobile": [1, 1], "desktop": [1, 2]}  # mobile shot twice without an edit
        self.assertIn("mobile: 1/2", self.tb.call("complete_task", json.dumps({"summary": "x"})))
        ev["shots"]["mobile"].append(2)
        with self.assertRaises(TaskFinished):
            self.tb.call("complete_task", json.dumps({"summary": "x"}))


class MiscTest(unittest.TestCase):
    def test_parse_json_fenced(self):
        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json('sure: {"a": 2} done'), {"a": 2})

    def test_cost_ledger_cached_and_budget(self):
        from agentswarm.config import ModelConfig

        cfg = ModelConfig("m", "u", "K", input_price=1.0, cached_input_price=0.1, output_price=2.0)
        led = CostLedger(budget_usd=0.002)
        led.record("worker", cfg, {"prompt_tokens": 1000, "completion_tokens": 500,
                                   "prompt_tokens_details": {"cached_tokens": 500}})
        self.assertAlmostEqual(led.total, (500 * 1 + 500 * 0.1 + 500 * 2) / 1e6)
        led.record("worker", cfg, {"prompt_tokens": 1000, "completion_tokens": 0})
        from agentswarm.llm import BudgetExceeded

        with self.assertRaises(BudgetExceeded):
            led.check_budget()


if __name__ == "__main__":
    unittest.main()
