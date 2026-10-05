"""Emit the authoritative pocketful-stage2 tag -> image ID table.

Tag state has been carried forward in chat prose three times and was wrong
every time. This reads the Docker daemon directly so nobody has to trust a
recalled table again.

    python verify/tagstate.py
"""

from __future__ import annotations

import json
import subprocess

REPO = "pocketful-stage2"

# Artifacts that must never ship, retained for evidence only.
NEVER_SHIP = {
    "w5": "unauthenticated import double-spend (4eeb3e45fc85)",
    "w6": "exports live bearer tokens; ungated (01bf6c15cc94)",
    "w4": "no void route exists; W4 UNGATED (741ae47f6f2e)",
}


def main() -> None:
    raw = subprocess.run(
        ["docker", "images", "--all", "--format", "{{.Repository}}\t{{.Tag}}\t{{.ID}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    rows: list[tuple[str, str, str]] = []
    for line in raw.splitlines():
        repo, _, rest = line.partition("\t")
        tag, _, short_id = rest.partition("\t")
        if repo == REPO and tag:
            rows.append((tag, short_id, short_id))

    rows.sort(key=lambda r: r[0])

    full: dict[str, str] = {}
    for tag, short_id, _ in rows:
        out = subprocess.run(
            ["docker", "image", "inspect", short_id, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
        )
        if out.returncode == 0 and out.stdout.strip():
            full[tag] = out.stdout.strip()

    print("TAG                 IMAGE ID (sha256)                                    NOTE")
    print("-" * 110)
    for tag, short_id, _ in rows:
        note = NEVER_SHIP.get(tag, "")
        if tag == "latest":
            note = "permanently banned as a gate target"
        print(f"{tag:<20} {full.get(tag, short_id):<52} {note}")

    print()
    print("HEAD commit:")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    subject = subprocess.run(
        ["git", "log", "--format=%s", "-1"], capture_output=True, text=True
    ).stdout.strip()
    print(f"  {head[:7]}  {subject}")

    print()
    print(json.dumps({"tags": full, "head": head}, indent=2))


if __name__ == "__main__":
    main()