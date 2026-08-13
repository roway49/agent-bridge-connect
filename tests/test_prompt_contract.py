"""PROMPT-001 characterization/golden tests for the executor prompt contracts.

Freezes the CURRENT Codex, Claude and Hermes prompt contract behavior that
must stay stable across the PROMPT-001 shared-builder refactor:

- task identity (task id/title, project/artifact/report ownership lines)
- once-per-prompt rules: deliverables location, customer_dir, path
  rejection, existing-baseline, report ownership by AgentBC Core
- long-task progress guidance and the exact progress command
- the exact strict v1 final-marker block (single-line marker example,
  input_required rule, choice/message/permission input spec, zero-exit rule)
- resumed-input context (prior request, user response, keep evidence)
- image input notes (Codex multi-image, Hermes single-image, Claude none)
- permission-mode independence (permission lives in CLI argv, not prompt text)

AFTER sizes recorded from the shared-builder implementation (10-step task,
common rules emitted exactly once for every executor):
- Codex:   3,440 chars total (was 12,892 with the rules repeated 10x)
- Hermes:  3,451 chars total (was 12,930 with the rules repeated 10x)
- Claude:  3,924 chars total (rules are still emitted once)
- Shared builder default extras: 3,252 chars total, 2,852 chars of common
  contract text after removing the ten variable step lines.
PROMPT-001 keeps the common contract text for a ten-step task at or below the
approximately 3,000-character budget and forbids per-step repetition.
"""

from __future__ import annotations

import json
import unittest

from agent_bridge_connect.execution_contract import FINAL_CALLBACK_PREFIX
from agent_bridge_connect.executors.claude import _build_prompt as claude_prompt
from agent_bridge_connect.executors.codex import _build_prompt as codex_prompt
from agent_bridge_connect.executors.hermes import _build_prompt as hermes_prompt
from agent_bridge_connect.prompt_contract import build_prompt_contract

# --- exact contract rule texts ----------------------------------------------

DELIVERABLES_RULE = (
    "Write user deliverables only under the Artifact root named above. Never write "
    "deliverables directly in the AgentBC workspace root, report directory, or record directory."
)
CUSTOMER_DIR_RULE = (
    "If customer_dir is true, edit the existing project in place and do not copy it "
    "into the AgentBC workspace."
)
PATH_REJECTION_RULE = (
    "If any path is rejected as outside allowed roots, stop and report the configuration "
    "problem; never copy the project/file to an allowed AgentBC directory to bypass the rejection."
)
BASELINE_RULE = (
    "If this task continues an existing deliverable, modify the existing baseline "
    "instead of creating a sibling project directory."
)
REPORT_OWNERSHIP_RULE = "AgentBC Core owns the execution report. Do not write or replace REPORT.md."
COMMON_RULES = (
    DELIVERABLES_RULE,
    CUSTOMER_DIR_RULE,
    PATH_REJECTION_RULE,
    BASELINE_RULE,
    REPORT_OWNERSHIP_RULE,
)
CODEX_IMAGE_RULE = (
    "For image generation or image editing work, use the native image-generation "
    "capability and save the final bitmap deliverables under the Artifact root; do not "
    "return only prose or preview links."
)
HERMES_IMAGE_RULE = (
    "For image generation or image editing work, use the native image_generate "
    "capability and save the final bitmap deliverables under the Artifact root; do not "
    "return only prose or preview links."
)
MARKER_LEAD = "Your final response must end with exactly one single-line terminal marker and no text after it:"
INPUT_REQUIRED_RULE = (
    "Use final_state input_required only with at least one declared step status blocked; "
    "an access, sandbox, approval, or permission blocker must use input.type permission with "
    "requested_permission full and must never use message or choice. Plain permission or approval "
    "prose is not a valid stop, and free-text message responses can never grant access."
)
CHOICE_SPEC = (
    'For a two-option user decision, include "input":{"type":"choice","reason":"why the user must decide",'
    '"options":[{"label":"Option A","description":"what A does or changes"},{"label":"Option B",'
    '"description":"what B does or changes"}]}; give a concrete reason and a concrete description for '
    "each option. Labels must be distinct and at most 48 characters; descriptions must be at most 160 "
    "characters. Use type message only for non-permission free text and type permission only for "
    "approve/deny access confirmation."
)
ZERO_EXIT_RULE = (
    "A zero CLI exit without a valid marker fails the task. completed means flow execution "
    "ended, not user acceptance or quality approval."
)
PROGRESS_LEAD = "For long-running work, refresh AgentBC progress at least every few minutes:"
PROGRESS_COMMAND = 'agentbc task progress TEST-001 --root /tmp/abc-record --summary "describe current progress"'

