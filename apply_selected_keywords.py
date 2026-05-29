#!/usr/bin/env python3
"""
Apply keywords selected from a digest email.

Reads a GitHub issue body (env ISSUE_BODY) produced by the digest's
"Select keywords to add" link, finds the ticked checklist items
(`- [x] some keyword`), and appends them to keywords.json -> "selected_keywords"
(APPEND-ONLY, de-duplicated against every existing bucket).

Writes the list of newly added keywords to added_keywords.txt so the workflow
can comment on the issue.
"""

import os
import re
import json
from pathlib import Path

KEYWORDS_FILE = Path(__file__).with_name("keywords.json")
ADDED_FILE = Path(__file__).with_name("added_keywords.txt")

# Matches "- [x] keyword" / "* [X] keyword" (ticked checklist items only).
CHECKED_RE = re.compile(r"^\s*[-*]\s*\[[xX]\]\s*(.+?)\s*$", re.MULTILINE)


def main():
    body = os.environ.get("ISSUE_BODY", "")
    checked = [m.strip() for m in CHECKED_RE.findall(body)]
    # Strip any stray markdown/backticks and skip empties.
    checked = [re.sub(r"[`*_]", "", c).strip() for c in checked]
    checked = [c for c in checked if c]

    data = json.loads(KEYWORDS_FILE.read_text())
    existing = {
        k.lower()
        for k in (
            data.get("seed_topic_keywords", [])
            + data.get("seed_high_value_keywords", [])
            + data.get("auto_keywords", [])
            + data.get("selected_keywords", [])
        )
    }

    added = []
    for kw in checked:
        if kw.lower() in existing:
            continue
        added.append(kw)
        existing.add(kw.lower())

    if added:
        data["selected_keywords"] = data.get("selected_keywords", []) + added
        KEYWORDS_FILE.write_text(json.dumps(data, indent=2) + "\n")

    ADDED_FILE.write_text("\n".join(added))
    print(f"Ticked: {len(checked)} | newly added: {len(added)}")
    for kw in added:
        print(f"  + {kw}")


if __name__ == "__main__":
    main()
