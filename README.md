# Public p5.js Editor Backup Tool

On October 22, 2026, the [Processing Foundation announced](https://discourse.processing.org/t/the-p5-js-editor-is-moving-to-a-new-backend-help-us-test-it/49748?utm_source=chatgpt.com) that the p5.js Editor will update its backend infrastructure. That's great! Since I have more than 700 sketches hosted there, it also seemed like it might be a good time to back everything up. 

This program backs up an account's publicly listed p5.js Web Editor sketches as complete, unchanged ZIP exports. The utility enumerates every listing page, validates each archive, records JSON/CSV manifests, and supports resuming and incremental backups.

It uses anonymous, sequential GET requests to the official editor API. No password is needed. Private sketches are excluded, and the utility does not modify server data. The default minimum between request starts is **2.0 seconds**.

## Install

Requires Python 3.9 or newer:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows, activate the environment with `.venv\Scripts\Activate.ps1` in PowerShell instead. The only external dependency is `requests`; archive processing and tests use Python's standard library.

## Back up an account

Replace **`yourname`** in these commands with the account's p5.js Web Editor username. The CLI also defaults to the placeholder `yourname`. Use a separate output directory for each account.

First enumerate the public sketches without downloading ZIPs:

```sh
python backup_p5js.py --username yourname --output ./p5js-backup --dry-run
```

Discovery always retrieves every page, including when `--limit` is set. It prints the total public count and saves the response pages and project records in `discovery.json`.

Try a small batch:

```sh
python backup_p5js.py --username yourname --output ./p5js-backup --limit 10
```

This selects the first ten projects in creation order (or all projects if the account has fewer than ten). Alternatively, repeat `--project-id ACTUAL_ID` to select specific projects from `discovery.json`; IDs absent from the public listing are rejected before any individual project request. Sketch names do not need to be unique.

Inspect an archive without extracting or modifying it. Replace `PROJECT_ID` with an ID from your batch:

```sh
python -m zipfile -l p5js-backup/sketches/PROJECT_ID.zip
python backup_p5js.py --username yourname --output ./p5js-backup --verify-only
```

To test resumption, interrupt a batch with Ctrl-C, then repeat the exact command. Completed, valid, unchanged archives are skipped; an interrupted download starts a fresh export and its old `.part` bytes are retained.

After reviewing the small batch, download the full public collection:

```sh
python backup_p5js.py --username yourname --output ./p5js-backup
```

Rerun that command for an incremental backup. It skips valid unchanged archives, retries failed downloads, and archives new or changed sketches. Earlier versions are preserved in `history/`. There is no interactive approval dialog; omitting `--limit` and `--project-id` selects the full public listing.

## Options

| Option | Behavior |
| --- | --- |
| `--username yourname` | Account to enumerate; defaults to the placeholder `yourname`. |
| `--output PATH` | Backup directory; defaults to `./p5js-backup`. |
| `--dry-run` | Saves discovery and manifests without downloading ZIPs. |
| `--limit 10` | Processes at most ten sketches, after complete discovery. |
| `--project-id ID` | Selects a publicly listed project; repeat for an ordered batch. |
| `--delay 2.0` | Minimum seconds between sequential request starts; defaults to 2.0. |
| `--force` | Redownloads selected sketches while preserving existing ZIPs in history. |
| `--verify-only` | Validates local current/historical archives without HTTP requests. |
| `--verbose` | Adds request and retry diagnostics to logs. |

Exit codes: `0` means the selected work succeeded, `1` means a discovery/download/validation/filesystem error, and `130` means interrupted. A successful dry run does not mean any ZIP was archived. Verification cannot establish current public visibility or completeness without saved remote inventory.

## Files and recovery

```text
p5js-backup/
    manifest.json
    manifest.csv
    discovery.json
    .backup.lock
    logs/
        backup.log
        discovery-failed-<timestamp>.json
        verification-<timestamp>.json
    sketches/
        <immutable-project-id>.zip
        partials/
            <id>.<timestamp>.zip.part
    metadata/
        <id>/<timestamp>.json
    history/
        <id>/<timestamp>.zip
        <id>/<timestamp>.json
        discovery/<timestamp>.json
```

`manifest.json` is authoritative; CSV is regenerated from it. JSON retains per-archive validation details, the saved file inventory, warnings, archive paths, previous versions, and the latest run summary. CSV contains the primary metadata and validation columns. Remote metadata and downloaded-version timestamps are separate, so a failed refresh cannot make an old archive appear current.

Discovery must finish consistently before the utility flags any previously known sketch as no longer publicly listed. Such a flag does not distinguish a deletion, rename of the account, or privacy change. No archive is deleted. Failed discovery evidence goes to a separate log file and leaves the last complete discovery and manifest untouched.

A sketch is skipped only when its modification timestamp matches the downloaded version, its ZIP passes validation, its saved checksum matches, and its file inventory matches the listing when supplied. Missing timestamps cause a fresh export. Previous failures are retried. Use `--force` when remote timestamps or external assets may have changed without a project save.

Temporary downloads have unique `.part` names. Failed or interrupted bytes remain there for inspection; the next run starts a fresh request rather than appending to a partial ZIP. Only validated, inventory-checked exports become final ZIPs. Failed replacements leave an existing final archive in place.

Before replacement, the existing archive is copied to a uniquely named history ZIP, flushed, and accompanied by a metadata sidecar. The new ZIP is then atomically installed. Manifests are checkpointed after each project using temporary files and atomic replacement. A process lock prevents concurrent writers and releases automatically after a crash. A power loss can still leave a newly installed ZIP ahead of its manifest; the next run detects the checksum/timestamp inconsistency and downloads again, preserving that ZIP too. Untracked history ZIPs are included in offline integrity checks; their sidecars remain available for manual recovery. No recovery step automatically removes files.

Summary categories are mutually exclusive: newly archived, previously archived, failed, corrupt, and not processed add up to the number discovered. `--limit`, dry runs, and interruptions leave sketches in “not processed.” Missing assets or mismatched source count as failed/incomplete; structurally broken ZIPs count as corrupt. Earlier failed attempts whose retry succeeds are not counted again. Verification has its own local-archive counts, including history.

## Validation and completeness limits

Each ZIP must contain files and pass `zipfile.testzip()`. The program rejects duplicate/unsafe file paths, HTML responses, unexpected statuses/types, missing expected paths, changed inline source content, and empty URL-backed assets. It records ZIP size, SHA-256, file count, and filenames. Nonstandard source filenames generate a warning rather than an automatic failure. Additional nonempty bundled dependencies are allowed; empty additional export files are flagged.

The program gets anonymous project metadata immediately before and after an export. Visibility, ownership, ID, file inventory, source hashes, and modification timestamps are checked. A concurrent change prevents installation. Asset URLs in metadata are retained for comparison but never requested by this utility. All client traffic stays on the official editor origin; redirects are refused. Environment proxy/CA settings work, while implicit `.netrc` authentication and retained cookies are disabled.

Completeness checks are evidence, not a proof that every remote asset is correct. Nonempty but incorrect remote bytes, an asset changed without metadata changes, dynamic resource requests, external styles/fonts/media, and services needed at runtime cannot be established by ZIP integrity. The original export may keep online URLs in HTML even when it bundles a library. The unchanged ZIPs and metadata are usable without the editor website, but a sketch may still need external services or URL adjustments to run offline. This utility does not rewrite the archive to fix those dependencies. Legitimately empty remote assets are conservatively flagged as incomplete for inspection; the export endpoint does not provide a per-asset failure report.

## API research

The utility uses these upstream API routes and behaviors. Consult the editor source when diagnosing deployment changes:

- [Routes](https://github.com/processing/p5.js-web-editor/blob/develop/server/routes/project.routes.ts) and [server mount](https://github.com/processing/p5.js-web-editor/blob/develop/server/server.js): the project routes are mounted at `/editor`. Listing, project metadata, and ZIP exports use GET.
- [Listing controller](https://github.com/processing/p5.js-web-editor/blob/develop/server/controllers/project.controller/getProjectsForUser.js): anonymous listing excludes `Private`; both `page` and `limit` enable pagination. Pages start at one, the server caps the limit at 100, and responses contain `projects` plus pagination `metadata`. Sorting uses a project-ID tie-breaker. The utility requests creation-order pages, checks totals and page lengths, and deduplicates immutable IDs. Legacy list responses continue until empty; repeated pages fail closed rather than assuming enumeration is complete.
- [Model](https://github.com/processing/p5.js-web-editor/blob/develop/server/models/project.js): visibility defaults to `Public`; timestamps are enabled and IDs are immutable filename keys. Listing does not select slug; individual metadata can provide it.
- [Export controller](https://github.com/processing/p5.js-web-editor/blob/develop/server/controllers/project.controller.js): exports the root folder graph, includes inline source and fetched URL assets, and attempts to bundle external script libraries. Asset-fetch exceptions produce empty files and server-side warnings, with no client-readable failure inventory. This motivates path, source-content, and empty-asset checks. ZIP validation cannot prove remote-asset completeness.
- [Editor download action](https://github.com/processing/p5.js-web-editor/blob/develop/client/modules/IDE/actions/project.js): the editor's ZIP action opens the same `/projects/{id}/zip` endpoint.
- [p5mirror documentation](https://p5molab.github.io/p5mirror-p5moLab/), [shell entry point](https://github.com/p5molab/p5mirror-p5moLab/blob/master/bin/mirror.sh), and [delegated generator](https://github.com/molab-itp/p5mirrorLib/blob/master/node/build.js): related archival work. The generator saves listing JSON, filters on modification timestamps, and emits GET commands for the same ZIP endpoint. This utility preserves original ZIP exports without the extraction and file-removal steps used by p5mirror.

## Tests

Run the offline test suite from the repository root:

```sh
python3 -m unittest discover -s tests -v
```

Tests use synthetic projects, in-memory ZIPs, mocked HTTP responses, and temporary directories. They do not contact the editor, require credentials, or need `requests` installed. Coverage includes pagination, private-record rejection, manifests, ZIP/source/asset validation, retries and rate limits, preserved history, interruption/resumption, batch selection, and offline verification.

### Optional live ten-sketch test

`tools/live_10_sketch_test.py` is a developer helper for a chosen account and **exactly ten distinct public IDs**. It never selects the full collection. Its phases record GET responses, summary counts, archive hashes, and partial sizes under the backup directory's `live-test/` folder.

1. Enumerate the account with `--dry-run` and choose ten IDs, preferably covering different assets and nested folders.
2. In a fresh output directory, download only the first selected sketch with the main CLI's `--project-id FIRST_ID --limit 1`.
3. Run the helper's `interrupt` phase with those ten IDs in order. It skips the first archive, starts the second export, and sends SIGINT after the first nonempty response chunk is written.
4. Repeat the same selection with phase `resume`, then phase `skip`. Resume completes the batch; skip requires ten unchanged archives and zero ZIP requests.
5. Run the main CLI with `--verify-only` and inspect the evidence before a full backup.

For example, replace every `PROJECT_ID_XX` below with a real ID from the public listing:

```sh
python tools/live_10_sketch_test.py interrupt --username yourname --output ./p5js-backup \
  --project-id PROJECT_ID_01 --project-id PROJECT_ID_02 \
  --project-id PROJECT_ID_03 --project-id PROJECT_ID_04 \
  --project-id PROJECT_ID_05 --project-id PROJECT_ID_06 \
  --project-id PROJECT_ID_07 --project-id PROJECT_ID_08 \
  --project-id PROJECT_ID_09 --project-id PROJECT_ID_10
```

The helper also accepts `--delay` (default 2.0 seconds). Its interruption phase requires only the first chosen archive to be present; use a separate trial directory. Keep the username, output, IDs, and order identical across phases. Concurrent remote edits can invalidate assertions and should be investigated rather than bypassed. The offline suite tests the helper with simulated responses; it does not start live downloads.

## Repository contents

- `backup_p5js.py`: backup and verification CLI.
- `requirements.txt`: runtime dependency.
- `tests/`: offline regression tests.
- `tools/live_10_sketch_test.py`: configurable live trial helper.
- `codex_instructions.md`: project requirements and maintenance guidance for coding agents.

Generated archives, manifests, discovery responses, metadata, logs, history, local environments, and caches are excluded by `.gitignore`. No account backup data or run-specific reports are included in this repository.