CLAUDE_EXTRA_RULES = (
    "Do not claim user acceptance. completed only means your agent turn is finished and ready for user review.",
    "Do not create Claude-internal tasks/todos. The AgentBC task record and report are the only execution ledger.",
    "If the step asks another agent to execute or review work, use the AgentBC CLI handoff/dispatch command instead of doing that agent's work inline.",
    "Keep required long-running commands in the foreground with a tool timeout longer than the expected runtime.",
    "If Claude Code moves a command to the background, use BashOutput repeatedly until it exits. Never end this turn while a required background command is still running.",
)

# --- golden snapshots (frozen before the shared-builder refactor) ------------

GOLDEN_CODEX = """You are executing a structured task.

Task: Golden contract task
Project root: /tmp/abc-worktree/artifacts
Artifact root: /tmp/abc-worktree/artifacts
Report directory: /tmp/abc-report/2026-08-08/TEST
Task brief: /tmp/abc-report/2026-08-08/TEST/TEST-001-task.md
Report: /tmp/abc-report/2026-08-08/TEST/TEST-001-report.md

Rules:
- Write user deliverables only under the Artifact root named above. Never write deliverables directly in the AgentBC workspace root, report directory, or record directory.
- If customer_dir is true, edit the existing project in place and do not copy it into the AgentBC workspace.
- If any path is rejected as outside allowed roots, stop and report the configuration problem; never copy the project/file to an allowed AgentBC directory to bypass the rejection.
- If this task continues an existing deliverable, modify the existing baseline instead of creating a sibling project directory.
- AgentBC Core owns the execution report. Do not write or replace REPORT.md.
- For image generation or image editing work, use the native image-generation capability and save the final bitmap deliverables under the Artifact root; do not return only prose or preview links.

Steps:
1. Step 1 description. [status: pending]
2. Step 2 description. [status: pending]

Iteration chain root: TEST-001
Base task: TEST-001
Task code: TEST
Iteration: 1
Base artifact root: /tmp/abc-worktree/artifacts

After completing all steps, write a summary of what you did.
For long-running work, refresh AgentBC progress at least every few minutes:
agentbc task progress TEST-001 --root /tmp/abc-record --summary "describe current progress"

Your final response must end with exactly one single-line terminal marker and no text after it:
AGENTBC_FINAL_CALLBACK: {"version":1,"task_id":"TEST-001","final_state":"completed","summary":"concise summary","step_results":[{"id":1,"status":"done"},{"id":2,"status":"done"}]}
Use final_state input_required only with at least one declared step status blocked; an access, sandbox, approval, or permission blocker must use input.type permission with requested_permission full and must never use message or choice. Plain permission or approval prose is not a valid stop, and free-text message responses can never grant access.
For a two-option user decision, include "input":{"type":"choice","reason":"why the user must decide","options":[{"label":"Option A","description":"what A does or changes"},{"label":"Option B","description":"what B does or changes"}]}; give a concrete reason and a concrete description for each option. Labels must be distinct and at most 48 characters; descriptions must be at most 160 characters. Use type message only for non-permission free text and type permission only for approve/deny access confirmation.
A zero CLI exit without a valid marker fails the task. completed means flow execution ended, not user acceptance or quality approval."""

