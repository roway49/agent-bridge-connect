"""Container half of the host terminal-dialog handshake; no business writes."""
from __future__ import annotations
import json
import sys
from .terminal_delivery import StageOutcome, TERMINAL_DELIVERY_EXTENSION_KEY as KEY, terminal_delivery_eligible
from .terminal_delivery_coordinator import TerminalDeliveryCoordinator

BOARD = '/data/workspace/record'


def discover(board=BOARD):
    coordinator = TerminalDeliveryCoordinator(board)
    result = []
    for task in coordinator.store.list_tasks():
        receipt = (task.get('extensions') or {}).get(KEY) or {}
        stage = receipt.get('stages', {}).get('ui_notification', {})
        if terminal_delivery_eligible(task) and receipt.get('delivery_id') and stage.get('state') not in ('succeeded', 'not_applicable'):
            result.append({'task_id': task['id'], 'delivery_id': receipt['delivery_id']})
    return result


def deliver(task_id, delivery_id, exchange, board=BOARD):
    coordinator = TerminalDeliveryCoordinator(board)
    def notify(payload):
        # Invoked under the coordinator's existing task lock. Recheck the exact
        # receipt before allowing host UI; retry/handoff cannot reuse old proof.
        receipt = coordinator.receipt(task_id)
        if receipt.get('delivery_id') != delivery_id:
            return StageOutcome(False, deferred=True)
        reply = exchange({'task_id': task_id, 'delivery_id': delivery_id, 'payload': payload})
        if not isinstance(reply, dict) or reply.get('delivery_id') != delivery_id or reply.get('shown') is not True:
            return StageOutcome(False, deferred=True)
        return StageOutcome(True)
    coordinator._ui_notifier = notify
    return coordinator.deliver_now(task_id)


def repair_legacy(task_id, board=BOARD):
    """Explicit migration of stdout-only success; never replay actual host UI."""
    from .terminal_delivery import build_terminal_delivery_receipt
    coordinator = TerminalDeliveryCoordinator(board)
    with coordinator._task_lock(task_id):
        task = coordinator.store.read_task(task_id)
        receipt = coordinator.receipt(task_id)
        events = coordinator.store.read_events(task_id)
        notices = [e for e in events if e.get('event_type') == 'notification_delivery' and e.get('notification_event') != 'task.input_required' and e.get('dialog_message')]
        if not terminal_delivery_eligible(task) or not notices or notices[-1].get('dialog_message') != 'headless terminal notification written':
            raise ValueError('No exact legacy stdout-only notification evidence')
        if receipt['stages']['ui_notification']['state'] != 'succeeded':
            return {'status': 'already_pending'}
        receipt['stages']['ui_notification'] = build_terminal_delivery_receipt(task_id, terminal_state=receipt['terminal_state'], terminal_event=receipt['terminal_event'])['stages']['ui_notification']
        coordinator._persist_receipt(task, receipt, [{'stage': 'ui_notification', 'status': 'legacy_stdout_corrected'}], receipt['updated_at'])
        return {'status': 'pending_host_delivery'}


def main():
    action = sys.argv[1]
    if action == 'discover':
        result = discover()
    elif action == 'deliver':
        def exchange(value):
            print(json.dumps(value), flush=True)
            line = sys.stdin.readline(4096)
            return json.loads(line) if line else {}
        result = deliver(sys.argv[2], sys.argv[3], exchange)
    elif action == 'repair-legacy':
        result = repair_legacy(sys.argv[2])
    else:
        raise ValueError('Unknown terminal delivery operation')
    print(json.dumps({'result': result}), flush=True)


if __name__ == '__main__':
    main()
