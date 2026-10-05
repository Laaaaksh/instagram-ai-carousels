#!/usr/bin/env python3
"""Render every carousel slide in slides.json to a 1080x1350 PNG.

Usage:
  python3 build.py              # all posts
  python3 build.py 04           # only posts whose slug starts with "04"

Layouts match the Claude Design canvas "AI News Carousels"
(https://claude.ai/artifact/G9cS2vNXooD8gykCx2ksoB): cover, point (stat or text), list, cta.
Change the look there first, then mirror it in CSS below, so canvas and output never drift.

Guards (each one fails loudly, never ships a broken slide):
  - slides.json is validated before anything renders (fields, types, copy rules).
  - Each slide is checked in headless Chrome for text overflow and for the web fonts
    actually loading (a missing font silently falls back to a system face otherwise).
  - Each PNG is checked for the exact 1080x1350 size.
  - Chrome is retried once per slide, because font fetches are occasionally flaky.
"""
import html
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
W, H = 1080, 1350
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

INK, PAPER, ACCENT, MUTED_DARK, MUTED_LIGHT = "#0E0F0C", "#F2EFE6", "#C6F432", "#A8A79F", "#5A5952"

# Humaniser copy rules: em dashes and semicolons never reach a slide.
BANNED_CHARS = {"—": "em dash", ";": "semicolon"}


class BuildError(Exception):
    pass


# ---------- validation ----------

def validate(data):
    errors = []
    for key in ("series", "handle", "posts"):
        if key not in data:
            errors.append(f"top level: missing '{key}'")
    # Placeholders like @[YOUR HANDLE] must never reach a posted slide.
    for m in re.findall(r"\[[A-Z][A-Z ]+\]", json.dumps(data)):
        errors.append(f"unfilled placeholder {m}")
    for p in data.get("posts", []):
        where = p.get("slug", "<no slug>")
        if not p.get("slug") or not p.get("tag") or not p.get("slides"):
            errors.append(f"{where}: needs slug, tag and slides")
            continue
        types = [s.get("type") for s in p["slides"]]
        if types[0] != "cover" or types[-1] != "cta":
            errors.append(f"{where}: first slide must be cover, last must be cta")
        if len(types) > 10:
            errors.append(f"{where}: Instagram allows at most 10 slides per post (has {len(types)})")
        for i, s in enumerate(p["slides"], 1):
            t = s.get("type")
            if t not in ("cover", "point", "list", "cta"):
                errors.append(f"{where} slide {i}: unknown type {t!r}")
            if not s.get("title"):
                errors.append(f"{where} slide {i}: missing title")
            if t == "cover" and s.get("hl") and s["hl"] not in s["title"]:
                errors.append(f"{where} slide {i}: hl {s['hl']!r} is not in the title")
            if t == "list" and not s.get("items"):
                errors.append(f"{where} slide {i}: list slide needs items")
            if s.get("big") and not s.get("bigLabel"):
                errors.append(f"{where} slide {i}: big number needs a bigLabel")
            text = json.dumps(s, ensure_ascii=False)
            for ch, name in BANNED_CHARS.items():
                if ch in text:
                    errors.append(f"{where} slide {i}: contains a {name}")
    if errors:
        raise BuildError("slides.json is invalid:\n  " + "\n  ".join(errors))


# ---------- markup ----------

