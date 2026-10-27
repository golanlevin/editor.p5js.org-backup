# Project: Bulk Backup of Public p5.js Web Editor Sketches

*Instructions provided to Codex to build the backup program.*

---

## Objective

Build a robust, resumable Python 3 command-line utility to download and archive **all publicly accessible sketches** belonging to an account selected with `--username` on the official p5.js Web Editor.

Account URL (replace `yourname` with the desired username):

https://editor.p5js.org/yourname/sketches

Example sketch:

https://editor.p5js.org/yourname/sketches/PROJECT_ID

Collection sizes vary, and accounts may include private sketches. **Do not attempt to access or download private sketches.** Do not modify, delete, rename, or otherwise alter anything on the server.

Provide a complete local backup, preserving the full ZIP archive for each sketch, including all source files and project assets.

---

## 1. Research the Existing API

Before implementing anything, examine the p5.js Web Editor source code:

https://github.com/processing/p5.js-web-editor

Relevant known endpoints include:

- `GET /editor/{username}/projects` — enumerate projects.
- `GET /editor/projects/{projectId}/zip` — download a complete ZIP archive.

These endpoints have been documented previously, but verify their current implementation, pagination parameters, response structure, visibility filtering, and behavior.

The ZIP endpoint is believed to be the same one used by the editor's "File → Download" command.

Also inspect this existing archival tool for useful ideas:

https://p5molab.github.io/p5mirror-p5moLab/

**Prefer direct HTTP requests over browser automation.** Use Python `requests` or `httpx`.

Do not use Selenium or Playwright unless you establish that the necessary operations cannot be performed through ordinary HTTP requests.

---

## 2. Enumerate All Public Sketches

Retrieve the complete list of public sketches owned by the requested username. The CLI default is the placeholder `yourname`; examples must tell readers to substitute their account name.

Requirements:

- Handle API pagination correctly.
- Continue until all pages have been retrieved.
- Do not assume that a single API response contains every sketch.
- Respect the server's actual public/private visibility model.
- Deduplicate by immutable project ID.
- Preserve project metadata wherever available: ID, name, slug, creation date, modification date, visibility, and original URL.
- Do not assume that sketch names are unique.
- Save the complete enumeration results locally as JSON.

If the API cannot reliably distinguish public from private sketches without authentication, investigate the publicly accessible account listing and explain the limitation before proceeding.

The script must report the total number of public sketches discovered before downloading.

---

## 3. Download Complete ZIP Archives

For every public sketch, download the original ZIP export.

Use a stable directory structure:

```
p5js-backup/
    manifest.json
    manifest.csv
    discovery.json
    logs/
        backup.log
    sketches/
        PROJECT_ID.zip
        ANOTHER_PROJECT_ID.zip
        ...
```

Use the immutable project ID as the ZIP filename. Store human-readable sketch names in the manifest rather than filenames, avoiding collisions and filesystem encoding problems.

Requirements:

- Preserve the ZIP files exactly as downloaded.
- Do not extract or modify them.
- Stream downloads to disk.
- Write to a temporary `.part` file first.
- Validate the ZIP before renaming it to its final filename.
- Check HTTP status codes and content types.
- Reject HTML error pages masquerading as ZIP downloads.
- Retry transient failures with exponential backoff.
- Handle HTTP 429 rate-limit responses respectfully.
- Avoid aggressive parallel downloading. Begin with sequential downloads and a configurable delay of 2.0 seconds by default between requests.
- Use a descriptive User-Agent.
- Set reasonable connection and read timeouts.
- Never issue DELETE, PATCH, POST, or PUT requests.

If a particular sketch fails to export, record the failure and continue with the remaining sketches.

---

## 4. Validate Every Archive

A successful HTTP response does not necessarily mean that a usable backup was obtained.

For each downloaded ZIP:

- Verify that Python's `zipfile` library can open it.
- Check ZIP integrity using `testzip()`.
- Confirm that the archive contains files.
- Check for expected project files, such as `sketch.js` or `index.html`, without assuming that every project uses identical filenames.
- Record the archive's byte size and SHA-256 checksum.
- Record the number of contained files.

Do not silently accept incomplete or corrupt ZIP files.

