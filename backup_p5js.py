#!/usr/bin/env python3
"""Read-only, resumable backup of public p5.js Web Editor projects."""

import argparse
import contextlib
import csv
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit
import uuid
import zipfile
import zlib


# Run flow: enumerate the public listing, export selected projects, then checkpoint
# each result. Offline verification reuses archive validation without making HTTP requests.
# Keep every request on this origin; asset URLs are inventory data, not download targets.
ORIGIN = "https://editor.p5js.org"
USER_AGENT = "p5js-public-backup/1.0 (sequential personal archive; GET only)"
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
LOG = logging.getLogger("p5js_backup")
CSV_FIELDS = [
    "project_id", "title", "slug", "original_url", "created_at", "modified_at",
    "visibility", "zip_file", "downloaded_at", "archived_modified_at",
    "size_bytes", "sha256", "file_count", "validation_status", "error",
    "not_publicly_listed", "last_attempt_status", "last_checked_at",
]


class BackupError(Exception):
    pass


class ArchiveError(BackupError):
    def __init__(self, message, status="corrupt"):
        super().__init__(message)
        self.status = status


class TransientError(BackupError):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]


def safe_id(value):
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise BackupError(f"Invalid project ID or username: {value!r}")
    return value


def local_path(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise BackupError(f"Unsafe manifest path: {relative!r}")
    result = root / path
    # Resolve symlinks as well as '..' so a manifest cannot redirect writes outside root.
    if not result.resolve().is_relative_to(root.resolve()):
        raise BackupError(f"Manifest path leaves backup directory: {relative!r}")
    return result


def atomic_text(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Stage on the same filesystem so replacement is atomic. Flush the new contents
    # before publishing them, leaving the old file intact if writing fails.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=path.name + ".", suffix=".tmp", delete=False) as f:
        temp = Path(f.name)
        f.write(contents)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def atomic_json(path, data):
    atomic_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


@contextlib.contextmanager
def output_lock(root):
    """An OS lock releases automatically even if the process is killed."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".backup.lock").open("a+b") as f:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            f.write(b"0")
            f.flush()
            f.seek(0)
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BackupError("Another process is using this backup directory") from exc
        else:
            import fcntl
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise BackupError("Another process is using this backup directory") from exc
        yield


def load_manifest(root, username):
    path = root / "manifest.json"
    if not path.exists():
        return {"schema_version": 1, "username": username, "projects": {}}
    # Refuse damaged or mismatched state instead of treating it as a fresh backup.
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data["schema_version"] != 1 or data["username"] != username
                or not isinstance(data["projects"], dict)):
            raise ValueError("wrong version, username, or project structure")
        for project_id, row in data["projects"].items():
            safe_id(project_id)
            if row["project_id"] != project_id:
                raise ValueError("project ID mismatch")
            for record in [row] + row.get("history", []):
                if record.get("zip_file"):
                    local_path(root, record["zip_file"])
        return data
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise BackupError(f"Cannot load {path}; refusing to overwrite it: {exc}") from exc


def save_manifest(root, manifest):
    # JSON holds the full recovery state; CSV is a readable projection of that state.
    manifest["updated_at"] = utcnow()
    atomic_json(root / "manifest.json", manifest)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for project_id in sorted(manifest["projects"]):
        writer.writerow(manifest["projects"][project_id])
    atomic_text(root / "manifest.csv", output.getvalue())


def retry_after_seconds(value):
    # Retry-After can be a duration in seconds or an absolute HTTP date.
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except (ValueError, TypeError):
        try:
            return max(0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


class Client:
    """Only GET, fixed HTTPS origin, no redirects, credentials, or retained cookies."""

    def __init__(self, delay=2.0, retries=3, session=None, sleep=time.sleep):
        self.delay = delay
        self.retries = retries
        self.sleep = sleep
        self.last_request = None
        if session is None:
            try:
                import requests
            except ImportError as exc:
                raise BackupError("Install dependencies: python -m pip install -r requirements.txt") from exc
            session = requests.Session()
            # A truthy no-op auth callable suppresses requests' implicit .netrc auth.
            # Keep environment proxy and CA support for managed networks.
            session.auth = lambda request: request
            self.network_errors = (OSError, requests.RequestException)
        else:
            self.network_errors = (OSError,)
        self.session = session
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    def close(self):
        self.session.close()

    def request(self, path, consume, params=None, accept="application/json"):
        url = ORIGIN + path
        parsed = urlsplit(url)
        if (not path.startswith("/editor/") or parsed.scheme != "https"
                or parsed.netloc != "editor.p5js.org" or parsed.fragment):
            raise BackupError("Refusing a URL outside the official editor API")
        for attempt in range(self.retries + 1):
            try:
                if self.last_request is not None:
                    # Space request starts, counting time already spent downloading.
                    # A monotonic clock keeps system clock changes out of this calculation.
                    self.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
                self.session.cookies.clear()
                self.last_request = time.monotonic()
                LOG.debug("GET %s params=%s attempt=%s", url, params, attempt + 1)
                with self.session.get(url, params=params, headers={"Accept": accept},
                                      timeout=(15, 180), stream=True, allow_redirects=False) as response:
                    status = response.status_code
                    if status in (408, 425, 429) or 500 <= status <= 599:
                        raise TransientError(f"GET {path}: HTTP {status}",
                                             retry_after_seconds(response.headers.get("Retry-After")))
                    if 300 <= status < 400:
                        raise BackupError(f"GET {path}: refused redirect (HTTP {status})")
                    if status != 200:
                        raise BackupError(f"GET {path}: HTTP {status}")
                    # Consume the body inside the retry scope: streamed reads can fail
                    # after successful headers, and must restart as a fresh GET too.
                    return consume(response)
            except (TransientError,) + self.network_errors as exc:
                if attempt == self.retries:
                    raise BackupError(f"GET {path} failed after {attempt + 1} attempts: {exc}") from exc
                # A server-requested wait takes precedence over our capped backoff.
                wait = max(min(60, 2 ** attempt), getattr(exc, "retry_after", 0))
                LOG.warning("%s; retrying in %.1fs", exc, wait)
                self.sleep(wait)

    def json(self, path, params=None):
        def consume(response):
            if response.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                raise BackupError(f"GET {path}: expected JSON, received {response.headers.get('Content-Type')!r}")
            try:
                return response.json()
            except ValueError as exc:
                raise BackupError(f"GET {path}: invalid JSON") from exc
        return self.request(path, consume, params)

    def download(self, project_id, root, expected):
        def consume(response):
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"application/zip", "application/x-zip-compressed", "application/octet-stream"}:
                raise BackupError(f"Expected ZIP content type, received {content_type!r}")
            part = root / "sketches" / "partials" / f"{safe_id(project_id)}.{stamp()}.zip.part"
            part.parent.mkdir(parents=True, exist_ok=True)
            LOG.info("Streaming %s to %s", project_id, part)
            with part.open("xb") as f:
                for chunk in response.iter_content(chunk_size=128 * 1024):
                    if chunk:
                        f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            length = response.headers.get("Content-Length")
            # requests decodes compressed responses, so their on-wire Content-Length
            # cannot be compared directly with the bytes written to disk.
            if length and not response.headers.get("Content-Encoding"):
                try:
                    expected_length = int(length)
                except ValueError as exc:
                    raise BackupError(f"Invalid Content-Length; bytes retained at {part}") from exc
                if part.stat().st_size != expected_length:
                    raise TransientError(f"Truncated ZIP (retained at {part})")
            try:
                result = validate_zip(part, expected)
            except ArchiveError as exc:
                raise ArchiveError(f"{exc}; downloaded bytes retained at {part}", exc.status) from exc
            return part, result
        return self.request(f"/editor/projects/{safe_id(project_id)}/zip", consume, accept="application/zip")


def public_metadata(project, username):
    if not isinstance(project, dict):
        raise BackupError("Invalid project object")
    if project.get("visibility") == "Private":
        raise BackupError("Private project encountered; refusing to request its files")
    if project.get("visibility") not in (None, "Public"):
        raise BackupError(f"Unrecognized visibility: {project.get('visibility')!r}")
    project_id = safe_id(project.get("id") or project.get("_id"))
    owner = project.get("user")
    if isinstance(owner, dict) and owner.get("username", username).casefold() != username.casefold():
        raise BackupError(f"Project {project_id} belongs to a different account")
    return {
        "project_id": project_id, "title": project.get("name", ""),
        "slug": project.get("slug"), "created_at": project.get("createdAt"),
        "modified_at": project.get("updatedAt"),
        "visibility": project.get("visibility") or "Public (legacy default; anonymous listing)",
        "original_url": f"{ORIGIN}/{quote(username, safe='')}/sketches/{project_id}",
    }


def discover(client, username, evidence):
    """Never infer disappearance from an incomplete or inconsistent enumeration."""
    safe_id(username)
    found = {}
    declared_total = None
    signatures = set()
    for page in range(1, 10001):
        # Creation order is less likely to shift during a run than modification order.
        data = client.json(f"/editor/{username}/projects", {
            "page": page, "limit": 100, "sortField": "createdAt", "sortDir": "asc",
        })
        evidence["pages"].append({"requested_page": page, "response": data})
        if isinstance(data, list):
            projects, metadata = data, None
        elif isinstance(data, dict) and isinstance(data.get("projects"), list):
            projects, metadata = data["projects"], data.get("metadata")
        else:
            raise BackupError("Unexpected listing response; cannot establish complete enumeration")
        # Detect ignored pagination even if the server returns a repeated page reordered.
        signature = tuple(sorted(safe_id(p.get("id") or p.get("_id")) for p in projects))
        if signature and signature in signatures:
            raise BackupError("Repeated listing page; server may be ignoring pagination")
        signatures.add(signature)
        for project in projects:
            # Current anonymous listing excludes Private; do not probe any leaked private ID.
            if project.get("visibility") == "Private":
                raise BackupError("Anonymous listing exposed a private record; stopping before downloads")
            row = public_metadata(project, username)
            old = found.get(row["project_id"])
            if old and old != project:
                raise BackupError("Project changed during pagination; rerun discovery")
            found[row["project_id"]] = project
        LOG.info("Listing page %s: %s records; %s unique public sketches", page, len(projects), len(found))
        if metadata is not None:
            # Inconsistent totals or page lengths could hide projects. Fail before
            # declaring any previously archived sketch absent from the public listing.
            try:
                total, total_pages, limit = (metadata[k] for k in ("totalProjects", "totalPages", "limit"))
                if any(type(v) is not int for v in (total, total_pages, limit)):
                    raise ValueError("non-integer pagination values")
                if (total < 0 or not 1 <= limit <= 100 or metadata["page"] != page
                        or metadata.get("hasPagination") is not True
                        or total_pages != max(1, math.ceil(total / limit))):
                    raise ValueError("inconsistent pagination metadata")
                if declared_total is not None and declared_total != total:
                    raise ValueError("listing count changed during discovery; rerun")
                declared_total = total
                if len(projects) != (limit if page < total_pages else total - (page - 1) * limit):
                    raise ValueError("page length does not match pagination metadata")
            except (KeyError, TypeError, ValueError) as exc:
                raise BackupError(f"Invalid pagination metadata: {exc}") from exc
            if page >= total_pages:
                if len(found) != total:
                    raise BackupError(f"Only {len(found)} unique IDs for reported total {total}; rerun discovery")
                evidence.update(complete=True, total_public=len(found), projects=list(found.values()))
                return list(found.values())
        elif not projects:
            # Without pagination metadata, only an empty page ends enumeration;
            # a short page alone does not establish that the listing is complete.
            evidence.update(complete=True, total_public=len(found), projects=list(found.values()))
            return list(found.values())
    raise BackupError("Pagination exceeded safety limit")


def expected_files(project):
    """Resolve the exact folder graph exported by the editor, without following asset URLs."""
    files = project.get("files")
    if not isinstance(files, list) or not files:
        raise BackupError("Project metadata has no file inventory; completeness cannot be checked")
    indexed = {str(f.get("id") or f.get("_id")): f for f in files}
    if len(indexed) != len(files) or "None" in indexed:
        raise BackupError("Duplicate or absent file IDs")
    roots = [f for f in files if f.get("name") == "root" and f.get("fileType") == "folder"]
    if len(roots) != 1:
        raise BackupError("Project must have exactly one root folder")
    result, visited, paths = [], set(), set()

    def walk(f, parent):
        file_id = str(f.get("id") or f.get("_id"))
        if file_id in visited:
            raise BackupError("File inventory has a cycle or repeated child")
        visited.add(file_id)
        if f is roots[0]:
            # The editor exports root's children directly, without a 'root/' prefix.
            path = ""
        else:
            name = f.get("name")
            if not isinstance(name, str) or name in ("", ".", "..") or any(c in name for c in "/\\\0"):
                raise BackupError(f"Unsafe or ambiguous project filename: {name!r}")
            path = parent + name
            if path in paths:
                raise BackupError(f"Duplicate project path: {path}")
            paths.add(path)
        if f.get("fileType") == "folder":
            children = f.get("children")
            if not isinstance(children, list):
                raise BackupError("Folder has no child list")
            for child_id in children:
                if str(child_id) not in indexed:
                    raise BackupError(f"Missing child in project metadata: {child_id}")
                walk(indexed[str(child_id)], path + "/" if path else "")
        else:
            spec = {"path": path, "remote_asset": bool(f.get("url"))}
            if spec["remote_asset"]:
                spec["url"] = f["url"]  # Compare metadata only; never follow this URL.
            if not spec["remote_asset"]:
                # Inline source has known bytes, allowing an exact content comparison.
                content = f.get("content")
                if not isinstance(content, str):
                    raise BackupError(f"No source content for {path}")
                encoded = content.encode("utf-8")
                spec.update(size_bytes=len(encoded), sha256=hashlib.sha256(encoded).hexdigest())
            result.append(spec)
    walk(roots[0], "")
    if len(visited) != len(files):
        raise BackupError("File inventory contains unreachable files that the export would omit")
    if not result:
        raise BackupError("Project has no files")
    return sorted(result, key=lambda f: f["path"])


def file_sha256(path):
    with path.open("rb") as f:
        return stream_sha256(f)


def stream_sha256(f):
    digest = hashlib.sha256()
    for chunk in iter(lambda: f.read(128 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def validate_zip(path, expected=None, known_sha256=None):
    try:
        with zipfile.ZipFile(path) as archive:
            entries = [f for f in archive.infolist() if not f.is_dir()]
            if not entries:
                raise ArchiveError("ZIP contains no files")
            names = [f.filename for f in entries]
            if len(names) != len(set(names)):
                raise ArchiveError("ZIP contains duplicate file paths")
            for name in names:
                if (PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts
                        or "\\" in name or "\0" in name):
                    raise ArchiveError(f"ZIP contains an unsafe path: {name!r}")
            bad = archive.testzip()
            if bad:
                raise ArchiveError(f"ZIP CRC check failed for {bad}")
            by_name = {f.filename: f for f in entries}
            # CRC checks establish ZIP integrity, not export completeness. Compare the
            # saved inventory too: the editor can emit empty files when asset fetches fail.
            discrepancies = []
            for spec in expected or []:
                name = spec["path"]
                if name not in by_name:
                    discrepancies.append(f"missing file: {name}")
                elif spec["remote_asset"]:
                    if by_name[name].file_size == 0:
                        discrepancies.append(f"empty remote asset (possible export fetch failure): {name}")
                else:
                    with archive.open(name) as f:
                        digest = stream_sha256(f)
                    if digest != spec["sha256"] or by_name[name].file_size != spec["size_bytes"]:
                        discrepancies.append(f"source content differs from project metadata: {name}")
            if expected is not None:
                expected_names = {spec["path"] for spec in expected}
                for name, entry in by_name.items():
                    if name not in expected_names and entry.file_size == 0:
                        discrepancies.append(f"empty additional export file (possible bundled dependency failure): {name}")
            if discrepancies:
                raise ArchiveError("; ".join(discrepancies), "incomplete")
            warnings = []
            if not any(PurePosixPath(n).name in {"sketch.js", "index.html"} for n in names):
                warnings.append("No sketch.js or index.html; filenames may be intentional")
            if expected is None:
                warnings.append("No saved remote file inventory; completeness unverified")
            digest = file_sha256(path)
            if known_sha256 and digest != known_sha256:
                raise ArchiveError("Archive SHA-256 differs from the saved manifest")
            return {"size_bytes": path.stat().st_size, "sha256": digest,
                    "file_count": len(entries), "validation_status": "valid",
                    "warnings": warnings, "archive_files": names,
                    "completeness": "inventory_checked" if expected is not None else "unverified"}
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, EOFError, NotImplementedError, zlib.error) as exc:
        raise ArchiveError(f"Invalid ZIP: {exc}") from exc


def can_skip(root, row, project, force=False):
    # A matching remote timestamp is only a candidate for reuse; the saved checksum
    # and inventory must also validate against the actual local archive below.
    if force or row.get("last_attempt_status") not in {"archived", "previously_archived"}:
        return False
    modified = project.get("updatedAt")
    if not modified or modified != row.get("archived_modified_at") or not row.get("sha256"):
        return False
    if not row.get("expected_files") or not row.get("zip_file"):
        return False
    path = local_path(root, row["zip_file"])
    if not path.is_file():
        return False
    if "files" in project and expected_files(project) != row["expected_files"]:
        return False
    try:
        validate_zip(path, row["expected_files"], row["sha256"])
    except (ArchiveError, OSError) as exc:
        LOG.warning("Existing archive for %s needs replacement: %s", row["project_id"], exc)
        return False
    return True


def install_archive(root, row, part, validation, project, expected):
    """Checkpoint the old location before installing; a crash never loses the only copy."""
    project_id = safe_id(row["project_id"])
    final = local_path(root, f"sketches/{project_id}.zip")
    final.parent.mkdir(parents=True, exist_ok=True)
    history = list(row.get("history", []))
    if final.exists():
        import shutil
        history_path = local_path(root, f"history/{project_id}/{stamp()}.zip")
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with final.open("rb") as src, history_path.open("xb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        # Preserve metadata alongside the bytes even if interrupted before manifest save.
        previous = {k: v for k, v in row.items() if k != "history"}
        preserved_sha256 = file_sha256(history_path)
        previous.update(zip_file=history_path.relative_to(root).as_posix(),
                        preserved_at=utcnow(), preserved_bytes_sha256=preserved_sha256)
        previous.setdefault("sha256", preserved_sha256)
        if row.get("archived_title") is not None:
            previous["title"] = row["archived_title"]
        if row.get("archived_modified_at") is not None:
            previous["modified_at"] = row["archived_modified_at"]
        atomic_json(history_path.with_suffix(".json"), previous)
        history.append(previous)
    snapshot = local_path(root, f"metadata/{project_id}/{stamp()}.json")
    atomic_json(snapshot, project)
    # Only install validated bytes, after preserving any existing archive and metadata.
    os.replace(part, final)
    row.update(validation)
    row.update(public_metadata(project, row["username"]))
    row.update(zip_file=final.relative_to(root).as_posix(), downloaded_at=utcnow(),
               archived_modified_at=project.get("updatedAt"), expected_files=expected,
               archived_title=project.get("name"),
               metadata_file=snapshot.relative_to(root).as_posix(), history=history,
               error=None, last_attempt_status="archived")


def get_project(client, username, project_id):
    project = client.json(f"/editor/{username}/projects/{project_id}")
    metadata = public_metadata(project, username)
    if metadata["project_id"] != project_id:
        raise BackupError("Project detail ID does not match listing")
    return project


def archive_project(client, root, row, listing, force=False):
    project_id, username = row["project_id"], row["username"]
    if can_skip(root, row, listing, force):
        row.update(last_attempt_status="previously_archived", validation_status="valid", error=None)
        return "previously_archived"
    before = get_project(client, username, project_id)
    expected = expected_files(before)
    part, validation = client.download(project_id, root, expected)
    after = get_project(client, username, project_id)
    # Avoid installing an export assembled while the project was being edited.
    if (before.get("updatedAt") != after.get("updatedAt")
            or before.get("name") != after.get("name")
            or before.get("files") != after.get("files")):
        raise ArchiveError(f"Project changed during export; bytes retained at {part}", "incomplete")
    install_archive(root, row, part, validation, before, expected)
    return "archived"


def summary_text(summary):
    return "\n".join([
        f"Public sketches discovered: {summary['discovered']}",
        f"Successfully archived:      {summary['archived']}",
        f"Previously archived:        {summary['previously_archived']}",
        f"Failed:                     {summary['failed']}",
        f"Corrupt archives:           {summary['corrupt']}",
        f"Not processed this run:     {summary['not_processed']}",
    ])


def run_backup(args, client, manifest):
    root = args.output
    evidence = {"username": args.username, "started_at": utcnow(), "complete": False, "pages": []}
    try:
        projects = discover(client, args.username, evidence)
    except BaseException as exc:
        evidence.update(error=str(exc) or type(exc).__name__, finished_at=utcnow())
        # Preserve the last complete discovery on failed enumeration.
        atomic_json(root / "logs" / f"discovery-failed-{stamp()}.json", evidence)
        raise
    evidence["finished_at"] = utcnow()
    if (root / "discovery.json").exists():
        previous = (root / "discovery.json").read_text(encoding="utf-8")
        atomic_text(root / "history" / "discovery" / f"{stamp()}.json", previous)
    atomic_json(root / "discovery.json", evidence)
    print(f"Public sketches discovered: {len(projects)}", flush=True)
    # Apply batch selection only after full discovery, so a limit cannot make the
    # unselected sketches appear to have disappeared from the account.
    selected = projects
    if args.project_id:
        by_id = {p.get("id") or p.get("_id"): p for p in projects}
        missing = set(args.project_id) - by_id.keys()
        if missing:
            raise BackupError(f"Requested IDs are not publicly listed: {', '.join(sorted(missing))}")
        selected = [by_id[k] for k in dict.fromkeys(args.project_id)]
    if args.limit is not None:
        selected = selected[:args.limit]
    summary = dict(discovered=len(projects), archived=0, previously_archived=0,
                   failed=0, corrupt=0, not_processed=len(projects),
                   selected=len(selected), dry_run=args.dry_run, started_at=utcnow())
    rows = manifest["projects"]
    current_ids = {p.get("id") or p.get("_id") for p in projects}
    for project_id, row in rows.items():
        row["not_publicly_listed"] = project_id not in current_ids
    for project in projects:
        metadata = public_metadata(project, args.username)
        row = rows.setdefault(metadata["project_id"], {"history": []})
        # Refresh listing metadata separately from archived_* fields, which describe
        # the downloaded version and must survive a failed replacement attempt.
        row.update(metadata, username=args.username, not_publicly_listed=False,
                   last_publicly_listed_at=evidence["finished_at"])
        row.setdefault("validation_status", "not_downloaded")
        row.setdefault("error", None)
    manifest["last_run"] = summary
    save_manifest(root, manifest)
    try:
        if args.dry_run:
            for project in selected:
                meta = public_metadata(project, args.username)
                print(f"{meta['project_id']}  {meta['title']}")
        else:
            for index, project in enumerate(selected, 1):
                project_id = project.get("id") or project.get("_id")
                row = rows[project_id]
                row["last_checked_at"] = utcnow()
                LOG.info("[%s/%s] %s %s", index, len(selected), project_id, row["title"])
                try:
                    category = archive_project(client, root, row, project, args.force)
                except ArchiveError as exc:
                    category = "corrupt" if exc.status == "corrupt" else "failed"
                    row.update(validation_status=exc.status, error=str(exc), last_attempt_status=category)
                    LOG.error("%s: %s", project_id, exc)
                except (BackupError, OSError) as exc:
                    category = "failed"
                    row.update(validation_status="failed", error=str(exc), last_attempt_status=category)
                    LOG.error("%s: %s", project_id, exc)
                summary[category] += 1
                summary["not_processed"] -= 1
                # Persist each completed outcome so Ctrl-C or a crash loses at most
                # the current attempt, rather than the progress of the whole batch.
                save_manifest(root, manifest)
    except KeyboardInterrupt:
        summary["interrupted"] = True
        LOG.warning("Interrupted; completed archives and partial downloads are preserved")
        return 130
    finally:
        summary["finished_at"] = utcnow()
        save_manifest(root, manifest)
        print(summary_text(summary), flush=True)
    return 1 if summary["failed"] or summary["corrupt"] else 0


def verify_only(root, manifest):
    records = {}
    for row in manifest["projects"].values():
        for record in [row] + row.get("history", []):
            if record.get("zip_file"):
                records[record["zip_file"]] = record
    paths = {p.relative_to(root).as_posix() for folder in ("sketches", "history")
             for p in (root / folder).rglob("*.zip")}
    # Include both untracked ZIPs on disk and manifest paths whose files are missing.
    # Staged .part files remain inspection evidence and are not accepted archives.
    paths.update(records)
    report = {"checked_at": utcnow(), "valid": 0, "corrupt": 0, "incomplete": 0, "missing": 0, "archives": []}
    for relative in sorted(paths):
        record = records.get(relative, {})
        result = {"zip_file": relative}
        try:
            path = local_path(root, relative)
            if not path.is_file():
                raise FileNotFoundError(relative)
            result.update(validate_zip(path, record.get("expected_files"), record.get("sha256")))
            report["valid"] += 1
        except ArchiveError as exc:
            result.update(validation_status=exc.status, error=str(exc))
            report[exc.status] += 1
        except OSError as exc:
            result.update(validation_status="missing", error=str(exc))
            report["missing"] += 1
        report["archives"].append(result)
        if record:
            record.update(verified_at=report["checked_at"], validation_status=result["validation_status"],
                          error=result.get("error"))
    manifest["last_verification"] = report
    save_manifest(root, manifest)
    atomic_json(root / "logs" / f"verification-{stamp()}.json", report)
    print(f"Local archives checked: {len(paths)}\nValid: {report['valid']}\n"
          f"Corrupt: {report['corrupt']}\nIncomplete: {report['incomplete']}\nMissing: {report['missing']}")
    return 1 if report["corrupt"] or report["incomplete"] or report["missing"] else 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="yourname")
    parser.add_argument("--output", type=Path, default=Path("./p5js-backup"))
    parser.add_argument("--dry-run", action="store_true", help="Enumerate and save metadata without ZIP downloads")
    parser.add_argument("--limit", type=int, help="Maximum projects processed; discovery is always complete")
    parser.add_argument("--delay", type=float, default=2.0, help="Minimum seconds between GET requests (default: 2.0)")
    parser.add_argument("--force", action="store_true", help="Redownload selected projects, preserving previous bytes")
    parser.add_argument("--verify-only", action="store_true", help="Offline validation of current and historical ZIPs")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--project-id", action="append", help="Select a publicly listed ID; repeat for an ordered test batch")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if not math.isfinite(args.delay) or args.delay < 0:
        parser.error("--delay must be a finite, nonnegative number")
    if args.verify_only and (args.dry_run or args.force or args.limit is not None or args.project_id):
        parser.error("--verify-only cannot be combined with --dry-run, --force, --limit, or --project-id")
    try:
        safe_id(args.username)
        for project_id in args.project_id or []:
            safe_id(project_id)
    except BackupError as exc:
        parser.error(str(exc))
    args.output = args.output.resolve()
    return args


def main(argv=None):
    args = parse_args(argv)
    (args.output / "logs").mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(args.output / "logs" / "backup.log", encoding="utf-8"), logging.StreamHandler()]
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", handlers=handlers, force=True)
    try:
        with output_lock(args.output):
            manifest = load_manifest(args.output, args.username)
            if args.verify_only:
                # Branch before constructing Client: offline checks need no requests
                # dependency, credentials, or network access.
                return verify_only(args.output, manifest)
            client = Client(delay=args.delay)
            try:
                return run_backup(args, client, manifest)
            finally:
                client.close()
    except KeyboardInterrupt:
        LOG.warning("Interrupted before download processing; no local archives removed")
        return 130
    except (BackupError, OSError) as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
