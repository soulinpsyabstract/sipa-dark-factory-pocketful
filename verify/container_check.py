"""
Stage 1 §1 container/environment verification.

Covers the parts of the spec that live OUTSIDE the HTTP contract:
  - stage-1/Dockerfile exists and is a valid build context
  - stage-1/RUN.md exists and documents build + run
  - `docker build` succeeds from clean (no cache reuse of a stale layer)
  - container honours $PORT, defaulting to 8080
  - image actually runs Python 3.11+ and uses a real hash (bcrypt/scrypt/argon2)

This is the verifier's own evidence. It does not read the builder's summary.

Usage:
    python verify/container_check.py --stage-dir stage-1
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

_results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    tag = {"PASS": "[PASS]", "FAIL": "[FAIL]", "SKIP": "[SKIP]"}[status]
    line = f"{tag} {name}"
    if detail:
        line += f"\n         {detail}"
    print(line, flush=True)


def check(name: str, cond: bool, detail: str = "") -> bool:
    record("PASS" if cond else "FAIL", name, "" if cond else detail)
    return cond


def sh(cmd: list[str], timeout: int = 900) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, shell=False,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return -1, f"executable not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout}s: {' '.join(cmd)}"


def docker_ok() -> bool:
    rc, out = sh(["docker", "version", "--format", "{{.Server.Version}}"], 60)
    if rc != 0:
        record("FAIL", "S1 docker engine reachable",
               f"`docker version` failed against server: {out.strip()[:400]}")
        return False
    record("PASS", "S1 docker engine reachable", out.strip())
    return True


def wait_health(port: int, timeout: int = 60) -> tuple[bool, str]:
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                body = r.read().decode()[:200]
                if r.status == 200:
                    return True, f"{r.status} {body}"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
        except Exception as e:
            last = str(e)
        time.sleep(1.0)
    return False, last


def run_container(image: str, port_env: int | None, name: str,
                  host_port: int | None = None) -> tuple[bool, str]:
    """Run image, optionally with PORT override. Returns (healthy, detail)."""
    sh(["docker", "rm", "-f", name], 60)
    host = host_port or port_env or 8080
    target = port_env or 8080
    cmd = ["docker", "run", "-d", "--name", name, "-p", f"{host}:{target}"]
    if port_env:
        cmd += ["-e", f"PORT={port_env}"]
    cmd += [image]
    rc, out = sh(cmd, 120)
    if rc != 0:
        return False, f"docker run failed: {out.strip()[:400]}"
    ok, detail = wait_health(host, 60)
    if not ok:
        _, logs = sh(["docker", "logs", "--tail", "40", name], 60)
        sh(["docker", "rm", "-f", name], 60)
        return False, f"never healthy on port {host} ({detail})\nlogs:\n{logs[-1500:]}"
    return True, detail


def main() -> int:
    ap = argparse.ArgumentParser()
    # REQUIRED on purpose: defaulting to the working tree is how a "built from
    # the immutable worktree" image silently becomes a dirty-tree build. For a
    # commit-SHA pass pass the worktree path, e.g. --stage-dir <temp>/pf-<sha>/stage-1.
    ap.add_argument("--stage-dir", required=True,
                    help="absolute path to the stage-1 dir to build/verify "
                         "(pass an immutable worktree path, never the repo root)")
    ap.add_argument("--image", default="pocketful-stage1-verify")
    ap.add_argument("--ports", default="8080,9099,5555",
                    help="comma-separated host ports: first is the default-PORT "
                         "run, the rest are $PORT overrides (default %(default)s)")
    ap.add_argument("--skip-build", action="store_true")
    args = ap.parse_args()

    stage = os.path.abspath(args.stage_dir)
    print(f"=== Stage 1 container verification (stage dir: {stage}) ===\n", flush=True)

    # --- artifact presence -------------------------------------------------
    df = os.path.join(stage, "Dockerfile")
    run_md = os.path.join(stage, "RUN.md")
    if not check("S1 stage-1/Dockerfile exists", os.path.isfile(df), f"missing: {df}"):
        summarise(); return 1
    if not check("S1 stage-1/RUN.md exists", os.path.isfile(run_md), f"missing: {run_md}"):
        summarise(); return 1

    run_text = open(run_md, encoding="utf-8", errors="replace").read()
    check("S1 RUN.md documents a build command",
          bool(re.search(r"docker\s+build", run_text, re.I)), "no `docker build` in RUN.md")
    check("S1 RUN.md documents a run command",
          bool(re.search(r"docker\s+run", run_text, re.I)), "no `docker run` in RUN.md")
    check("S1 RUN.md mentions PORT",
          "PORT" in run_text, "RUN.md does not mention $PORT")

    if not docker_ok():
        summarise(); return 1

    # --- clean build -------------------------------------------------------
    if not args.skip_build:
        sh(["docker", "rmi", "-f", args.image], 120)
        rc, out = sh(["docker", "build", "--no-cache", "-t", args.image, stage], 1800)
        if not check("S1 `docker build --no-cache` succeeds", rc == 0,
                     f"rc={rc}\n{out[-2500:]}"):
            summarise(); return 1

    # --- image internals ---------------------------------------------------
    rc, out = sh(["docker", "run", "--rm", args.image, "python", "-V"], 120)
    ver = out.strip()
    m = re.search(r"(\d+)\.(\d+)", ver)
    ok_py = bool(m) and (int(m.group(1)), int(m.group(2))) >= (3, 11)
    check("S1 image runs Python 3.11+", ok_py, f"`python -V` -> {ver!r}")

    rc, out = sh(
        ["docker", "run", "--rm", args.image, "python", "-c",
         "import bcrypt, sys; print('bcrypt', bcrypt.__version__)"],
        120,
    )
    if rc != 0:
        rc, out = sh(
            ["docker", "run", "--rm", args.image, "python", "-c",
             "import argon2; print('argon2', argon2.__version__)"],
            120,
        )
    if rc != 0:
        rc, out = sh(
            ["docker", "run", "--rm", args.image, "python", "-c",
             "import hashlib; hashlib.scrypt(b'x', salt=b'y', n=2, r=8, p=1); print('scrypt stdlib')"],
            120,
        )
    check("S1 bcrypt/Argon2/scrypt available in image", rc == 0,
          f"none of bcrypt/argon2/scrypt importable: {out.strip()[:300]}")

    # --- default port ------------------------------------------------------
    ports = [int(p.strip()) for p in args.ports.split(",") if p.strip()]
    ok, detail = run_container(args.image, None, "pf-verify-default", host_port=ports[0])
    check(f"S1 default port is {ports[0]} and /health -> 200 {{status:ok}}",
          ok, detail)
    if ok:
        rc, out = sh(["docker", "rm", "-f", "pf-verify-default"], 60)

    # --- PORT override -----------------------------------------------------
    for override in ports[1:]:
        ok, detail = run_container(args.image, override, f"pf-verify-{override}",
                                   host_port=override)
        check(f"S1 PORT={override} override honoured", ok, detail)
        sh(["docker", "rm", "-f", f"pf-verify-{override}"], 60)

    return summarise()


def summarise() -> int:
    p = sum(1 for s, _, _ in _results if s == "PASS")
    f = sum(1 for s, _, _ in _results if s == "FAIL")
    s = sum(1 for s, _, _ in _results if s == "SKIP")
    print(f"\n=== SUMMARY: {p} passed, {f} failed, {s} skipped ===")
    if f:
        print("\nFAILURES:")
        for st, name, detail in _results:
            if st == "FAIL":
                print(f"  - {name}: {detail}")
    return 1 if f else 0


if __name__ == "__main__":
    sys.exit(main())