GOLDEN_CLAUDE = """You are executing a structured AgentBC task with Claude Code.

Task ID: TEST-001
Task: Golden contract task
Project root: /tmp/abc-worktree/artifacts
Artifact root: /tmp/abc-worktree/artifacts
Report directory: /tmp/abc-report/2026-08-08/TEST
Task brief: /tmp/abc-report/2026-08-08/TEST/TEST-001-task.md
Report: /tmp/abc-report/2026-08-08/TEST/TEST-001-report.md

Rules:
- Write user deliverables only under the Artifact root named above. Never write deliverables directly in the AgentBC workspace root, report directory, or record directory.
- If customer_dir is true, edit the existing project in place and do not copy it into the AgentBC workspace.
- If any path is rejected as outside allowed roots, stop and report the configuration problem; never copy the project/file to an allowed AgentBC directory to bypass the rejection.
- If this task continues an existing deliverable, modify the existing baseline instead of creating a sibling project directory.
- AgentBC Core owns the execution report. Do not write or replace REPORT.md.
- Do not claim user acceptance. completed only means your agent turn is finished and ready for user review.
- Do not create Claude-internal tasks/todos. The AgentBC task record and report are the only execution ledger.
- If the step asks another agent to execute or review work, use the AgentBC CLI handoff/dispatch command instead of doing that agent's work inline.
- Keep required long-running commands in the foreground with a tool timeout longer than the expected runtime.
- If Claude Code moves a command to the background, use BashOutput repeatedly until it exits. Never end this turn while a required background command is still running.

Steps:
1. Step 1 description. [status: pending]
2. Step 2 description. [status: pending]

Iteration chain root: TEST-001
Base task: TEST-001
Task code: TEST
Iteration: 1
Base artifact root: /tmp/abc-worktree/artifacts

After completing all steps, print a concise summary.
For long-running work, refresh AgentBC progress at least every few minutes:
agentbc task progress TEST-001 --root /tmp/abc-record --summary "describe current progress"

Your final response must end with exactly one single-line terminal marker and no text after it:
AGENTBC_FINAL_CALLBACK: {"version":1,"task_id":"TEST-001","final_state":"completed","summary":"concise summary","step_results":[{"id":1,"status":"done"},{"id":2,"status":"done"}]}
Use final_state input_required only with at least one declared step status blocked; an access, sandbox, approval, or permission blocker must use input.type permission with requested_permission full and must never use message or choice. Plain permission or approval prose is not a valid stop, and free-text message responses can never grant access.
For a two-option user decision, include "input":{"type":"choice","reason":"why the user must decide","options":[{"label":"Option A","description":"what A does or changes"},{"label":"Option B","description":"what B does or changes"}]}; give a concrete reason and a concrete description for each option. Labels must be distinct and at most 48 characters; descriptions must be at most 160 characters. Use type message only for non-permission free text and type permission only for approve/deny access confirmation.
A zero CLI exit without a valid marker fails the task. completed means flow execution ended, not user acceptance or quality approval."""

GOLDEN_HERMES = """You are executing a structured AgentBC task.

Task: Golden contract task
Project root: /tmp/abc-worktree/artifacts
Artifact root: /tmp/abc-worktree/artifacts
Report directory: /tmp/abc-report/2026-08-08/TEST
Task brief: /tmp/abc-report/2026-08-08/TEST/TEST-001-task.md
Report: /tmp/abc-report/2026-08-08/TEST/TEST-001-report.md

Rules:
- Write user deliverables only under the Artifact root named above. Never write deliverables directly in the AgentBC workspace root, report directory, or record directory.
- If customer_dir is true, edit the existing project in place and do not copy it into the AgentBC workspace.
- If any path is rejected as outside allowed roots, stop and report the configuration problem; never copy the project/file to an allowed AgentBC directory to bypass the rejection.
- If this task continues an existing deliverable, modify the existing baseline instead of creating a sibling project directory.
- AgentBC Core owns the execution report. Do not write or replace REPORT.md.
- For image generation or image editing work, use the native image_generate capability and save the final bitmap deliverables under the Artifact root; do not return only prose or preview links.

Steps:
1. Step 1 description. [status: pending]
2. Step 2 description. [status: pending]

Iteration chain root: TEST-001
Base task: TEST-001
Task code: TEST
Iteration: 1
Base artifact root: /tmp/abc-worktree/artifacts

Return a concise execution summary and mention any files changed.
For long-running work, refresh AgentBC progress at least every few minutes:
agentbc task progress TEST-001 --root /tmp/abc-record --summary "describe current progress"

Your final response must end with exactly one single-line terminal marker and no text after it:
AGENTBC_FINAL_CALLBACK: {"version":1,"task_id":"TEST-001","final_state":"completed","summary":"concise summary","step_results":[{"id":1,"status":"done"},{"id":2,"status":"done"}]}
Use final_state input_required only with at least one declared step status blocked; an access, sandbox, approval, or permission blocker must use input.type permission with requested_permission full and must never use message or choice. Plain permission or approval prose is not a valid stop, and free-text message responses can never grant access.
For a two-option user decision, include "input":{"type":"choice","reason":"why the user must decide","options":[{"label":"Option A","description":"what A does or changes"},{"label":"Option B","description":"what B does or changes"}]}; give a concrete reason and a concrete description for each option. Labels must be distinct and at most 48 characters; descriptions must be at most 160 characters. Use type message only for non-permission free text and type permission only for approve/deny access confirmation.
A zero CLI exit without a valid marker fails the task. completed means flow execution ended, not user acceptance or quality approval."""

