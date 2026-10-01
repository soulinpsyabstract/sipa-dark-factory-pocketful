#!/usr/bin/env python3
"""Verify a Pocketful stage-1 docker image was really built from a commit.

Two checks, both required:

  1. IMAGE_AGE  - the image's Created timestamp is not older than the
                  commit's committer time (a 4h-old image carrying a
                  fresh-SHA tag is the exact trap this exists for).
  2. BLOCK_HASH - the in-image blobs for the audited files match the
                  blobs committed at that SHA. Computed as git blob
                  SHA-1s ("blob <len>\\0<content>") on both sides, so
                  apples-to-apples.

Exit code 0 only if every check PASSes. Pure stdlib: shells out to git
and docker. Run with the repo checkout on PATH (needs the git object
db and a working docker CLI).

Workflow for a commit-SHA pass (use it as a mandatory gate):
  1. git worktree add --detach <temp>/pf-<sha> <sha>   # immutable tree
  2. docker build --no-cache -t verify-<sha> <temp>/pf-<sha>/stage-1
  3. python verify/container_check.py --stage-dir <temp>/pf-<sha>/stage-1 \
        --image verify-<sha>
  4. python verify/image_source_check.py --sha <sha> --image verify-<sha>
  5. only if 4 is all-PASS, run verify/verify.py against the container.

Step 4 exists because step 2/3 alone can silently build a dirty tree
(the default --stage-dir of container_check.py is the *working* tree, not
a SHA snapshot); IMAGE_AGE catches a reused/stale tag and BLOB_HASH
catches any content drift.

Usage:
  python image_source_check.py --sha <commit> --image <name[:tag]>
                               [--file REPO_PATH=IMAGE_PATH] ...

Example:
  python image_source_check.py --sha 127c64f... --image pf-127c64f-verify
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# repo-path -> candidate in-container paths (searched in order)
DEFAULT_FILES = {
    "stage-1/app/store.py": [
        "/app/app/store.py",
        "/app/store.py",
        "/src/app/store.py",
        "/workspace/app/store.py",
    ],
    "stage-1/app/dependencies.py": [
        "/app/app/dependencies.py",
        "/app/dependencies.py",
        "/src/app/dependencies.py",
        "/workspace/app/dependencies.py",
    ],
}

_IMAGE_PATH_CACHE: dict[tuple[str, str], str] = {}


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and p.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(args)}\n  rc={p.returncode}\n  {p.stderr.strip()}"
        )
    return p


def _parse_iso(value: str) -> datetime:
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    if value.endswith(("+0800", "-0700")):  # compact offsets -> : separators
        value = value[:-5] + value[-5:-2] + ":" + value[-2:]
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def commit_time(sha: str) -> datetime:
    return _parse_iso(_run(["git", "show", "-s", "--format=%cI", sha]).stdout.strip())


def image_created(image: str) -> datetime:
    return _parse_iso(
        _run(["docker", "image", "inspect", "--format", "{{.Created}}", image]).stdout.strip()
    )


def committed_blob_sha1(sha: str, repo_path: str) -> str:
    return _run(["git", "rev-parse", f"{sha}:{repo_path}"]).stdout.strip()


def _image_file_exists(image: str, image_path: str) -> bool:
    cid = _run(["docker", "create", image]).stdout.strip()
    try:
        with tempfile.TemporaryDirectory() as td:
            probe = Path(td) / "probe"
            p = subprocess.run(
                ["docker", "cp", f"{cid}:{image_path}", str(probe)],
                capture_output=True,
                text=True,
            )
            return p.returncode == 0 and probe.exists()
    finally:
        _run(["docker", "rm", "-f", cid], check=False)


def _image_file_path(image: str, candidates: list[str]) -> str | None:
    key = (image, candidates[0])
    cached = _IMAGE_PATH_CACHE.get(key)
    if cached is not None:
        return cached or None
    for cand in candidates:
        if _image_file_exists(image, cand):
            _IMAGE_PATH_CACHE[key] = cand
            return cand
    _IMAGE_PATH_CACHE[key] = ""
    return None


def _git_blob_sha1(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode("utf-8") + data).hexdigest()


def image_blob_sha1(image: str, image_path: str) -> str:
    cid = _run(["docker", "create", image]).stdout.strip()
    try:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / Path(image_path).name
            _run(["docker", "cp", f"{cid}:{image_path}", str(out)])
            return _git_blob_sha1(out.read_bytes())
    finally:
        _run(["docker", "rm", "-f", cid], check=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sha", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument(
        "--file",
        action="append",
        default=[],
        metavar="REPO_PATH=IMAGE_PATH",
        help="override an audited file mapping",
    )
    args = ap.parse_args()

    files = {p: list(c) for p, c in DEFAULT_FILES.items()}
    for spec in args.file:
        repo_path, image_path = spec.split("=", 1)
        files[repo_path] = [image_path]

    failures = 0

    ctime = commit_time(args.sha)
    created = image_created(args.image)
    ok = created >= ctime
    failures += 0 if ok else 1
    print(f"[IMAGE_AGE] commit_time={ctime.isoformat()} image_created={created.isoformat()} "
          f"{'PASS' if ok else 'FAIL (image predates commit)'}")

    for repo_path, candidates in sorted(files.items()):
        committed = committed_blob_sha1(args.sha, repo_path)
        image_path = _image_file_path(args.image, candidates)
        if image_path is None:
            print(f"[BLOB_HASH] {repo_path}: FAIL (no matching in-image path in "
                  f"{candidates})")
            failures += 1
            continue
        in_image = image_blob_sha1(args.image, image_path)
        ok = in_image == committed
        failures += 0 if ok else 1
        print(f"[BLOB_HASH] {repo_path}: image={in_image[:12]} committed={committed[:12]} "
              f"({'PASS' if ok else 'FAIL (mismatch)'})")

    print("RESULT: " + ("PASS" if failures == 0 else f"FAIL ({failures} check(s))"))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())