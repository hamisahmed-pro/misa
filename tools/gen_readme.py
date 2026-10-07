#!/usr/bin/env python3
"""Generate README status section from spec/roadmap.json.

Usage: python3 tools/gen_readme.py [--check]

--check: exit 1 if README.md is out of sync (for CI).
Otherwise: update README.md in place.

(Implements Benchmark Review Plan §23: documentation drift fix.)
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROADMAP = os.path.join(ROOT, "spec", "roadmap.json")
README = os.path.join(ROOT, "README.md")

STATUS_EMOJI = {
    "complete": "✅",
    "in-progress": "🚧",
    "not-started": "⏳",
}


def generate_status(roadmap):
    lines = ["## Project Status", ""]
    lines.append(f"**Version:** {roadmap['version']}")
    lines.append("")
    lines.append("| Phase | Name | Status |")
    lines.append("|-------|------|--------|")
    for num in sorted(roadmap["phases"].keys(), key=int):
        p = roadmap["phases"][num]
        emoji = STATUS_EMOJI.get(p["status"], "❓")
        notes = f" — {p['notes']}" if p.get("notes") else ""
        lines.append(f"| {num} | {p['name']} | {emoji} {p['status']}{notes} |")
    lines.append("")
    tc = roadmap["test_counts"]
    lines.append(f"**Tests:** {tc['unit']} unit, {tc['aot_parity']} AOT parity, "
                 f"{tc['differential']} differential")
    lines.append("")
    lines.append("<!-- STATUS:GENERATED — do not hand-edit. "
                 "Run tools/gen_readme.py to regenerate. -->")
    return "\n".join(lines)


def main():
    with open(ROADMAP) as f:
        roadmap = json.load(f)
    status_md = generate_status(roadmap)

    with open(README) as f:
        readme = f.read()

    # Replace between markers, or append if not present
    start_marker = "<!-- STATUS:START -->"
    end_marker = "<!-- STATUS:END -->"
    if start_marker in readme and end_marker in readme:
        pattern = re.compile(
            re.escape(start_marker) + r".*?" + re.escape(end_marker),
            re.DOTALL,
        )
        new_readme = pattern.sub(
            start_marker + "\n" + status_md + "\n" + end_marker, readme
        )
    else:
        new_readme = readme.rstrip() + "\n\n" + start_marker + "\n" + \
            status_md + "\n" + end_marker + "\n"

    check_only = "--check" in sys.argv
    if new_readme != readme:
        if check_only:
            print("README.md is out of sync with spec/roadmap.json")
            return 1
        with open(README, "w") as f:
            f.write(new_readme)
        print("README.md updated from spec/roadmap.json")
    else:
        print("README.md is in sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