# --- test packet builders ----------------------------------------------------

REPORT_ROOT = "/tmp/abc-report/2026-08-08/TEST"
RECORD_ROOT = "/tmp/abc-record"


def managed_packet(
    step_count: int = 2,
    task_id: str = "TEST-001",
    title: str = "Golden contract task",
    steps: list[dict] | None = None,
) -> dict:
    if steps is None:
        steps = [{"id": i, "description": f"Step {i} description."} for i in range(1, step_count + 1)]
    return {
        "task_id": task_id,
        "title": title,
        "steps": steps,
        "workspace": {
            "customer_dir": False,
            "root": "/tmp/abc-worktree",
            "project_root": "/tmp/abc-worktree/artifacts",
            "artifact_root": "/tmp/abc-worktree/artifacts",
            "artifacts_dir": "/tmp/abc-worktree/artifacts",
            "report_root": REPORT_ROOT,
            "output_dir": REPORT_ROOT,
            "task_file": f"{REPORT_ROOT}/{task_id}-task.md",
            "report_file": f"{REPORT_ROOT}/{task_id}-report.md",
            "task_code": "TEST",
            "iteration": 1,
        },
        "task_board": {"root": RECORD_ROOT},
        "extensions": {
            "agentbc.lineage": {
                "chain_root_task_id": task_id,
                "base_task_id": task_id,
                "task_code": "TEST",
                "iteration_index": 1,
                "base_artifacts_dir": "/tmp/abc-worktree/artifacts",
            },
            "agentbc.permission": {
                "requested_mode": "safe",
                "effective_mode": "safe",
                "selection_source": "configured_default",
            },
        },
    }


def customer_packet(task_id: str = "TEST-001") -> dict:
    packet = managed_packet(task_id=task_id)
    packet["workspace"] = dict(packet["workspace"])
    packet["workspace"].update(
        {
            "customer_dir": True,
            "root": "/tmp/abc-customer",
            "project_root": "/tmp/abc-customer/project",
            "artifact_root": "/tmp/abc-customer/project",
            "artifacts_dir": "/tmp/abc-customer/project",
        }
    )
    packet["extensions"] = dict(packet["extensions"])
    packet["extensions"]["agentbc.lineage"] = dict(packet["extensions"]["agentbc.lineage"])
    packet["extensions"]["agentbc.lineage"]["base_artifacts_dir"] = "/tmp/abc-customer/project"
    return packet


def with_resume(packet: dict) -> dict:
    packet = dict(packet)
    packet["extensions"] = dict(packet["extensions"])
    packet["extensions"]["agentbc.input"] = {
        "input_id": "inp-1",
        "executor_run_id": "codex-run-1",
        "blocked_step_id": 2,
        "type": "choice",
        "summary": "Choose the integration target.",
        "status": "answered",
        "response": {"type": "choice", "summary": "User picked option B (staging)."},
    }
    return packet


def with_images(packet: dict, paths: list[str]) -> dict:
    packet = dict(packet)
    packet["extensions"] = dict(packet["extensions"])
    packet["extensions"]["agentbc.media"] = {"images": list(paths)}
    return packet


def with_permission(packet: dict, mode: str) -> dict:
    packet = dict(packet)
    packet["extensions"] = dict(packet["extensions"])
    packet["extensions"]["agentbc.permission"] = {
        "requested_mode": mode,
        "effective_mode": mode,
        "selection_source": "test",
    }
    return packet


def assert_strict_marker_block(tc: unittest.TestCase, prompt: str, task_id: str = "TEST-001", step_ids: tuple = (1, 2)) -> None:
    lines = prompt.splitlines()
    tc.assertEqual(lines[-1], ZERO_EXIT_RULE)
    tc.assertIn(MARKER_LEAD, prompt)
    step_results = ",".join(
        json.dumps({"id": step_id, "status": "done"}, separators=(",", ":")) for step_id in step_ids
    )
    marker = (
        f'{FINAL_CALLBACK_PREFIX} {{"version":1,"task_id":{json.dumps(task_id)},'
        f'"final_state":"completed","summary":"concise summary","step_results":[{step_results}]}}'
    )
    tc.assertEqual(prompt.count(marker), 1)
    tc.assertIn(INPUT_REQUIRED_RULE, prompt)
    tc.assertIn(CHOICE_SPEC, prompt)


