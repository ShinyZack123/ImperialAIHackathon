"""Notebook 04 and its deploy command. Mocked SDK; no workspace is contacted."""
import base64
import gzip
import hashlib
import json
import re
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from backend.databricks_data import DataSettings
from backend.detector_job import JOB_NAME, deploy, job_settings, run_once
from backend.result_store import detector_version
from build_detector_job_notebook import DESTINATION, PACKED_FILES, REPO, build_notebook


SETTINGS = DataSettings("abc123", "workspace")


def code_of(notebook):
    return "\n".join("".join(c["source"]) for c in notebook["cells"] if c["cell_type"] == "code")


def unpack(notebook):
    namespace = {}
    source = "".join(next(c for c in notebook["cells"] if c.get("id") == "load-detector")["source"])
    exec(source.split("files = json.loads")[0], namespace)  # only the MANIFEST and PAYLOAD assignments
    files = json.loads(gzip.decompress(base64.b64decode(namespace["PAYLOAD"])))
    return namespace["MANIFEST"], files


class NotebookTests(unittest.TestCase):
    def test_committed_notebook_matches_its_builder(self):
        self.assertEqual(json.loads(DESTINATION.read_text()), json.loads(json.dumps(build_notebook())))

    def test_packed_code_is_the_repository_code_and_has_the_same_detector_version(self):
        manifest, files = unpack(json.loads(DESTINATION.read_text()))
        self.assertEqual(set(files), set(PACKED_FILES))
        for path, text in files.items():
            data = text.encode("utf-8")
            self.assertEqual(data, (REPO / path).read_bytes(), path)
            self.assertEqual(hashlib.sha256(data).hexdigest(), manifest[path], path)
        packed_version = hashlib.sha256(b"".join(files[path].encode("utf-8") for path in (
            "flagging/flagger.py", "flagging/config.json", "databricks/backend/flagging_adapter.py"))).hexdigest()
        self.assertEqual(packed_version, detector_version())

    def test_notebook_writes_only_its_audit_tables_and_never_reads_answer_keys(self):
        code = code_of(build_notebook())
        self.assertNotRegex(code.upper(), r"\b(DROP|DELETE|TRUNCATE|OVERWRITE|ALTER)\b")
        self.assertEqual(set(re.findall(r"MERGE INTO (\w+)", code)), {"official_detector_results"})
        self.assertEqual(set(re.findall(r"INSERT INTO (\w+)", code)), {"detector_job_runs"})
        self.assertEqual(set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", code)), {"official_detector_results", "detector_job_runs"})
        self.assertNotIn("ground_truth", code.split("MANIFEST")[0] + code.split("PAYLOAD = (")[1].split(")\n", 1)[1])
        self.assertNotIn("_eval", code.replace("_evaluation_labels", ""))


def client_with_jobs(jobs):
    client = MagicMock()
    client.current_user.me.return_value = SimpleNamespace(user_name="someone@example.com")
    client.jobs.list.return_value = jobs
    client.jobs.create.return_value = SimpleNamespace(job_id=42)
    return client


class DeployTests(unittest.TestCase):
    def test_creates_a_paused_job_that_passes_its_run_id(self):
        client = client_with_jobs([])
        result = deploy(client, SETTINGS)
        self.assertEqual((result["action"], result["job_id"], result["schedule_paused"]), ("created", 42, True))
        upload = client.workspace.import_.call_args.kwargs
        self.assertEqual(upload["path"], "/Workspace/Users/someone@example.com/GPU/detector_job/04_detector_job")
        self.assertTrue(upload["overwrite"])
        config = client.jobs.create.call_args.kwargs
        self.assertEqual(config["name"], JOB_NAME)
        self.assertEqual(config["schedule"].pause_status.value, "PAUSED")
        self.assertEqual(config["max_concurrent_runs"], 1)
        parameters = config["tasks"][0].notebook_task.base_parameters
        self.assertEqual(parameters, {"catalog": "workspace", "schema": "gpu_ops_hackathon", "step_seconds": "15",
                                      "max_cursors": "500", "job_run_id": "{{job.run_id}}"})

    def test_updates_the_existing_job_instead_of_adding_another(self):
        client = client_with_jobs([SimpleNamespace(job_id=7, settings=SimpleNamespace(name=JOB_NAME))])
        result = deploy(client, SETTINGS, paused=False)
        self.assertEqual((result["action"], result["job_id"]), ("updated", 7))
        client.jobs.create.assert_not_called()
        reset = client.jobs.reset.call_args.kwargs
        self.assertEqual(reset["job_id"], 7)
        self.assertEqual(reset["new_settings"].schedule.pause_status.value, "UNPAUSED")

    def test_refuses_when_two_jobs_share_the_name(self):
        twins = [SimpleNamespace(job_id=n, settings=SimpleNamespace(name=JOB_NAME)) for n in (1, 2)]
        with self.assertRaises(ValueError):
            deploy(client_with_jobs(twins), SETTINGS)

    def test_rejects_invalid_spacing_and_limits(self):
        for step, limit in ((7, 500), (0, 500), (15, 0), (15, 5000), (True, 500)):
            with self.assertRaises(ValueError):
                job_settings("/x", SETTINGS, step_seconds=step, max_cursors=limit, paused=True)


class RunTests(unittest.TestCase):
    def run_with(self, states, error=None):
        client = MagicMock()
        client.jobs.run_now.return_value = SimpleNamespace(response=SimpleNamespace(run_id=99))
        runs = [SimpleNamespace(state=SimpleNamespace(life_cycle_state=SimpleNamespace(value=a), result_state=SimpleNamespace(value=b) if b else None),
                                tasks=[SimpleNamespace(run_id=100)]) for a, b in states]
        client.jobs.get_run.side_effect = runs
        client.jobs.get_run_output.return_value = SimpleNamespace(error=error)
        return run_once(client, 42, poll_seconds=0, sleep=lambda _: None, emit=lambda _: None)

    def test_waits_for_success(self):
        self.assertEqual(self.run_with([("PENDING", None), ("RUNNING", None), ("TERMINATED", "SUCCESS")])["status"], "succeeded")

    def test_reports_the_task_error_on_failure(self):
        report = self.run_with([("TERMINATED", "FAILED")], error="RuntimeError: Packed file does not match")
        self.assertEqual(report["status"], "failed")
        self.assertIn("Packed file", report["error"])


if __name__ == "__main__":
    unittest.main()
