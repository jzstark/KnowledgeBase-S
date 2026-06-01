#!/usr/bin/env python3
"""
Phase 2 migration: rename `raw_ref:` → `storage_key:` in wiki frontmatter.

Run on VPS after deploying Phase 1 code changes.
Safe to re-run (idempotent: skips files that already have storage_key:).

Usage:
  python3 migrate_wiki_raw_ref.py [--dry-run] [--wiki-dir /path/to/user_data/default/wiki]
"""

import argparse
import re
import sys
from pathlib import Path

FRONTMATTER_RE = re.compile(r"^(---\n)(.*?)(---\n)", re.DOTALL)
RAW_REF_LINE_RE = re.compile(r"^raw_ref:", re.MULTILINE)


def migrate_file(path: Path, dry_run: bool) -> bool:
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return False

    fm = m.group(2)
    if not RAW_REF_LINE_RE.search(fm):
        return False
    if re.search(r"^storage_key:", fm, re.MULTILINE):
        return False  # already migrated

    new_fm = RAW_REF_LINE_RE.sub("storage_key:", fm)
    new_text = m.group(1) + new_fm + m.group(3) + text[m.end():]

    if not dry_run:
        path.write_text(new_text, encoding="utf-8")
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print changes without writing")
    parser.add_argument("--wiki-dir", default="/app/user_data/default/wiki")
    args = parser.parse_args()

    wiki_dir = Path(args.wiki_dir)
    if not wiki_dir.exists():
        print(f"error: wiki dir not found: {wiki_dir}", file=sys.stderr)
        sys.exit(1)

    updated = 0
    skipped = 0
    for subdir in ("articles", "indices", "summaries", "entities"):
        sd = wiki_dir / subdir
        if not sd.exists():
            continue
        for f in sorted(sd.glob("*.md")):
            if migrate_file(f, args.dry_run):
                print(f"{'[dry] ' if args.dry_run else ''}updated: {f.name}")
                updated += 1
            else:
                skipped += 1

    label = "would update" if args.dry_run else "updated"
    print(f"\ndone: {label} {updated} files, skipped {skipped} (no raw_ref or already migrated)")


if __name__ == "__main__":
    main()