# --- Codex prompt contract ---------------------------------------------------


class CodexPromptContractTests(unittest.TestCase):
    def test_golden_two_step_prompt(self):
        self.assertEqual(codex_prompt(managed_packet()), GOLDEN_CODEX)

    def test_ten_step_prompt_emits_common_rules_once(self):
        prompt = codex_prompt(managed_packet(step_count=10))
        self.assertEqual(prompt.count(DELIVERABLES_RULE), 1)
        self.assertEqual(prompt.count("agentbc task progress"), 1)
        self.assertEqual(len(prompt), 3440)

    def test_resumed_input_context(self):
        prompt = codex_prompt(with_resume(managed_packet()))
        self.assertIn("Resume context:", prompt)
        self.assertIn("- Prior input request (choice, step 2): Choose the integration target.", prompt)
        self.assertIn("- User response (choice): User picked option B (staging).", prompt)
        self.assertIn("- Keep completed-step evidence intact and continue only pending steps.", prompt)
        self.assertLess(prompt.index("Resume context:"), prompt.index("1. Step 1 description."))
        self.assertLess(prompt.index("Steps:"), prompt.index("Resume context:"))

    def test_image_inputs_attached_through_codex_interface(self):
        prompt = codex_prompt(
            with_images(managed_packet(), ["/Users/abc/artifacts/a.png", "/Users/abc/artifacts/b.png"])
        )
        self.assertIn("Image inputs are attached through the native Codex CLI image interface:", prompt)
        self.assertIn("- /Users/abc/artifacts/a.png", prompt)
        self.assertIn("- /Users/abc/artifacts/b.png", prompt)
        self.assertIn("Inspect those images as task inputs. Do not copy them merely to make them accessible.", prompt)

    def test_permission_modes_do_not_change_prompt(self):
        base = codex_prompt(managed_packet())
        for mode in ("inherit", "safe", "full"):
            self.assertEqual(codex_prompt(with_permission(managed_packet(), mode)), base)
        self.assertNotIn("--sandbox", base)
        self.assertNotIn("--yolo", base)

    def test_managed_and_customer_paths(self):
        managed = codex_prompt(managed_packet())
        customer = codex_prompt(customer_packet())
        self.assertIn("Project root: /tmp/abc-worktree/artifacts", managed)
        self.assertIn("Artifact root: /tmp/abc-worktree/artifacts", managed)
        self.assertIn("Project root: /tmp/abc-customer/project", customer)
        self.assertIn("Artifact root: /tmp/abc-customer/project", customer)
        for rule in COMMON_RULES:
            self.assertIn(rule, managed)
            self.assertIn(rule, customer)

    def test_strict_v1_final_marker(self):
        prompt = codex_prompt(managed_packet())
        assert_strict_marker_block(self, prompt)

    def test_progress_guidance(self):
        prompt = codex_prompt(managed_packet())
        self.assertIn(PROGRESS_LEAD, prompt)
        self.assertIn(PROGRESS_COMMAND, prompt)


# --- Claude prompt contract --------------------------------------------------


