#!/usr/bin/env python3
"""Copy every rendered post into one AirDrop-ready folder, with its caption beside the slides.

Usage: python3 package.py [destination]   (default: ~/Downloads/AI Instagram Posts)

Guards:
  - Refuses to package while any post folder is missing slides, so a half-finished
    rebuild never reaches your phone.
  - Rebuilds the destination from scratch, so stale slides from an older version never mix in.
"""
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEST = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path.home() / "Downloads" / "AI Instagram Posts"


def captions():
    """Pull each post's caption out of the copy doc, in post order."""
    text = (ROOT / "ai-carousels-oct-2026.txt").read_text()
    found = re.findall(r"^Caption\n(.+?)\n\nSources", text, flags=re.M | re.S)
    return [c.strip() for c in found]


def main():
    posts = json.loads((ROOT / "slides.json").read_text())["posts"]
    caps = captions()
    if len(caps) != len(posts):
        sys.exit(f"package.py: found {len(caps)} captions for {len(posts)} posts in ai-carousels-oct-2026.txt")
    problems = []
    for p in posts:
        have = len(list((ROOT / "out" / p["slug"]).glob("slide-*.png")))
        if have != len(p["slides"]):
            problems.append(f"{p['slug']}: {have} of {len(p['slides'])} slides (run build.py first)")
    if problems:
        sys.exit("package.py: not packaging an incomplete build:\n  " + "\n  ".join(problems))

    if DEST.exists():
        shutil.rmtree(DEST)
    for p, cap in zip(posts, caps):
        folder = DEST / f"Post {p['slug']}"
        folder.mkdir(parents=True)
        for png in sorted((ROOT / "out" / p["slug"]).glob("slide-*.png")):
            shutil.copy2(png, folder / png.name)
        (folder / "caption.txt").write_text(cap + "\n")
    print(f"{DEST} ({len(posts)} posts)")


if __name__ == "__main__":
    main()