CSS = f"""
body{{margin:0}}
.root{{width:{W}px;height:{H}px;box-sizing:border-box;padding:72px 80px;font-family:'Space Grotesk',sans-serif;
  display:flex;flex-direction:column;justify-content:space-between;overflow:hidden}}
.mono{{font-family:'JetBrains Mono',monospace;letter-spacing:.08em}}
.top,.bottom{{display:flex;justify-content:space-between;align-items:center}}
.top{{font-size:24px}} .bottom{{font-size:26px;letter-spacing:.06em}}
.dark{{background:{INK};color:{PAPER}}} .dark .top{{color:{MUTED_DARK}}}
.light{{background:{PAPER};color:{INK}}} .light .top{{color:{MUTED_LIGHT}}}
.lime{{background:{ACCENT};color:{INK}}}
.body{{display:flex;flex-direction:column}}
.tag{{align-self:flex-start;background:{ACCENT};color:{INK};font-size:26px;padding:10px 20px}}
h1{{margin:0;font-size:112px;line-height:1.02;font-weight:700;letter-spacing:-.035em}}
h2{{margin:0;font-size:72px;line-height:1.05;font-weight:700;letter-spacing:-.03em}}
.hl{{background:{ACCENT};color:{INK};padding:0 12px;-webkit-box-decoration-break:clone;box-decoration-break:clone}}
.sub{{margin:0;font-size:46px;line-height:1.25;font-weight:500}}
.dark .sub{{color:{MUTED_DARK}}}
.stat{{display:flex;flex-direction:column;gap:20px;border-top:6px solid {INK};padding-top:24px}}
.big{{font-weight:700;letter-spacing:-.05em;line-height:1}}
.big span{{display:inline-block;background:{ACCENT};padding:.04em .08em 0;line-height:.86}}
.biglabel{{font-size:40px;line-height:1.25;font-weight:500;color:{MUTED_LIGHT}}}
.lines{{display:flex;flex-direction:column;gap:14px;font-size:44px;line-height:1.25;font-weight:500}}
.lines p{{margin:0}}
.textonly .lines{{font-size:56px;gap:22px;border-top:6px solid {INK};padding-top:32px}}
.items{{border-top:6px solid {INK}}}
.item{{display:flex;gap:32px;align-items:baseline;padding:32px 0;border-bottom:2px solid {INK}}}
.item .n{{font-size:34px;background:{ACCENT};padding:4px 14px}}
.item .t{{font-size:52px;line-height:1.2;font-weight:500}}
.handle{{background:{INK};color:{ACCENT};padding:10px 20px}}
"""

# Runs in Chrome before the DOM is dumped: records overflow and whether fonts loaded.
CHECK_JS = f"""
<script>
document.fonts.ready.then(() => {{
  const r = document.querySelector('.root');
  const over = [];
  if (r.scrollHeight > {H} + 1) over.push('height ' + r.scrollHeight);
  r.querySelectorAll('*').forEach(el => {{
    if (el.scrollWidth > el.clientWidth + 1 && getComputedStyle(el).display !== 'inline')
      over.push('wide ' + el.className + ' ' + el.scrollWidth);
  }});
  // Overlap guard: stacked blocks must not paint over each other (a highlight once covered its label).
  // The big number's painted box is its inner highlight span, which can poke out of its line box.
  const painted = k => k.classList.contains('big') ? k.firstElementChild.getBoundingClientRect() : k.getBoundingClientRect();
  r.querySelectorAll('.root, .body, .stat, .lines, .items').forEach(box => {{
    const kids = [...box.children].map(painted);
    for (let i = 1; i < kids.length; i++)
      if (kids[i].top < kids[i-1].bottom - 1) over.push('overlap in ' + box.className + ' at child ' + i);
  }});
  // The highlight must also stay below the divider line drawn on top of .stat.
  r.querySelectorAll('.stat').forEach(st => {{
    const line = st.getBoundingClientRect().top + 6;
    if (painted(st.firstElementChild).top < line - 1) over.push('highlight covers divider');
  }});
  const fonts = document.fonts.check('700 100px "Space Grotesk"') && document.fonts.check('500 20px "JetBrains Mono"');
  document.body.setAttribute('data-check', JSON.stringify({{over, fonts}}));
}});
</script>
"""


def e(s):
    return html.escape(s, quote=True)


def big_size(text):
    n = len(text)
    return 260 if n <= 4 else 210 if n <= 6 else 170 if n <= 8 else 130