class ClaudePromptContractTests(unittest.TestCase):
    def test_golden_two_step_prompt(self):
        self.assertEqual(claude_prompt(managed_packet()), GOLDEN_CLAUDE)

    def test_ten_step_prompt_emits_common_rules_once(self):
        prompt = claude_prompt(managed_packet(step_count=10))
        self.assertEqual(prompt.count(DELIVERABLES_RULE), 1)
        self.assertEqual(prompt.count("agentbc task progress"), 1)
        self.assertEqual(len(prompt), 3924)

    def test_resumed_input_context(self):
        prompt = claude_prompt(with_resume(managed_packet()))
        self.assertIn("Resume context:", prompt)
        self.assertIn("- Prior input request (choice, step 2): Choose the integration target.", prompt)
        self.assertIn("- User response (choice): User picked option B (staging).", prompt)
        self.assertIn("- Keep completed-step evidence intact and continue only pending steps.", prompt)
        self.assertLess(prompt.index("Resume context:"), prompt.index("1. Step 1 description."))
        self.assertLess(prompt.index("Steps:"), prompt.index("Resume context:"))

    def test_image_inputs_are_not_supported(self):
        plain = claude_prompt(managed_packet())
        with_image = claude_prompt(with_images(managed_packet(), ["/Users/abc/artifacts/a.png"]))
        self.assertEqual(with_image, plain)
        self.assertNotIn("image", plain.lower())

    def test_permission_modes_do_not_change_prompt(self):
        base = claude_prompt(managed_packet())
        for mode in ("inherit", "safe", "full"):
            self.assertEqual(claude_prompt(with_permission(managed_packet(), mode)), base)
        self.assertNotIn("--dangerously", base)

    def test_managed_and_customer_paths(self):
        managed = claude_prompt(managed_packet())
        customer = claude_prompt(customer_packet())
        self.assertIn("Project root: /tmp/abc-worktree/artifacts", managed)
        self.assertIn("Project root: /tmp/abc-customer/project", customer)
        for rule in COMMON_RULES:
            self.assertIn(rule, managed)
            self.assertIn(rule, customer)

    def test_strict_v1_final_marker(self):
        prompt = claude_prompt(managed_packet())
        assert_strict_marker_block(self, prompt)

    def test_progress_guidance(self):
        prompt = claude_prompt(managed_packet())
        self.assertIn(PROGRESS_LEAD, prompt)
        self.assertIn(PROGRESS_COMMAND, prompt)

    def test_claude_platform_rules(self):
        prompt = claude_prompt(managed_packet())
        self.assertTrue(prompt.startswith("You are executing a structured AgentBC task with Claude Code.\n"))
        self.assertIn("Task ID: TEST-001", prompt)
        for rule in CLAUDE_EXTRA_RULES:
            self.assertIn(rule, prompt)


# --- Hermes prompt contract --------------------------------------------------


class HermesPromptContractTests(unittest.TestCase):
    def test_golden_two_step_prompt(self):
        self.assertEqual(hermes_prompt(managed_packet()), GOLDEN_HERMES)

    def test_ten_step_prompt_emits_common_rules_once(self):
        prompt = hermes_prompt(managed_packet(step_count=10))
        self.assertEqual(prompt.count(DELIVERABLES_RULE), 1)
        self.assertEqual(prompt.count("agentbc task progress"), 1)
        self.assertEqual(len(prompt), 3451)

    def test_resumed_input_context(self):
        prompt = hermes_prompt(with_resume(managed_packet()))
        self.assertIn("Resume context:", prompt)
        self.assertIn("- Prior input request (choice, step 2): Choose the integration target.", prompt)
        self.assertIn("- User response (choice): User picked option B (staging).", prompt)
        self.assertIn("- Keep completed-step evidence intact and continue only pending steps.", prompt)
        self.assertLess(prompt.index("Resume context:"), prompt.index("1. Step 1 description."))
        self.assertLess(prompt.index("Steps:"), prompt.index("Resume context:"))

    def test_single_image_input_attached_through_hermes_interface(self):
        prompt = hermes_prompt(with_images(managed_packet(), ["/Users/abc/artifacts/a.png"]))
        self.assertIn("An image input is attached through the native Hermes CLI image interface:", prompt)
        self.assertIn("- /Users/abc/artifacts/a.png", prompt)
        self.assertIn("Inspect that image as a task input. Do not copy it merely to make it accessible.", prompt)
        self.assertNotIn("/Users/abc/artifacts/b.png", prompt)

    def test_permission_modes_do_not_change_prompt(self):
        base = hermes_prompt(managed_packet())
        for mode in ("inherit", "safe", "full"):
            self.assertEqual(hermes_prompt(with_permission(managed_packet(), mode)), base)
        self.assertNotIn("--yolo", base)

    def test_managed_and_customer_paths(self):
        managed = hermes_prompt(managed_packet())
        customer = hermes_prompt(customer_packet())
        self.assertIn("Project root: /tmp/abc-worktree/artifacts", managed)
        self.assertIn("Project root: /tmp/abc-customer/project", customer)
        for rule in COMMON_RULES:
            self.assertIn(rule, managed)
            self.assertIn(rule, customer)

    def test_strict_v1_final_marker(self):
        prompt = hermes_prompt(managed_packet())
        assert_strict_marker_block(self, prompt)

    def test_progress_guidance(self):
        prompt = hermes_prompt(managed_packet())
        self.assertIn(PROGRESS_LEAD, prompt)
        self.assertIn(PROGRESS_COMMAND, prompt)


