"""Test task.json and steps/N.json schema validation."""

import json
import unittest
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


class TaskSchemaTests(unittest.TestCase):
    """Validate task.json schema rules."""

    def setUp(self):
        self.valid = json.loads((FIXTURES / "sample_task.json").read_text())

    def test_load_valid_task(self):
        """Valid task.json should load without error."""
        from agent_bridge_connect.schema import validate_task

        errors = validate_task(self.valid)
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")

    def test_reject_missing_id(self):
        """Task without id field must be rejected."""
        from agent_bridge_connect.schema import validate_task

        data = dict(self.valid)
        del data["id"]
        errors = validate_task(data)
        self.assertTrue(any("id" in e.lower() for e in errors))

    def test_reject_missing_status(self):
        """Task without status field must be rejected."""
        from agent_bridge_connect.schema import validate_task

        data = dict(self.valid)
        del data["status"]
        errors = validate_task(data)
        self.assertTrue(any("status" in e.lower() for e in errors))

    def test_reject_invalid_status(self):
        """Task with unknown status must be rejected."""
        from agent_bridge_connect.schema import validate_task

        data = dict(self.valid)
        data["status"] = "flying"
        errors = validate_task(data)
        self.assertTrue(len(errors) > 0)

    def test_accept_all_valid_statuses(self):
        """All valid lifecycle statuses should pass."""
        from agent_bridge_connect.schema import validate_task

        valid_statuses = ["pending", "assigned", "in_progress", "review", "done", "failed", "blocked"]
        for status in valid_statuses:
            data = dict(self.valid)
            data["status"] = status
            errors = validate_task(data)
            self.assertEqual(errors, [], f"Status '{status}' should be valid, got: {errors}")

    def test_reject_missing_assignee(self):
        """Task without assignee must be rejected."""
        from agent_bridge_connect.schema import validate_task

        data = dict(self.valid)
        del data["assignee"]
        errors = validate_task(data)
        self.assertTrue(any("assignee" in e.lower() for e in errors))

    def test_reject_missing_steps(self):
        """Task without steps must be rejected."""
        from agent_bridge_connect.schema import validate_task

        data = dict(self.valid)
        del data["steps"]
        errors = validate_task(data)
        self.assertTrue(any("steps" in e.lower() for e in errors))

    def test_reject_empty_steps(self):
        """Task with empty steps list must be rejected."""
        from agent_bridge_connect.schema import validate_task

        data = dict(self.valid)
        data["steps"] = []
        errors = validate_task(data)
        self.assertTrue(len(errors) > 0)

    def test_accept_session_id(self):
        """Task with session_id should pass validation."""
        from agent_bridge_connect.schema import validate_task

        data = dict(self.valid)
        data["session_id"] = "20260605_143022_a1b2c3"
        errors = validate_task(data)
        self.assertEqual(errors, [])

    def test_accept_intervention_fields(self):
        """Task with intervention fields should pass."""
        from agent_bridge_connect.schema import validate_task

        data = dict(self.valid)
        data["intervention"] = {"paused": True, "pause_reason": "testing", "latest_correction_id": "I-001"}
        errors = validate_task(data)
        self.assertEqual(errors, [])


class StepSchemaTests(unittest.TestCase):
    """Validate steps/N.json schema rules."""

    def setUp(self):
        self.valid = json.loads((FIXTURES / "sample_step.json").read_text())

    def test_load_valid_step(self):
        """Valid step record should load without error."""
        from agent_bridge_connect.schema import validate_step

        errors = validate_step(self.valid)
        self.assertEqual(errors, [], f"Unexpected errors: {errors}")

    def test_reject_missing_task_id(self):
        """Step without task_id must be rejected."""
        from agent_bridge_connect.schema import validate_step

        data = dict(self.valid)
        del data["task_id"]
        errors = validate_step(data)
        self.assertTrue(any("task_id" in e.lower() for e in errors))

    def test_reject_missing_step_id(self):
        """Step without step_id must be rejected."""
        from agent_bridge_connect.schema import validate_step

        data = dict(self.valid)
        del data["step_id"]
        errors = validate_step(data)
        self.assertTrue(any("step_id" in e.lower() for e in errors))

    def test_reject_missing_worker(self):
        """Step without worker identity must be rejected."""
        from agent_bridge_connect.schema import validate_step

        data = dict(self.valid)
        del data["worker"]
        errors = validate_step(data)
        self.assertTrue(any("worker" in e.lower() for e in errors))

    def test_accept_done_step(self):
        """Done step must have finished_at and duration_s."""
        from agent_bridge_connect.schema import validate_step

        data = dict(self.valid)
        data["status"] = "done"
        errors = validate_step(data)
        self.assertEqual(errors, [])

    def test_reject_done_step_without_finished_at(self):
        """Done step without finished_at must be rejected."""
        from agent_bridge_connect.schema import validate_step

        data = dict(self.valid)
        del data["finished_at"]
        errors = validate_step(data)
        self.assertTrue(any("finished_at" in e.lower() for e in errors))


class TaskDataclassTests(unittest.TestCase):
    """Test Task dataclass serialization round-trip."""

    def test_round_trip(self):
        """Task loaded from dict should serialize back identically."""
        from agent_bridge_connect.task import Task

        data = json.loads((FIXTURES / "sample_task.json").read_text())
        task = Task.from_dict(data)
        out = task.to_dict()

        self.assertEqual(data["id"], out["id"])
        self.assertEqual(data["title"], out["title"])
        self.assertEqual(data["status"], out["status"])
        self.assertEqual(data["assignee"], out["assignee"])
        self.assertEqual(len(data["steps"]), len(out["steps"]))

    def test_status_transition_valid(self):
        """Valid status transitions should succeed."""
        from agent_bridge_connect.task import Task

        task = Task.from_dict(json.loads((FIXTURES / "sample_task.json").read_text()))
        self.assertTrue(task.can_transition_to("assigned"))
        self.assertTrue(task.can_transition_to("in_progress"))

    def test_status_transition_invalid(self):
        """Invalid status transitions should be rejected."""
        from agent_bridge_connect.task import Task

        task = Task.from_dict(json.loads((FIXTURES / "sample_task.json").read_text()))
        self.assertFalse(task.can_transition_to("done"))  # can't skip to done from pending