Note that ZIP integrity alone cannot establish that every remote asset was included. Investigate whether the export endpoint reports missing assets, and record any detectable discrepancies.

---

## 5. Generate a Manifest

Produce both `manifest.json` and `manifest.csv`.

Include:

- Project ID
- Sketch title
- Original sketch URL
- Created and modified timestamps, when available
- ZIP filename
- Download timestamp
- ZIP file size
- SHA-256 checksum
- Number of archived files
- Validation status
- Error message, if any

At the end of each run, print a summary:

```
Public sketches discovered: 987
Successfully archived:      982
Previously archived:          0
Failed:                       5
Corrupt archives:             0
```

These numbers are illustrative, not expected results.

Ensure the counts reconcile correctly, with mutually exclusive categories.

---

## 6. Support Resuming and Incremental Backups

The script should be safe to run repeatedly.

On subsequent runs:

- Enumerate the current public sketches again.
- Skip already archived sketches if their existing ZIP files pass validation and have not changed.
- Redownload sketches whose remote modification timestamps indicate changes.
- Retry previously failed downloads.
- Download newly created sketches.
- Never automatically delete local archives when sketches disappear from the server or become private.
- Flag previously archived sketches that are no longer publicly listed.

If remote modification timestamps are unreliable or unavailable, provide a `--force` option to redownload everything.

Preserve an archival history when an existing sketch changes, rather than silently overwriting its only previous backup.

---

## 7. Command-Line Interface

Provide a simple CLI:

```
python backup_p5js.py --username yourname --output ./p5js-backup
```

Additional options:

```
--dry-run
--limit 10
--delay 2.0
--force
--verify-only
--verbose
```

`--dry-run` should enumerate and report sketches without downloading ZIPs.

`--limit 10` should allow a small initial test.

`--verify-only` should validate existing local archives without downloading anything.

---

## 8. Testing Procedure

Begin live testing with a small batch before attempting a full collection.

First:

1. Confirm the listing endpoint works.
2. Verify pagination and determine the number of public sketches.
3. Choose an example ID from the anonymous public listing and test its ZIP export.
4. Inspect the downloaded ZIP and confirm that its source files and assets are present.
5. Download a test batch of 10 sketches, preferably including projects with different types of assets.
6. Validate all 10 archives.
7. Test interruption and resumption.
8. Report the findings and any unresolved issues.

**Stop after the 10-sketch test and obtain the account owner's approval before downloading the full collection.**

---

## 9. Deliverables

Create:

- `backup_p5js.py` — the archival program.
- `requirements.txt` — minimal Python dependencies.
- `README.md` — installation and usage instructions.
- A small test suite for pagination, manifest handling, ZIP validation, and resumption.
- Local backup directories and manifests produced during testing, excluded from version control.

Keep the implementation simple, readable, and maintainable.

Avoid unnecessary dependencies, browser automation, cloud services, and external databases.

The resulting archive should be usable independently of the p5.js Editor website.

---

## 10. Security and Reliability

This is a read-only backup operation.

- NEVER send modifying requests to the p5.js Editor!
- Never require an account password for public sketches.
- Do not circumvent authentication or access controls.
- Do not attempt to access private projects.
- Do not embed credentials in source code.
- Do not delete existing backup files.
- Do not follow untrusted URLs to arbitrary hosts when downloading project archives.
- Keep the original downloaded ZIP bytes unchanged.

If any important API behavior is uncertain, inspect the editor's source code or test it with a small number of requests rather than guessing.

The principal objective is archival completeness and reliability, not download speed.

---

## Publishing and maintenance

- Keep examples, tests, and tools account-independent; use `yourname` as the account placeholder and synthetic IDs in tests.
- Keep the default request delay at 2.0 seconds in both the CLI and HTTP client.
- Publish source code, requirements, documentation, offline tests, and the configurable live-test helper.
- Exclude personal archives, manifests, discovery responses, run reports, logs, metadata, history, environments, caches, and downloaded upstream source snapshots.
- Do not start live account downloads as part of offline tests or import-time code.
- The live-test helper must accept ten distinct public IDs from the caller, never hardcode someone's account or projects, and remain limited to that selected batch.
- Run `python3 -m unittest discover -s tests -v` from the repository root when validating behavior changes.
