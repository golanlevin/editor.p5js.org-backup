import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import backup_p5js as backup


def project(project_id="one", modified="2026-01-01T00:00:00.000Z", asset=False):
    files = [
        {"id": "root", "name": "root", "fileType": "folder", "children": ["html", "source"]},
        {"id": "html", "name": "index.html", "fileType": "file", "content": "<!doctype html>"},
        {"id": "source", "name": "sketch.js", "fileType": "file", "content": "function setup() {}"},
    ]
    if asset:
        files[0]["children"].append("folder")
        files.extend([
            {"id": "folder", "name": "assets", "fileType": "folder", "children": ["image"]},
            {"id": "image", "name": "image.png", "fileType": "file", "url": "https://untrusted.invalid/image.png"},
        ])
    return {"id": project_id, "name": "Repeated title 🎨", "user": {"username": "yourname"},
            "createdAt": "2020-01-01T00:00:00Z", "updatedAt": modified,
            "visibility": "Public", "files": files}


def zip_bytes(files=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        for name, contents in (files or {"index.html": "<!doctype html>", "sketch.js": "function setup() {}"}).items():
            z.writestr(name, contents)
    return output.getvalue()


def page(projects, number=1, total=None, limit=100):
    total = len(projects) if total is None else total
    return {"projects": projects, "metadata": {"page": number, "totalProjects": total,
            "totalPages": max(1, (total + limit - 1) // limit), "limit": limit, "hasPagination": True}}


class FakeResponse:
    def __init__(self, data=None, body=b"", status=200, content_type="application/json", headers=None, interrupt=False):
        self.data = data
        self.body = body
        self.status_code = status
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.interrupt = interrupt

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def json(self):
        return self.data

    def iter_content(self, chunk_size):
        yield self.body[:len(self.body) // 2]
        if self.interrupt:
            raise KeyboardInterrupt()
        yield self.body[len(self.body) // 2:]


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.headers = {}
        self.cookies = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        pass


def client(responses, retries=0):
    return backup.Client(delay=0, retries=retries, session=FakeSession(responses), sleep=lambda s: None)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.p = project()

    def tearDown(self):
        self.temp.cleanup()

    def write_zip(self, contents=None, filename="archive.zip"):
        path = self.root / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(zip_bytes(contents))
        return path

    def row(self):
        return {**backup.public_metadata(self.p, "yourname"), "username": "yourname", "history": []}

    def export_responses(self, p=None, body=None, interrupt=False):
        p = p or self.p
        return [FakeResponse(p), FakeResponse(body=body or zip_bytes(), content_type="application/zip", interrupt=interrupt), FakeResponse(p)]

    def test_pagination_all_pages_and_server_limit(self):
        projects = [project(str(i)) for i in range(5)]
        c = client([FakeResponse(page(projects[:2], 1, 5, 2)),
                    FakeResponse(page(projects[2:4], 2, 5, 2)), FakeResponse(page(projects[4:], 3, 5, 2))])
        evidence = {"pages": []}
        found = backup.discover(c, "yourname", evidence)
        self.assertEqual(len(found), 5)
        self.assertTrue(evidence["complete"])
        self.assertEqual([call[1]["params"]["page"] for call in c.session.calls], [1, 2, 3])

    def test_missing_metadata_uses_empty_page_not_short_page(self):
        c = client([FakeResponse({"projects": [self.p]}), FakeResponse({"projects": [project("two")]}), FakeResponse({"projects": []})])
        self.assertEqual(len(backup.discover(c, "yourname", {"pages": []})), 2)

    def test_legacy_array_supported_until_empty_page(self):
        c = client([FakeResponse([self.p]), FakeResponse([])])
        self.assertEqual(backup.discover(c, "yourname", {"pages": []}), [self.p])

    def test_repeated_page_rejected(self):
        c = client([FakeResponse([self.p]), FakeResponse([self.p])])
        with self.assertRaisesRegex(backup.BackupError, "ignoring pagination"):
            backup.discover(c, "yourname", {"pages": []})

    def test_deduplicates_immutable_id(self):
        c = client([FakeResponse([self.p, copy.deepcopy(self.p)]), FakeResponse([])])
        self.assertEqual(len(backup.discover(c, "yourname", {"pages": []})), 1)

    def test_duplicate_ids_cannot_hide_incomplete_enumeration(self):
        c = client([FakeResponse(page([self.p, self.p], total=2))])
        with self.assertRaisesRegex(backup.BackupError, "unique IDs"):
            backup.discover(c, "yourname", {"pages": []})

    def test_listing_total_change_fails_closed(self):
        c = client([FakeResponse(page([self.p], 1, 2, 1)), FakeResponse(page([project("two")], 2, 3, 1))])
        with self.assertRaisesRegex(backup.BackupError, "count changed"):
            backup.discover(c, "yourname", {"pages": []})

    def test_private_record_stops_discovery_without_probing_id(self):
        self.p["visibility"] = "Private"
        c = client([FakeResponse(page([self.p]))])
        with self.assertRaisesRegex(backup.BackupError, "private record"):
            backup.discover(c, "yourname", {"pages": []})
        self.assertEqual(len(c.session.calls), 1)

    def test_current_visibility_legacy_default_and_unknown(self):
        self.p.pop("visibility")
        self.assertIn("legacy", backup.public_metadata(self.p, "yourname")["visibility"])
        self.p["visibility"] = "Unlisted?"
        with self.assertRaises(backup.BackupError):
            backup.public_metadata(self.p, "yourname")

    def test_nested_inventory_and_asset(self):
        expected = backup.expected_files(project(asset=True))
        self.assertEqual([f["path"] for f in expected], ["assets/image.png", "index.html", "sketch.js"])
        self.assertTrue(expected[0]["remote_asset"])

    def test_inventory_cycles_unreachable_and_missing_children(self):
        for mutate in (lambda p: p["files"][0]["children"].append("root"),
                       lambda p: p["files"][0]["children"].remove("source"),
                       lambda p: p["files"][0]["children"].append("missing")):
            with self.subTest(mutate=mutate):
                p = project()
                mutate(p)
                with self.assertRaises(backup.BackupError):
                    backup.expected_files(p)

    def test_zip_validation_checksum_count_and_exact_bytes(self):
        path = self.write_zip()
        before = path.read_bytes()
        result = backup.validate_zip(path, backup.expected_files(self.p))
        self.assertEqual(result["file_count"], 2)
        self.assertEqual(len(result["sha256"]), 64)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(result["completeness"], "inventory_checked")

    def test_html_and_empty_zip_rejected(self):
        for data in (b"<!doctype html>error", b"PK\x03\x04broken"):
            path = self.root / "bad.zip"
            path.write_bytes(data)
            with self.assertRaises(backup.ArchiveError):
                backup.validate_zip(path)
        path = self.root / "empty.zip"
        with zipfile.ZipFile(path, "w"):
            pass
        with self.assertRaisesRegex(backup.ArchiveError, "no files"):
            backup.validate_zip(path)

    def test_crc_failure_rejected(self):
        path = self.root / "badcrc.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as z:
            z.writestr("sketch.js", "uniquepayload")
        path.write_bytes(path.read_bytes().replace(b"uniquepayload", b"changedbytes!"))
        with self.assertRaises(backup.ArchiveError):
            backup.validate_zip(path)

    def test_missing_empty_or_changed_source_rejected(self):
        cases = [({"sketch.js": "function setup() {}"}, self.p),
                 ({"index.html": "<!doctype html>", "sketch.js": "changed"}, self.p),
                 ({"index.html": "<!doctype html>", "sketch.js": "function setup() {}", "assets/image.png": b""}, project(asset=True)),
                 ({"index.html": "<!doctype html>", "sketch.js": "function setup() {}", "extra.js": b""}, self.p)]
        for files, p in cases:
            with self.subTest(files=files):
                with self.assertRaises(backup.ArchiveError) as error:
                    backup.validate_zip(self.write_zip(files), backup.expected_files(p))
                self.assertEqual(error.exception.status, "incomplete")

    def test_nonstandard_filenames_warn_but_do_not_reject(self):
        result = backup.validate_zip(self.write_zip({"main.js": "code"}))
        self.assertEqual(result["validation_status"], "valid")
        self.assertTrue(result["warnings"])

    def test_valid_zip_with_changed_bytes_detected_by_saved_checksum(self):
        path = self.write_zip()
        old_sha = backup.file_sha256(path)
        self.write_zip({"index.html": "updated"})
        with self.assertRaisesRegex(backup.ArchiveError, "SHA-256"):
            backup.validate_zip(path, known_sha256=old_sha)

    def test_manifest_json_csv_unicode_and_removed_project_preserved(self):
        manifest = backup.load_manifest(self.root, "yourname")
        row = self.row()
        row["title"] = 'same name, "quoted"\n🎨'
        manifest["projects"]["one"] = row
        backup.save_manifest(self.root, manifest)
        loaded = backup.load_manifest(self.root, "yourname")
        self.assertEqual(loaded["projects"]["one"]["title"], row["title"])
        import csv
        with (self.root / "manifest.csv").open(newline="", encoding="utf-8") as f:
            self.assertEqual(list(csv.DictReader(f))[0]["title"], row["title"])

    def test_corrupt_manifest_and_wrong_account_not_overwritten(self):
        path = self.root / "manifest.json"
        path.write_text("bad data")
        with self.assertRaises(backup.BackupError):
            backup.load_manifest(self.root, "yourname")
        self.assertEqual(path.read_text(), "bad data")
        backup.save_manifest(self.root, {"schema_version": 1, "username": "other", "projects": {}})
        with self.assertRaises(backup.BackupError):
            backup.load_manifest(self.root, "yourname")

    def test_resume_skip_changed_timestamp_force_and_missing_timestamp(self):
        row = self.row()
        c = client(self.export_responses())
        self.assertEqual(backup.archive_project(c, self.root, row, self.p), "archived")
        self.assertEqual(len(c.session.calls), 3)
        self.assertTrue(backup.can_skip(self.root, row, self.p))
        self.assertFalse(backup.can_skip(self.root, row, self.p, force=True))
        self.assertFalse(backup.can_skip(self.root, row, project(modified="later")))
        self.assertFalse(backup.can_skip(self.root, row, project(modified=None)))
        self.assertEqual(backup.archive_project(client([]), self.root, row, self.p), "previously_archived")
        self.assertFalse(list((self.root / "sketches" / "partials").glob("*.part")))

    def test_history_preserves_previous_bytes_on_change(self):
        row = self.row()
        backup.archive_project(client(self.export_responses()), self.root, row, self.p)
        original = (self.root / row["zip_file"]).read_bytes()
        p = project(modified="later")
        p["files"][2]["content"] = "new code"
        body = zip_bytes({"index.html": "<!doctype html>", "sketch.js": "new code"})
        backup.archive_project(client(self.export_responses(p, body)), self.root, row, p)
        self.assertEqual(len(row["history"]), 1)
        self.assertEqual((self.root / row["history"][0]["zip_file"]).read_bytes(), original)
        self.assertEqual((self.root / row["zip_file"]).read_bytes(), body)

    def test_failed_replacement_leaves_good_previous_archive(self):
        row = self.row()
        backup.archive_project(client(self.export_responses()), self.root, row, self.p)
        original = (self.root / row["zip_file"]).read_bytes()
        with self.assertRaises(backup.ArchiveError):
            backup.archive_project(client(self.export_responses(body=b"bad zip")), self.root, row, self.p, force=True)
        self.assertEqual((self.root / row["zip_file"]).read_bytes(), original)
        self.assertEqual(len(list((self.root / "sketches" / "partials").glob("*.part"))), 1)

    def test_private_or_changed_during_export_not_installed(self):
        private = project()
        private["visibility"] = "Private"
        c = client([FakeResponse(private)])
        with self.assertRaises(backup.BackupError):
            backup.archive_project(c, self.root, self.row(), self.p)
        self.assertEqual(len(c.session.calls), 1)
        c = client([FakeResponse(self.p), FakeResponse(body=zip_bytes(), content_type="application/zip"), FakeResponse(project(modified="changed"))])
        with self.assertRaisesRegex(backup.ArchiveError, "changed during"):
            backup.archive_project(c, self.root, self.row(), self.p)
        self.assertFalse((self.root / "sketches" / "one.zip").exists())

    def test_interruption_saves_checkpoint_then_resumes_remaining(self):
        p2 = project("two")
        args = backup.parse_args(["--output", str(self.root), "--limit", "2"])
        manifest = backup.load_manifest(self.root, "yourname")
        responses = [FakeResponse(page([self.p, p2]))] + self.export_responses() + self.export_responses(p2, interrupt=True)
        with patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(backup.run_backup(args, client(responses), manifest), 130)
        saved = backup.load_manifest(self.root, "yourname")
        self.assertTrue(saved["last_run"]["interrupted"])
        self.assertTrue((self.root / "sketches" / "one.zip").exists())
        self.assertFalse((self.root / "sketches" / "two.zip").exists())
        parts = list((self.root / "sketches" / "partials").glob("*.part"))
        self.assertEqual(len(parts), 1)
        c = client([FakeResponse(page([self.p, p2]))] + self.export_responses(p2))
        with patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(backup.run_backup(args, c, saved), 0)
        self.assertEqual(saved["last_run"]["archived"], 1)
        self.assertEqual(saved["last_run"]["previously_archived"], 1)
        self.assertEqual(len(c.session.calls), 4)
        self.assertTrue(parts[0].exists())

    def test_failed_download_continues_and_summary_reconciles_with_limit(self):
        projects = [self.p, project("two"), project("three"), project("four")]
        args = backup.parse_args(["--output", str(self.root), "--limit", "3"])
        responses = [FakeResponse(page(projects)), FakeResponse(status=404)]
        responses += self.export_responses(projects[1], body=b"not a ZIP")
        responses += self.export_responses(projects[2])
        # Failed validation never requests the post-export project metadata.
        responses.pop(4)
        manifest = backup.load_manifest(self.root, "yourname")
        with patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(backup.run_backup(args, client(responses), manifest), 1)
        summary = manifest["last_run"]
        self.assertEqual([summary[k] for k in ("failed", "corrupt", "archived", "not_processed")], [1, 1, 1, 1])
        self.assertEqual(sum(summary[k] for k in ("failed", "corrupt", "archived", "previously_archived", "not_processed")), 4)

    def test_dry_run_enumerates_all_and_marks_disappeared_without_deleting(self):
        path = self.write_zip(filename="sketches/gone.zip")
        manifest = backup.load_manifest(self.root, "yourname")
        manifest["projects"]["gone"] = {"project_id": "gone", "zip_file": "sketches/gone.zip"}
        c = client([FakeResponse(page([self.p, project("two")]))])
        args = backup.parse_args(["--output", str(self.root), "--dry-run", "--limit", "1"])
        with patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(backup.run_backup(args, c, manifest), 0)
        self.assertEqual(len(c.session.calls), 1)
        self.assertTrue(path.exists())
        self.assertTrue(manifest["projects"]["gone"]["not_publicly_listed"])
        self.assertEqual(len(json.loads((self.root / "discovery.json").read_text())["projects"]), 2)

    def test_failed_discovery_preserves_prior_state(self):
        (self.root / "discovery.json").write_text('{"previous": true}')
        manifest = backup.load_manifest(self.root, "yourname")
        args = backup.parse_args(["--output", str(self.root)])
        with self.assertRaises(backup.BackupError):
            backup.run_backup(args, client([FakeResponse(status=503)]), manifest)
        self.assertEqual((self.root / "discovery.json").read_text(), '{"previous": true}')
        self.assertEqual(len(list((self.root / "logs").glob("discovery-failed-*.json"))), 1)

    def test_offline_verify_current_historical_orphan_and_missing(self):
        row = self.row()
        backup.archive_project(client(self.export_responses()), self.root, row, self.p)
        backup.archive_project(client(self.export_responses()), self.root, row, self.p, force=True)
        manifest = backup.load_manifest(self.root, "yourname")
        manifest["projects"]["one"] = row
        manifest["projects"]["missing"] = {"project_id": "missing", "zip_file": "sketches/missing.zip"}
        self.write_zip(filename="sketches/orphan.zip")
        with patch("sys.stdout", new=io.StringIO()), patch.object(backup.Client, "__init__", side_effect=AssertionError("No HTTP allowed")):
            self.assertEqual(backup.verify_only(self.root, manifest), 1)
        report = manifest["last_verification"]
        self.assertEqual((report["valid"], report["missing"]), (3, 1))

    def test_redirect_html_status_and_url_boundaries(self):
        for response in (FakeResponse(status=302, headers={"Location": "https://evil.invalid"}),
                         FakeResponse(content_type="text/html"), FakeResponse(status=403)):
            c = client([response])
            with self.assertRaises(backup.BackupError):
                c.json("/editor/yourname/projects")
            self.assertFalse(c.session.calls[0][1]["allow_redirects"])
        c = client([])
        with self.assertRaises(backup.BackupError):
            c.json("//evil.invalid")
        with self.assertRaises(backup.BackupError):
            backup.safe_id("../../secret")
        with self.assertRaises(backup.BackupError):
            backup.local_path(self.root, "../secret")

    def test_429_retry_after_and_exponential_backoff(self):
        session = FakeSession([FakeResponse(status=429, headers={"Retry-After": "7"}),
                               FakeResponse(status=503), FakeResponse(page([self.p]))])
        sleeps = []
        c = backup.Client(delay=0, retries=3, session=session, sleep=sleeps.append)
        self.assertEqual(c.json("/editor/yourname/projects"), page([self.p]))
        self.assertEqual([s for s in sleeps if s], [7, 2])
        self.assertEqual(len(session.calls), 3)

    def test_network_error_retries(self):
        c = client([OSError("network down"), FakeResponse({"ok": True})], retries=1)
        self.assertEqual(c.json("/editor/yourname/projects"), {"ok": True})

    def test_truncated_stream_retried_retaining_failed_bytes(self):
        body = zip_bytes()
        c = client([FakeResponse(body=body[:20], content_type="application/zip", headers={"Content-Length": str(len(body))}),
                    FakeResponse(body=body, content_type="application/zip")], retries=1)
        part, result = c.download("one", self.root, backup.expected_files(self.p))
        self.assertTrue(part.exists())
        self.assertEqual(result["validation_status"], "valid")
        self.assertEqual(len(list((self.root / "sketches" / "partials").glob("*.part"))), 2)

    def test_partial_files_are_not_accepted_as_archives(self):
        row = self.row()
        row.update(last_attempt_status="archived", archived_modified_at=self.p["updatedAt"],
                   expected_files=backup.expected_files(self.p), sha256="unused", zip_file="sketches/one.zip")
        self.write_zip(filename="sketches/one.zip.part")
        self.assertFalse(backup.can_skip(self.root, row, self.p))

    def test_unknown_project_selection_cannot_probe_unlisted_id(self):
        args = backup.parse_args(["--output", str(self.root), "--project-id", "notlisted"])
        c = client([FakeResponse(page([self.p]))])
        with self.assertRaisesRegex(backup.BackupError, "not publicly listed"):
            backup.run_backup(args, c, backup.load_manifest(self.root, "yourname"))
        self.assertEqual(len(c.session.calls), 1)


if __name__ == "__main__":
    unittest.main()