def slide_html(data, post, s, idx, total, check=False):
    count = f"{idx:02d} / {total:02d}"
    arrow = "SWIPE &#8594;"
    t = s["type"]
    if t == "cover":
        cls = "dark"
        top_left = e(data['series'])  # no date: posts go out over several days
        title = e(s["title"])
        if s.get("hl"):
            title = title.replace(e(s["hl"]), f'<span class="hl">{e(s["hl"])}</span>', 1)
        sub = f'<p class="sub">{e(s["sub"])}</p>' if s.get("sub") else ""
        body = f'<div class="body" style="gap:40px"><span class="tag mono">{e(post["tag"])}</span><h1>{title}</h1>{sub}</div>'
        bottom = f'<span>{e(data["handle"])}</span><span style="color:{ACCENT}">{arrow}</span>'
    elif t == "cta":
        cls = "lime"
        top_left = e(data['series'])  # no date: posts go out over several days
        size = 116 if len(s["title"]) <= 50 else 96
        body = (f'<div class="body" style="gap:40px"><h2 style="font-size:{size}px;line-height:1;letter-spacing:-.04em">{e(s["title"])}</h2>'
                f'<p class="sub">{e(s.get("sub", ""))}</p></div>')
        bottom = f'<span class="handle">{e(data["handle"])}</span><span>SAVE / SHARE / FOLLOW</span>'
    else:
        cls = "light"
        top_left = f"{e(post['tag'])} / {e(s.get('kicker', f'{idx:02d}'))}"
        bottom = f'<span>{e(data["handle"])}</span><span>{arrow}</span>'
        if t == "list":
            items = "".join(f'<div class="item"><span class="n mono">{i:02d}</span><span class="t">{e(x)}</span></div>'
                            for i, x in enumerate(s["items"], 1))
            body = f'<div class="body" style="gap:48px"><h2 style="font-size:88px;line-height:1.02">{e(s["title"])}</h2><div class="items">{items}</div></div>'
        else:
            lines = "".join(f"<p>{e(x)}</p>" for x in s.get("lines", []))
            if s.get("big"):
                stat = (f'<div class="stat"><span class="big" style="font-size:{big_size(s["big"])}px"><span>{e(s["big"])}</span></span>'
                        f'<span class="biglabel">{e(s["bigLabel"])}</span></div>')
                body = f'<div class="body" style="gap:28px"><h2>{e(s["title"])}</h2>{stat}<div class="lines">{lines}</div></div>'
            else:
                cls += " textonly"
                body = f'<div class="body" style="gap:40px"><h2 style="font-size:96px;line-height:1.02">{e(s["title"])}</h2><div class="lines">{lines}</div></div>'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@500&family=Space+Grotesk:wght@500;700&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body>
<div class="root {cls}"><div class="top mono"><span>{top_left}</span><span>{count}</span></div>{body}<div class="bottom mono">{bottom}</div></div>
{CHECK_JS if check else ""}</body></html>"""


# ---------- rendering ----------

def chrome(args):
    base = [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
            "--virtual-time-budget=6000", f"--window-size={W},{H}"]
    return subprocess.run(base + args, capture_output=True, text=True, timeout=60)


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise BuildError(f"{path} is not a PNG")
    return struct.unpack(">II", head[16:24])


def render(data, post, s, idx, total, out_png):
    with tempfile.TemporaryDirectory() as tmp:
        check_file = Path(tmp) / "check.html"
        shot_file = Path(tmp) / "shot.html"
        check_file.write_text(slide_html(data, post, s, idx, total, check=True))
        shot_file.write_text(slide_html(data, post, s, idx, total))
        last = "unknown error"
        for _ in range(2):  # retry once: font fetches are occasionally flaky
            dom = chrome(["--dump-dom", f"file://{check_file}"]).stdout
            m = re.search(r"data-check=\"([^\"]*)\"", dom)
            if not m:
                last = "check script did not run"
                continue
            result = json.loads(html.unescape(m.group(1)))
            if not result["fonts"]:
                last = "web fonts did not load (offline?)"
                continue
            if result["over"]:
                raise BuildError(f"{post['slug']} slide {idx}: text overflows ({', '.join(result['over'])}). Shorten the copy.")
            chrome([f"--screenshot={out_png}", f"file://{shot_file}"])
            if out_png.exists() and png_size(out_png) == (W, H):
                return
            last = f"bad screenshot size {png_size(out_png) if out_png.exists() else 'missing'}"
        raise BuildError(f"{post['slug']} slide {idx}: {last}")


def main():
    if not Path(CHROME).exists():
        raise BuildError(f"Chrome not found at {CHROME}. Set CHROME=/path/to/chrome.")
    data = json.loads((ROOT / "slides.json").read_text())
    validate(data)
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    posts = [p for p in data["posts"] if p["slug"].startswith(only)]
    if not posts:
        raise BuildError(f"no post slug starts with {only!r}")
    for post in posts:
        folder = OUT / post["slug"]
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*.png"):
            old.unlink()  # stale slides from a longer earlier version would get posted by mistake
        total = len(post["slides"])
        for idx, s in enumerate(post["slides"], 1):
            render(data, post, s, idx, total, folder / f"slide-{idx:02d}.png")
        print(f"{post['slug']}: {total} slides")


if __name__ == "__main__":
    try:
        main()
    except BuildError as err:
        sys.exit(f"build.py: {err}")
