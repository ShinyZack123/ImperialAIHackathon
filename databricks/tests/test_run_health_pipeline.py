"""Serverless runner contract checks with SDK models; no remote operations."""

import base64
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from databricks.sdk.service.iam import User
from databricks.sdk.service.jobs import Run, SubmitRunResponse

from backend.databricks_data import DataSettings
from backend.run_health_pipeline import execute_pipeline


RUN_ID = "c573ec80-c960-451a-92b3-a177f8ef4005"
CURSOR = "2026-11-14T09:07:30Z"


class Clock:
    now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class PipelineRunnerTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.current_user.me.return_value = User(user_name="member@example.org")
        self.client.jobs.submit.return_value = SimpleNamespace(response=SubmitRunResponse(run_id=123))
        self.client.jobs.get_run.return_value = Run.from_dict({
            "run_id": 123, "state": {"life_cycle_state": "TERMINATED", "result_state": "SUCCESS"},
        })
        self.clock = Clock()

    def execute(self, **kwargs):
        return execute_pipeline(DataSettings("warehouse", "workspace"), RUN_ID, CURSOR, client=self.client, monotonic=self.clock.monotonic, sleep=self.clock.sleep, **kwargs)

    def test_upload_never_overwrites_and_exactly_one_serverless_task_is_submitted(self):
        report = self.execute()
        self.assertEqual(report["status"], "succeeded")
        self.assertEqual(report["job_run_id"], 123)
        upload = self.client.workspace.import_.call_args.kwargs
        self.assertFalse(upload["overwrite"])
        self.assertIn("/Workspace/Users/member@example.org/GPU/02_gpu_health_pipeline_", upload["path"])
        self.assertIn(b'"nbformat": 4', base64.b64decode(upload["content"]))
        self.client.jobs.submit.assert_called_once()
        submission = self.client.jobs.submit.call_args.kwargs
        self.assertEqual(submission["timeout_seconds"], 300)
        self.assertEqual(len(submission["tasks"]), 1)
        task = submission["tasks"][0].as_dict()
        self.assertNotIn("new_cluster", task)
        self.assertNotIn("existing_cluster_id", task)
        self.assertEqual(task["notebook_task"]["base_parameters"], {"catalog": "workspace", "schema": "gpu_ops_hackathon", "run_id": RUN_ID, "as_of_ts": CURSOR})
        self.assertEqual(task["max_retries"], 0)
        self.assertTrue(task["disable_auto_optimization"])
        self.assertEqual(submission["environments"][0].as_dict()["spec"], {"environment_version": "4"})

    def test_v2_state_is_understood(self):
        self.client.jobs.get_run.return_value = Run.from_dict({"run_id": 123, "status": {"state": "TERMINATED", "termination_details": {"code": "SUCCESS", "type": "SUCCESS"}}})
        self.assertEqual(self.execute()["status"], "succeeded")

    def test_production_constructor_uses_config_for_timeout_options(self):
        # Autospec enforces the installed WorkspaceClient signature, which does
        # not accept HTTP/retry timeout keyword arguments directly. Mock Config
        # separately so constructing it never reads credentials or authenticates.
        with patch("databricks.sdk.config.Config", autospec=True) as config, patch("databricks.sdk.WorkspaceClient", autospec=True, return_value=self.client) as constructor:
            report = execute_pipeline(DataSettings("warehouse", "workspace", host="https://example.cloud.databricks.com"), RUN_ID, CURSOR, monotonic=self.clock.monotonic, sleep=self.clock.sleep)
        self.assertEqual(report["status"], "succeeded")
        config.assert_called_once_with(profile="gpu-ops", host="https://example.cloud.databricks.com", http_timeout_seconds=30, retry_timeout_seconds=30)
        constructor.assert_called_once_with(config=config.return_value)

    def test_failed_run_is_never_reported_successful(self):
        self.client.jobs.get_run.return_value = Run.from_dict({"run_id": 123, "state": {"life_cycle_state": "TERMINATED", "result_state": "FAILED", "state_message": "TOKEN secret"}})
        report = self.execute()
        self.assertEqual(report["status"], "finished_without_success")
        self.assertEqual(report["result_state"], "FAILED")
        self.assertNotIn("secret", str(report))

    def test_timeout_only_requests_cancel_and_preserves_last_state(self):
        self.client.jobs.get_run.return_value = Run.from_dict({"run_id": 123, "state": {"life_cycle_state": "RUNNING"}})
        report = self.execute(max_poll_seconds=10)
        self.assertEqual(report["status"], "polling_timed_out")
        self.assertEqual(report["lifecycle_state"], "RUNNING")
        self.assertTrue(report["cancellation_request_accepted"])
        self.assertEqual(self.clock.now, 10)
        self.client.jobs.cancel_run.assert_called_once_with(run_id=123)

    def test_upload_failure_does_not_submit_or_retry_with_overwrite(self):
        self.client.workspace.import_.side_effect = RuntimeError("Authorization secret")
        report = self.execute()
        self.assertEqual(report["status"], "setup_or_submission_failed")
        self.assertEqual(report["failed_stage"], "upload_notebook")
        self.assertNotIn("secret", str(report))
        self.client.workspace.import_.assert_called_once()
        self.client.jobs.submit.assert_not_called()

    def test_monitor_error_preserves_run_id_and_does_not_resubmit(self):
        self.client.jobs.get_run.side_effect = RuntimeError("Authorization secret")
        report = self.execute()
        self.assertEqual(report["status"], "monitoring_failed")
        self.assertEqual(report["job_run_id"], 123)
        self.assertNotIn("secret", str(report))
        self.client.jobs.submit.assert_called_once()

    def test_bad_inputs_fail_before_any_remote_call(self):
        for run, cursor in (("invalid", CURSOR), (RUN_ID, "2026-11-14T09:07:30")):
            with self.subTest(run=run, cursor=cursor), self.assertRaises(ValueError):
                execute_pipeline(DataSettings("warehouse", "workspace"), run, cursor, client=self.client)
        self.assertFalse(self.client.mock_calls)


if __name__ == "__main__":
    unittest.main()