# --- cross-adapter required information --------------------------------------


class AllPromptContractTests(unittest.TestCase):
    ADAPTERS = (
        ("codex", codex_prompt, "You are executing a structured task."),
        ("claude", claude_prompt, "You are executing a structured AgentBC task with Claude Code."),
        ("hermes", hermes_prompt, "You are executing a structured AgentBC task."),
    )

    def test_required_information_present_in_all_three_prompts(self):
        for label, build, opening in self.ADAPTERS:
            with self.subTest(adapter=label):
                prompt = build(managed_packet())
                self.assertTrue(prompt.startswith(opening + "\n"))
                self.assertIn("Task: Golden contract task", prompt)
                self.assertIn("Project root: /tmp/abc-worktree/artifacts", prompt)
                self.assertIn("Artifact root: /tmp/abc-worktree/artifacts", prompt)
                self.assertIn("Report directory: /tmp/abc-report/2026-08-08/TEST", prompt)
                self.assertIn("Task brief: /tmp/abc-report/2026-08-08/TEST/TEST-001-task.md", prompt)
                self.assertIn("Report: /tmp/abc-report/2026-08-08/TEST/TEST-001-report.md", prompt)
                self.assertIn("1. Step 1 description. [status: pending]", prompt)
                self.assertIn("2. Step 2 description. [status: pending]", prompt)
                for rule in COMMON_RULES:
                    self.assertIn(rule, prompt)
                self.assertIn(PROGRESS_LEAD, prompt)
                self.assertIn(PROGRESS_COMMAND, prompt)
                self.assertIn("Iteration chain root: TEST-001", prompt)
                self.assertIn("Base task: TEST-001", prompt)
                self.assertIn("Task code: TEST", prompt)
                self.assertIn("Iteration: 1", prompt)
                self.assertIn("Base artifact root: /tmp/abc-worktree/artifacts", prompt)
                assert_strict_marker_block(self, prompt)

    def test_platform_differences_kept_per_adapter(self):
        codex = codex_prompt(managed_packet())
        claude = claude_prompt(managed_packet())
        hermes = hermes_prompt(managed_packet())
        self.assertIn(CODEX_IMAGE_RULE, codex)
        self.assertIn(HERMES_IMAGE_RULE, hermes)
        self.assertNotIn(CODEX_IMAGE_RULE, hermes)
        self.assertNotIn(HERMES_IMAGE_RULE, codex)
        self.assertIn("Task ID: TEST-001", claude)
        self.assertNotIn("Task ID:", codex)
        self.assertNotIn("Task ID:", hermes)
        self.assertIn("After completing all steps, write a summary of what you did.", codex)
        self.assertIn("Return a concise execution summary and mention any files changed.", hermes)
        self.assertIn("After completing all steps, print a concise summary.", claude)


# --- PROMPT-001 regression limits --------------------------------------------


class PromptContractRegressionTests(unittest.TestCase):
    """PROMPT-001 size/repetition limits for the shared prompt contract."""

    ADAPTERS = (
        ("codex", codex_prompt),
        ("claude", claude_prompt),
        ("hermes", hermes_prompt),
    )

    def test_ten_step_common_contract_text_within_budget(self):
        # Common contract text for a ten-step task: shared-builder output with
        # the ten variable step lines removed (identity, rules, lineage,
        # progress guidance and the strict marker block). Target <= ~3,000.
        prompt = build_prompt_contract(managed_packet(step_count=10))
        for index in range(1, 11):
            prompt = prompt.replace(
                f"{index}. Step {index} description. [status: pending]", ""
            )
        self.assertLessEqual(len(prompt), 3000)

    def test_ten_step_prompt_does_not_repeat_common_rules_per_step(self):
        for label, build in self.ADAPTERS:
            with self.subTest(adapter=label):
                prompt = build(managed_packet(step_count=10))
                self.assertEqual(prompt.count(DELIVERABLES_RULE), 1)
                self.assertEqual(prompt.count(PATH_REJECTION_RULE), 1)
                self.assertEqual(prompt.count(REPORT_OWNERSHIP_RULE), 1)
                self.assertEqual(prompt.count("agentbc task progress"), 1)
                self.assertEqual(prompt.count(MARKER_LEAD), 1)


if __name__ == "__main__":
    unittest.main()
