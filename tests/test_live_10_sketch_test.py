"""Exercise the configurable live runner with synthetic HTTP responses only."""

import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import backup_p5js as backup
from test_backup_p5js import client, FakeResponse, page, project, zip_bytes


spec = importlib.util.spec_from_file_location(
    "live_trial", Path(__file__).resolve().parents[1] / "tools" / "live_10_sketch_test.py")
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


def close_unused_handlers(**kwargs):
    # The runner creates handlers for CLI use; tests retain their own logging.
    for handler in kwargs["handlers"]:
        handler.close()


class LiveTrialTests(unittest.TestCase):
    def test_invalid_batch_rejected_before_client_or_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "not-created"
            batches = [[f"id-{i}" for i in range(9)], ["duplicate"] * 10,
                       [f"id-{i}" for i in range(11)]]
            for ids in batches:
                with self.subTest(ids=ids):
                    argv = ["live_trial", "resume", "--output", str(output)]
                    for project_id in ids:
                        argv.extend(("--project-id", project_id))
                    with patch("sys.argv", argv), patch("sys.stderr", new=io.StringIO()), \
                            patch.object(backup, "Client") as factory:
                        with self.assertRaises(SystemExit) as caught:
                            live.main()
                        self.assertEqual(caught.exception.code, 2)
                        factory.assert_not_called()
                    self.assertFalse(output.exists())

    def test_configurable_account_interrupt_resume_skip_and_batch_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            username = "sampleaccount"
            projects = [project(f"project-{i}") for i in range(11)]
            for p in projects:
                p["user"]["username"] = username
            selected = projects[:10]
            manifest = backup.load_manifest(root, username)
            first = selected[0]
            row = {**backup.public_metadata(first, username), "username": username, "history": []}
            backup.archive_project(client([
                FakeResponse(first), FakeResponse(body=zip_bytes(), content_type="application/zip"),
                FakeResponse(first)]), root, row, first)
            manifest["projects"][first["id"]] = row
            backup.save_manifest(root, manifest)
            first_hash = backup.file_sha256(root / row["zip_file"])

            for phase in ("interrupt", "resume", "skip"):
                responses = [FakeResponse(page(projects))]
                if phase == "interrupt":
                    responses.extend((FakeResponse(selected[1]),
                                      FakeResponse(body=zip_bytes(), content_type="application/zip")))
                elif phase == "resume":
                    for p in selected[1:]:
                        responses.extend((FakeResponse(p),
                                          FakeResponse(body=zip_bytes(), content_type="application/zip"),
                                          FakeResponse(p)))
                c = client(responses)
                argv = ["live_trial", phase, "--username", username,
                        "--output", str(root), "--delay", "0"]
                for p in selected:
                    argv.extend(("--project-id", p["id"]))
                with patch("sys.argv", argv), patch("sys.stdout", new=io.StringIO()), \
                        patch.object(backup, "Client", return_value=c), \
                        patch.object(live.logging, "basicConfig", side_effect=close_unused_handlers), \
                        patch.object(live.os, "kill", side_effect=KeyboardInterrupt) as kill:
                    self.assertEqual(live.main(), 0)
                    self.assertEqual(kill.call_count, int(phase == "interrupt"))
                evidence_file, = (root / "live-test").glob(f"{phase}-*.json")
                evidence = json.loads(evidence_file.read_text())
                self.assertEqual(evidence["username"], username)
                self.assertEqual(evidence["project_ids"], [p["id"] for p in selected])
                self.assertEqual(evidence["summary"]["discovered"], 11)
                self.assertEqual(evidence["summary"]["selected"], 10)
                exports = [r for r in evidence["requests"] if r["url"].endswith("/zip")]
                self.assertEqual(len(exports), {"interrupt": 1, "resume": 9, "skip": 0}[phase])
                self.assertEqual(backup.file_sha256(root / row["zip_file"]), first_hash)

            self.assertEqual(len(list((root / "sketches").glob("*.zip"))), 10)
            self.assertFalse((root / "sketches" / "project-10.zip").exists())
            self.assertTrue(list((root / "sketches" / "partials").glob("*.part")))


if __name__ == "__main__":
    unittest.main()
