"""Shared executor prompt contract builder (PROMPT-001).

Builds the common AgentBC prompt contract exactly once per prompt: task
identity, steps, project/artifact/report ownership rules, long-task progress
guidance, the strict v1 final marker block, and resumed-input context.
Each Executor Adapter appends only its real platform differences through
:class:`PromptPlatformExtras` (opening wording, task-id line, image input
notes and the image-generation rule, adapter-only rules, and the summary
line). argv and permission semantics stay in the adapter command builders.

The common rules are emitted once under a "Rules:" heading and are never
repeated per step, so a ten-step task stays close to the 3,000-character
budget for the common contract text.

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

#: Common rules emitted once per prompt for every executor. Wording is the
#: stable contract; adapters must not re-emit these lines themselves.
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
    "an access, sandbox, approval, or permission blocker must use input.type permission with "
    "requested_permission full, a concrete reason of at most 240 characters, and exactly one "
    "declared step status blocked; "
    "keep all other steps pending or done, include no native flags, and never use message or choice. "
    "Plain permission or approval prose is not a valid stop, and free-text message responses can "
    "never grant access."
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


@dataclass(frozen=True)
class PromptPlatformExtras:
    """Real platform differences an Adapter appends to the shared contract."""

    opening: str = "You are executing a structured AgentBC task."
    task_id_line: bool = False
    image_note: str | None = None
    image_inputs: tuple[str, ...] = ()
    image_rule: str | None = None
    summary_line: str = "After completing all steps, write a summary of what you did."
    extra_rules: tuple[str, ...] = ()


def build_prompt_contract(
    task_packet: dict[str, Any],
    extras: PromptPlatformExtras | None = None,
) -> str:
    """Build the shared AgentBC prompt contract exactly once per prompt.

    The returned text contains the common identity, steps, ownership rules,
    progress guidance, strict v1 final marker and resumed-input context.
    Platform differences come only from ``extras``.
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
            "Rules:",
        ]
    )
    for rule in COMMON_RULES:
        lines.append(f"- {rule}")
    if platform.image_rule:
        lines.append(f"- {platform.image_rule}")
    for rule in platform.extra_rules:
        lines.append(f"- {rule}")

    if platform.image_note and platform.image_inputs:
        lines.extend(["", platform.image_note])
        lines.extend(f"- {image}" for image in platform.image_inputs)
        if len(platform.image_inputs) > 1:
            lines.append(
                "Inspect those images as task inputs. Do not copy them merely to make them accessible."
            )
        else:
            lines.append(
                "Inspect that image as a task input. Do not copy it merely to make it accessible."
            )
    else:
        lines.append("")
    lines.append("Steps:")

    resume_context = resumed_input_prompt_lines(task_packet)
    if resume_context:
        lines.extend(["", *resume_context, ""])
    for index, step in enumerate(task_packet.get("steps") or [], 1):
        lines.append(f"{index}. {task_step_text(step)} [status: {step.get('status', 'pending')}]")

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
        for index, step in enumerate(task_packet.get("steps") or [], 1)
    )
    lines.extend(
        [
            "",
            platform.summary_line,
            PROGRESS_LEAD,
            progress_command,
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
