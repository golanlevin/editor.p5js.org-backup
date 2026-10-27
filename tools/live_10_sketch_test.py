#!/usr/bin/env python3
"""Live trial of ten caller-selected public projects; never runs the full collection.

Run with the project's virtualenv. The interruption phase sends a real SIGINT
after writing the first chunk of the second selected export. Other HTTP traffic and
archive processing use the unchanged production client. Evidence includes each
GET, phase summaries, and archive hashes. No archives are extracted or deleted.
"""

import argparse
import logging
import os
from pathlib import Path
import signal
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import backup_p5js as backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("interrupt", "resume", "skip"))
    parser.add_argument("--username", default="yourname", help="Account username (default: yourname)")
    parser.add_argument("--output", type=Path, default=Path("p5js-backup"))
    parser.add_argument("--project-id", action="append", required=True,
                        help="Public project ID; repeat exactly ten times in the same order for each phase")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="Minimum seconds between request starts (default: 2.0)")
    opts = parser.parse_args()
    if len(opts.project_id) != 10 or len(set(opts.project_id)) != 10:
        parser.error("Provide exactly ten distinct --project-id values")
    project_ids = opts.project_id
    root = opts.output.resolve()
    arguments = ["--username", opts.username, "--output", str(root), "--limit", "10",
                 "--delay", str(opts.delay)]
    for project_id in project_ids:
        arguments.extend(("--project-id", project_id))
    args = backup.parse_args(arguments)
    evidence_dir = root / "live-test"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(root / "logs" / "backup.log")],
    )
    evidence = dict(phase=opts.phase, username=args.username, delay_seconds=args.delay,
                    started_at=backup.utcnow(), project_ids=project_ids,
                    requests=[], interruption_sent=False)
    client = backup.Client(delay=args.delay)
    original_get = client.session.get

    def tracked_get(url, **kwargs):
        record = dict(url=url, params=kwargs.get("params"), started_at=backup.utcnow())
        evidence["requests"].append(record)
        response = original_get(url, **kwargs)
        record.update(status=response.status_code,
                      content_type=response.headers.get("Content-Type"))
        if (opts.phase == "interrupt" and response.status_code == 200
                and url.endswith(f"/projects/{project_ids[1]}/zip")):
            original_iter = response.iter_content

            def interrupted_stream(chunk_size):
                for chunk in original_iter(chunk_size=min(chunk_size, 64 * 1024)):
                    yield chunk  # The production downloader writes these actual bytes.
                    if chunk and not evidence["interruption_sent"]:
                        evidence["interruption_sent"] = True
                        os.kill(os.getpid(), signal.SIGINT)

            response.iter_content = interrupted_stream
        return response

    client.session.get = tracked_get
    with backup.output_lock(root):
        manifest = backup.load_manifest(root, args.username)
        before = {p.name: backup.file_sha256(p) for p in (root / "sketches").glob("*.zip")}
        try:
            code = backup.run_backup(args, client, manifest)
        finally:
            client.close()
            evidence.update(finished_at=backup.utcnow(), summary=manifest.get("last_run"),
                            hashes_before=before,
                            hashes_after={p.name: backup.file_sha256(p)
                                          for p in (root / "sketches").glob("*.zip")},
                            partials=[dict(path=p.relative_to(root).as_posix(), size_bytes=p.stat().st_size)
                                      for p in (root / "sketches" / "partials").glob("*.part")])
            backup.atomic_json(evidence_dir / f"{opts.phase}-{backup.stamp()}.json", evidence)
    exports = [r for r in evidence["requests"] if r["url"].endswith("/zip")]
    assert all(r["url"].split("/")[-2] in project_ids for r in exports)
    if opts.phase == "interrupt":
        assert code == 130 and evidence["interruption_sent"], "Live SIGINT was not handled"
        assert evidence["summary"]["previously_archived"] == 1
        assert before == evidence["hashes_after"], "Interruption changed a completed archive"
        assert any(p["size_bytes"] > 0 for p in evidence["partials"])
    elif opts.phase == "skip":
        assert code == 0 and evidence["summary"]["previously_archived"] == 10
        assert not exports, "Unchanged batch unexpectedly requested an export"
        assert before == evidence["hashes_after"], "Skip run changed archive bytes"
    else:
        assert code == 0, "Some live exports failed; review retained evidence"
        selected_names = {f"{project_id}.zip" for project_id in project_ids}
        assert selected_names <= evidence["hashes_after"].keys()
        assert evidence["hashes_after"].keys() - before.keys() <= selected_names
        assert all(evidence["hashes_after"].get(k) == v for k, v in before.items())
    print(f"Live {opts.phase} assertions passed; {len(exports)} ZIP requests.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
