from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
HOOKS = ROOT / ".codex" / "hooks"


def run_hook(name: str, payload: object, **environment: str):
    env = os.environ.copy()
    env.pop("AURA_WORK_ITEM", None)
    env.pop("AURA_AGENT_ROLE", None)
    env.update(environment)
    return subprocess.run(
        ["python3", str(HOOKS / name)], cwd=ROOT, env=env,
        input=json.dumps(payload), text=True, capture_output=True, check=False,
    )


class HookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = ROOT / "work-items" / "active" / "AURA-0001.yaml"
        shutil.copyfile(ROOT / "work-items" / "archived" / "AURA-0001.yaml", cls.fixture)
        base = yaml.safe_load(cls.fixture.read_text(encoding="utf-8"))
        cls.extra_fixtures = []
        variants = {
            "AURA-0900": {"change_class": "documentation", "human_approval_required": False, "allowed_paths": [".codex/**"], "baseline_changes_allowed": False},
            "AURA-0901": {"change_class": "evaluation", "human_approval_required": True, "allowed_paths": ["evaluations/golden-results/**"], "forbidden_paths": ["services/**"], "baseline_changes_allowed": False},
            "AURA-0902": {"change_class": "evaluation", "human_approval_required": True, "allowed_paths": ["evaluations/golden-results/**"], "forbidden_paths": ["services/**"], "baseline_changes_allowed": True},
        }
        for identifier, changes in variants.items():
            item = dict(base)
            item.update(id=identifier, **changes)
            path = ROOT / "work-items" / "active" / f"{identifier}.yaml"
            path.write_text(yaml.safe_dump(item, sort_keys=False), encoding="utf-8")
            cls.extra_fixtures.append(path)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.unlink(missing_ok=True)
        for path in cls.extra_fixtures:
            path.unlink(missing_ok=True)

    def test_no_work_item_blocks_patch(self):
        result = run_hook("pre-tool-scope-guard.py", {"tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch\n*** Add File: docs/x.md\n+x\n*** End Patch"}})
        output = json.loads(result.stdout)
        self.assertEqual("deny", output["hookSpecificOutput"]["permissionDecision"])

    def test_first_active_work_item_can_be_created(self):
        result = run_hook(
            "pre-tool-scope-guard.py",
            {"tool_name": "apply_patch", "tool_input": {"command": f"*** Begin Patch\n*** Add File: {ROOT / 'work-items' / 'active' / 'AURA-0127.yaml'}\n+id: AURA-0127\n*** End Patch"}},
        )
        self.assertEqual({}, json.loads(result.stdout))

    def test_bootstrap_patch_cannot_include_other_paths(self):
        command = "*** Begin Patch\n*** Add File: work-items/active/AURA-0127.yaml\n+id: AURA-0127\n*** Add File: docs/x.md\n+x\n*** End Patch"
        result = run_hook("pre-tool-scope-guard.py", {"tool_name": "apply_patch", "tool_input": {"command": command}})
        self.assertEqual("deny", json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"])

    def test_valid_work_item_allows_in_scope_patch(self):
        result = run_hook(
            "pre-tool-scope-guard.py",
            {"tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch\n*** Add File: docs/development/x.md\n+x\n*** End Patch"}},
            AURA_WORK_ITEM="AURA-0001",
        )
        self.assertEqual({}, json.loads(result.stdout))

    def test_forbidden_path_is_denied(self):
        result = run_hook(
            "pre-tool-scope-guard.py",
            {"tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch\n*** Add File: services/x.py\n+x\n*** End Patch"}},
            AURA_WORK_ITEM="AURA-0001",
        )
        self.assertEqual("deny", json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"])

    def test_read_only_role_is_denied(self):
        result = run_hook(
            "pre-tool-scope-guard.py",
            {"tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch\n*** Add File: docs/development/x.md\n+x\n*** End Patch"}},
            AURA_WORK_ITEM="AURA-0001", AURA_AGENT_ROLE="general_reviewer",
        )
        self.assertEqual("deny", json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"])

    def test_critical_path_requires_authorization(self):
        payload = {"tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch\n*** Update File: .codex/config.toml\n@@\n-old\n+new\n*** End Patch"}}
        denied = run_hook("pre-tool-scope-guard.py", payload, AURA_WORK_ITEM="AURA-0900")
        self.assertEqual("deny", json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"])
        allowed = run_hook("pre-tool-scope-guard.py", payload, AURA_WORK_ITEM="AURA-0001")
        self.assertEqual({}, json.loads(allowed.stdout))

    def test_baseline_change_requires_explicit_permission(self):
        payload = {"tool_name": "apply_patch", "tool_input": {"command": "*** Begin Patch\n*** Update File: evaluations/golden-results/result.yaml\n@@\n-old\n+new\n*** End Patch"}}
        denied = run_hook("pre-tool-scope-guard.py", payload, AURA_WORK_ITEM="AURA-0901")
        self.assertEqual("deny", json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"])
        allowed = run_hook("pre-tool-scope-guard.py", payload, AURA_WORK_ITEM="AURA-0902")
        self.assertEqual({}, json.loads(allowed.stdout))

    def test_valid_review_handoff(self):
        message = """```yaml
agent: general_reviewer
work_item: AURA-0001
verdict: PASS
checked: [scope]
findings: []
```"""
        result = run_hook("validate-subagent-handoff.py", {"agent_type": "general_reviewer", "last_assistant_message": message, "stop_hook_active": False})
        self.assertEqual({}, json.loads(result.stdout))

    def test_invalid_handoff_continues_once(self):
        result = run_hook("validate-subagent-handoff.py", {"agent_type": "general_reviewer", "last_assistant_message": "done", "stop_hook_active": False})
        self.assertEqual("block", json.loads(result.stdout)["decision"])
        result = run_hook("validate-subagent-handoff.py", {"agent_type": "general_reviewer", "last_assistant_message": "done", "stop_hook_active": True})
        self.assertEqual({}, json.loads(result.stdout))

    def test_malformed_hook_input_fails_closed(self):
        env = os.environ.copy()
        env.pop("AURA_WORK_ITEM", None)
        result = subprocess.run(["python3", str(HOOKS / "pre-tool-scope-guard.py")], cwd=ROOT, env=env, input="not-json", text=True, capture_output=True, check=False)
        self.assertEqual("deny", json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"])

    def test_stop_hook_reentry_does_not_loop(self):
        result = run_hook("validate-turn-completion.py", {"last_assistant_message": "implemented and tested", "stop_hook_active": True})
        self.assertEqual({}, json.loads(result.stdout))

    def test_completion_requires_structured_handoff(self):
        result = run_hook(
            "validate-turn-completion.py",
            {"last_assistant_message": "Implementation complete; tests passed.", "stop_hook_active": False},
            AURA_WORK_ITEM="AURA-0001",
        )
        self.assertEqual("block", json.loads(result.stdout)["decision"])

    def test_completion_accepts_handoff_with_required_checks(self):
        message = """Implementation complete.
```yaml
agent: platform_worker
work_item: AURA-0001
status: complete
runtime: {model: gpt-5.6-luna, reasoning_effort: high, execution_tier: efficient, agent_thread_id: test, worktree: test}
scope: {ownership_zone: repository.governance, allowed_paths_respected: true}
commits: []
files_changed: []
contracts: {changed: false}
migrations: {changed: false}
observability: {components: [], metrics_added: []}
verification:
  passed:
    - python3 -m unittest discover -s tooling/architecture/tests -v
    - python3 tooling/architecture/validate-repository-shape.py
    - codex execpolicy check --rules .codex/rules/repository.rules -- git status
  failed: []
  not_run: []
decisions: []
risks: []
unresolved: []
```"""
        result = run_hook(
            "validate-turn-completion.py",
            {"last_assistant_message": message, "stop_hook_active": False},
            AURA_WORK_ITEM="AURA-0001",
        )
        self.assertEqual({}, json.loads(result.stdout))


if __name__ == "__main__":
    unittest.main()
