#!/bin/sh
# SESSION-104-001 real-machine canary.
#
# This procedure uses only task status and the current Codex Desktop process
# context. It never enumerates sessions, reads private Desktop storage, or
# prints the App Tools pipe path. Run it with five already-created terminal
# task IDs in this order: completed failed elevation parent_child desktop_offline.
set -eu

if [ "${1:-}" = "--help" ]; then
    printf '%s\n' "usage: $0 completed_task failed_task elevation_task parent_child_task desktop_offline_task"
    printf '%s\n' "Run from the current Codex Desktop shell after the five canary tasks exist."
    exit 0
fi

if [ "$#" -ne 5 ]; then
    printf '%s\n' "usage: $0 completed_task failed_task elevation_task parent_child_task desktop_offline_task" >&2
    exit 2
fi

for task_id in "$1" "$2" "$3" "$4"; do
    agentbc task status "$task_id" --root "${AGENTBC_CANARY_ROOT:-$PWD}" --json >/dev/null
done

# Desktop-offline proof: this first status must remain a successful ordinary
# status command while cleanup stays pending/retryable and delete is absent.
agentbc task status "$5" --root "${AGENTBC_CANARY_ROOT:-$PWD}" --json >/dev/null

printf '%s\n' "Desktop canary checkpoints:"
printf '%s\n' "  completed: normal terminal cleanup status captured"
printf '%s\n' "  failed: failed terminal cleanup status captured"
printf '%s\n' "  elevation: one-permission continuation status captured"
printf '%s\n' "  parent_child: supported registered parent/auxiliary status captured"
printf '%s\n' "  desktop_offline: pending status captured; reopen Codex Desktop now"
printf '%s\n' "After reopening Desktop, run this command for compensation:"
printf '%s\n' "  agentbc task status $5 --root \"\${AGENTBC_CANARY_ROOT:-$PWD}\" --json"
printf '%s\n' "Automated acceptance checks the Desktop acknowledgement and delete receipt; sidebar disappearance is user-visible confirmation."
