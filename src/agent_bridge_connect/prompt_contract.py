"""Shared executor prompt contract builder (PROMPT-001).

Builds the common AgentBC prompt contract: task identity, steps,
project/artifact/report ownership rules, long-task progress guidance, the
strict v1 final marker block, and resumed-input context.

Mechanical-migration stage: this builder reproduces the previous per-Adapter
output byte for byte. Codex and Hermes keep their legacy layout that repeats
the common rules after every step (``rules_section=False``); Claude keeps its
"Rules:" section layout (``rules_section=True``). The PROMPT-001 text change
that emits the common rules exactly once per prompt lands in a separate
commit on top of this one; the golden snapshots in
``tests/test_prompt_contract.py`` freeze the exact current output.

The strict v1 completion contract is not weakened here: a zero CLI exit
without a valid single-line ``AGENTBC_FINAL_CALLBACK`` marker still fails,
``input_required`` still requires at least one declared blocked step, and
report/record ownership stays with AgentBC Core. No completion v2 sidecar is
introduced.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from typing import Any

from agent_bridge_connect.execution_contract import FINAL_CALLBACK_PREFIX
from agent_bridge_connect.protocol import resumed_input_prompt_lines, task_step_text

#: Common rules every executor prompt must carry. Wording is the stable
#: contract; adapters must not re-emit these lines themselves.
COMMON_RULES = (
    "Write user deliverables only under the Artifact root named above. Never write "
    "deliverables directly in the AgentBC workspace root, report directory, or record directory.",
    "If customer_dir is true, edit the existing project in place and do not copy it "
    "into the AgentBC workspace.",
    "If any path is rejected as outside allowed roots, stop and report the configuration "
    "problem; never copy the project/file to an allowed AgentBC directory to bypass the rejection.",
    "If this task continues an existing deliverable, modify the existing baseline "
    "instead of creating a sibling project directory.",
    "AgentBC Core owns the execution report. Do not write or replace REPORT.md.",
)

PROGRESS_LEAD = "For long-running work, refresh AgentBC progress at least every few minutes:"

MARKER_LEAD = (
    "Your final response must end with exactly one single-line terminal marker "
    "and no text after it:"
)
INPUT_REQUIRED_RULE = (
    "Use final_state input_required only with at least one declared step status blocked; "
    "plain permission or approval prose is not a valid stop."
)
CHOICE_SPEC = (
    'For a two-option user decision, include "input":{"type":"choice","reason":"why the user must decide",'
    '"options":[{"label":"Option A","description":"what A does or changes"},{"label":"Option B",'
    '"description":"what B does or changes"}]}; give a concrete reason and a concrete description for '
    "each option. Labels must be distinct and at most 48 characters; descriptions must be at most 160 "
    "characters. Use type message for free text and type permission only for approve/deny."
)
ZERO_EXIT_RULE = (
    "A zero CLI exit without a valid marker fails the task. completed means flow execution "
    "ended, not user acceptance or quality approval."
)


@dataclass(frozen=True)
class PromptPlatformExtras:
    """Real platform differences an Adapter appends to the shared contract."""

    opening: str = "You are executing a structured AgentBC task."
    task_id_line: bool = False
    image_note: str | None = None
    image_inputs: tuple[str, ...] = ()
    image_inspect_line: str | None = None
    image_rule: str | None = None
    summary_line: str = "After completing all steps, write a summary of what you did."
    extra_rules: tuple[str, ...] = ()
    rules_section: bool = False


def build_prompt_contract(
    task_packet: dict[str, Any],
    extras: PromptPlatformExtras | None = None,
) -> str:
    """Build the shared AgentBC prompt contract for a task packet.

    Legacy layout (``rules_section=False``, Codex/Hermes): the common rules,
    image rule, summary line and progress guidance are repeated after every
    step. Section layout (``rules_section=True``, Claude): the common and
    platform rules are emitted once under a "Rules:" heading before the steps.
    Both layouts share the identity header, lineage block, strict final-marker
    block and resumed-input context.
    """
    platform = extras or PromptPlatformExtras()
    title = str(task_packet.get("title") or task_packet.get("task_id") or "Untitled task")
    workspace = task_packet.get("workspace") if isinstance(task_packet.get("workspace"), dict) else {}
    task_board = task_packet.get("task_board") if isinstance(task_packet.get("task_board"), dict) else {}
    board_root = str(task_board.get("root") or "")
    task_id = str(task_packet.get("task_id") or "")
    lineage = {}
    if isinstance(task_packet.get("extensions"), dict):
        value = task_packet["extensions"].get("agentbc.lineage")
        lineage = value if isinstance(value, dict) else {}
    progress_command = (
        f"agentbc task progress {shlex.quote(task_id)} --root {shlex.quote(board_root)} "
        '--summary "describe current progress"'
    )
    steps = task_packet.get("steps") or []
    resume_context = resumed_input_prompt_lines(task_packet)

    lines = [platform.opening, ""]
    if platform.task_id_line:
        lines.append(f"Task ID: {task_id}")
    lines.extend(
        [
            f"Task: {title}",
            f"Project root: {workspace.get('project_root') or workspace.get('root', '')}",
            f"Artifact root: {workspace.get('artifact_root') or workspace.get('artifacts_dir', '')}",
            f"Report directory: {workspace.get('report_root') or workspace.get('output_dir', '')}",
            f"Task brief: {workspace.get('task_file', '')}",
            f"Report: {workspace.get('report_file', '')}",
            "",
        ]
    )

    if platform.rules_section:
        lines.append("Rules:")
        for rule in COMMON_RULES:
            lines.append(f"- {rule}")
        if platform.image_rule:
            lines.append(f"- {platform.image_rule}")
        for rule in platform.extra_rules:
            lines.append(f"- {rule}")
        lines.append("")
        lines.append("Steps:")
        if resume_context:
            lines.extend(["", *resume_context, ""])
        for index, step in enumerate(steps, 1):
            lines.append(f"{index}. {task_step_text(step)} [status: {step.get('status', 'pending')}]")
        tail = ["", platform.summary_line, PROGRESS_LEAD, progress_command]
    else:
        lines.append("Steps:")
        if platform.image_inputs and platform.image_note:
            lines.append("")
            lines.append(platform.image_note)
            lines.extend(f"- {image}" for image in platform.image_inputs)
            if platform.image_inspect_line:
                lines.append(platform.image_inspect_line)
        if resume_context:
            lines.extend(["", *resume_context, ""])
        for index, step in enumerate(steps, 1):
            lines.append(f"{index}. {task_step_text(step)} [status: {step.get('status', 'pending')}]")
            # Legacy layout: the platform image rule sits between the fourth
            # and fifth common rules; the summary and progress guidance close
            # the per-step block.
            lines.extend(["", *COMMON_RULES[:4]])
            if platform.image_rule:
                lines.append(platform.image_rule)
            lines.append(COMMON_RULES[4])
            lines.extend([platform.summary_line, PROGRESS_LEAD, progress_command])
        tail = []

    if lineage:
        lines.extend(
            [
                "",
                f"Iteration chain root: {lineage.get('chain_root_task_id', '')}",
                f"Base task: {lineage.get('base_task_id', '')}",
                f"Task code: {lineage.get('task_code', workspace.get('task_code', ''))}",
                f"Iteration: {lineage.get('iteration_index', workspace.get('iteration', ''))}",
                f"Base artifact root: {lineage.get('base_artifacts_dir', workspace.get('artifacts_dir', ''))}",
            ]
        )

    step_results = ",".join(
        f'{{"id":{step.get("id", index)},"status":"done"}}'
        for index, step in enumerate(steps, 1)
    )
    lines.extend(tail)
    lines.extend(
        [
            "",
            MARKER_LEAD,
            (
                f'{FINAL_CALLBACK_PREFIX} {{"version":1,"task_id":{json.dumps(task_id)},'
                f'"final_state":"completed","summary":"concise summary",'
                f'"step_results":[{step_results}]}}'
            ),
            INPUT_REQUIRED_RULE,
            CHOICE_SPEC,
            ZERO_EXIT_RULE,
        ]
    )
    return "\n".join(lines)
