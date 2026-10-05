#!/usr/bin/env python3
"""Heading-structure check for RULINGS.md.

Every other instrument in this session was a substring search or a hash. None of
them read structure. This one does, because the defect that reached a sealed
receipt was not a wrong claim -- it was a correct claim in the wrong place.

The incident: an edit anchored on a substring that started mid-line left a stray
"### Receipt tiers - " prefix glued to a new heading, producing

    ### Receipt tiers - ### The `e8c0381` authorship question is NOT adjudicated ...

The prose on either side was correct. The document was not. And every string a
reader would grep for -- "Receipt tiers", "e8c0381" -- was still present, so a
substring check returned a clean hit on structurally broken content.

Rule: a check that greps for strings succeeds on broken structure, because the
broken structure still contains the strings. Presence is not position, and
position is what structure is.

Run with no arguments to check the live ledger. --self-test proves the checks
actually fire, which is the point: an instrument adopted without a positive
control is the 405-for-404 error waiting to recur.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

LEDGER = Path(__file__).resolve().parent.parent / "RULINGS.md"
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
# A heading marker ANYWHERE in the line, not just the leading run. The real defect
# was "### Receipt tiers - ### The ...", where a dash and prose sit between the
# two markers. Counting only the leading run sees ONE heading and passes. That
# mistake survived the first version of this instrument and was caught by its own
# positive control.
MARKER_ANYWHERE = re.compile(r"#{1,6}\s")


def parse_headings(text: str) -> list[tuple[int, int, int, str]]:
    """Return (line_no, level, marker_count, text) for each heading line."""
    found = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.startswith("#"):
            continue
        m = HEADING.match(line)
        if m:
            found.append((lineno, len(m.group(1)), len(MARKER_ANYWHERE.findall(line)), m.group(2).strip()))
    return found


def check(text: str) -> list[str]:
    problems: list[str] = []
    headings = parse_headings(text)

    if not headings:
        problems.append("no headings found at all -- parser is broken, not the document")
        return problems

    for lineno, level, markers, htext in headings:
        # 1. A line carrying two heading markers anywhere is two headings glued
        #    together. Presence, not adjacency -- see MARKER_ANYWHERE.
        if markers > 1:
            problems.append(
                f"line {lineno}: {markers} heading markers on one line -- "
                f"headings merged: {htext[:70]!r}"
            )
        # 2. A heading whose own text restarts with a marker is the same defect.
        if htext.startswith("#"):
            problems.append(f"line {lineno}: heading text begins with '#': {htext[:70]!r}")

    # 3. Level jumps of more than one are how content gets silently reparented:
    #    a reader looking for a section lands inside whatever preceded it.
    for (prev_lineno, prev_level, _, _), (lineno, level, _, htext) in zip(headings, headings[1:]):
        if level > prev_level + 1:
            problems.append(
                f"line {lineno}: heading jumps h{prev_level} -> h{level} "
                f"(content reparented): {htext[:60]!r}"
            )
    return problems


def self_test() -> int:
    """Positive control. Each case MUST fail, or the check does not work."""
    cases = [
        (
            "merged headings (the actual defect)",
            "### Receipt tiers - ### The `e8c0381` authorship question is NOT adjudicated.\nbody\n",
        ),
        (
            "heading text restarting with a marker",
            "## Header\n### Outer ### Inner\nbody\n",
        ),
        (
            "level jump reparenting content",
            "# Title\n### Skipped a level\nbody\n",
        ),
    ]
    failures = 0
    for name, sample in cases:
        found = check(sample)
        if found:
            print(f"  DETECTED  {name}")
            for f in found:
                print(f"            {f}")
        else:
            print(f"  MISSED    {name}  <-- instrument is broken")
            failures += 1
    if failures:
        print(f"\nself-test FAILED: {failures} case(s) not detected")
        return 1
    print(f"\nself-test PASSED: all {len(cases)} broken forms detected")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        print("positive control -- each case MUST fail:")
        return self_test()

    # --file lets the check be aimed at any historical commit, which is how a
    # control proves the instrument detects the defect it was written for rather
    # than merely passing the document it was written beside.
    target = LEDGER
    if "--file" in sys.argv:
        arg = sys.argv[sys.argv.index("--file") + 1]
        target = Path(arg)
        print(f"checking {target} (not the live ledger -- control run)")
    elif len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        target = Path(sys.argv[1])
        print(f"checking {target}")

    if not target.exists():
        print(f"FAIL: ledger not found at {target}")
        return 2

    text = target.read_text(encoding="utf-8")
    problems = check(text)
    headings = parse_headings(text)

    if problems:
        print(f"FAIL: {len(problems)} structural problem(s) in {target.name}")
        for p in problems:
            print(f"  {p}")
        return 1

    print(f"OK: {target.name} structure sound -- {len(headings)} headings, "
          f"no merged lines, no level jumps, no reparented content")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
