#!/usr/bin/env python3
"""Schedule and publish the carousels to Instagram through the Instagram Graph API.

Commands
  prepare [--start YYYY-MM-DD] [--time HH:MM]   PNG slides -> publish/<slug>/*.jpg, write schedule.json
  shift --start YYYY-MM-DD                      move every unpublished post to new dates, same order
  check                                         preflight: token, account, quota, every image URL
  run [--dry-run]                               publish whatever is due (GitHub Actions calls this)
  status                                        print the schedule
  refresh-token                                 extend the 60-day token, print the new one's lifetime

Environment
  IG_ACCESS_TOKEN   long-lived Instagram token (Instagram API with Instagram Login)
  IMAGE_BASE_URL    public base URL of the publish/ folder,
                    e.g. https://raw.githubusercontent.com/<user>/<repo>/main/publish

Why the guards exist (do not remove):
  - Instagram has no "schedule" field in the API, so this script is the scheduler. A missed
    timer run is caught up on the next run, but a post more than MAX_LATE_HOURS late is marked
    "missed" instead of published, so a long outage never dumps several posts at once.
  - Before publishing, recent posts are checked for the same caption. If an earlier run published
    but crashed before saving the status, the post is marked done instead of posted twice.
  - Every image URL is fetched before any container is created. Instagram fails slowly and
    vaguely on a bad URL; this fails fast with the exact file named.
  - Network errors and Instagram 5xx/429 responses are retried with backoff.
  - At most one post is published per run, to stay far under the daily API cap.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCHEDULE = ROOT / "schedule.json"
PUBLISH_DIR = ROOT / "publish"
API = "https://graph.instagram.com/v23.0"
IST = timezone(timedelta(hours=5, minutes=30))

MAX_LATE_HOURS = float(os.environ.get("MAX_LATE_HOURS", "6"))  # knob: how late a post may still go out
MAX_ATTEMPTS = 3  # failed runs per post before it is parked as "failed" (shift re-queues it)
CONTAINER_WAIT_SECS = 300  # how long to wait for Instagram to process images
MAX_SLIDES = 10  # Instagram API carousel limit
JPEG_QUALITY = 92

# Default posting order: most time-sensitive news first, evergreen stories last.
DEFAULT_ORDER = ["01-openai-devday", "04-ai-cybersecurity", "02-claude-5-5", "06-ai-prices",
                 "03-anthropic-ipo", "08-quick-hits", "05-google-science", "07-ai-and-your-job"]


class PublishError(Exception):
    pass


# ---------- schedule file ----------

def load_schedule():
    if not SCHEDULE.exists():
        raise PublishError("schedule.json missing. Run: python3 ig_publish.py prepare")
    return json.loads(SCHEDULE.read_text())


def save_schedule(sched):
    tmp = SCHEDULE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sched, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(SCHEDULE)  # atomic: a crash mid-write never leaves a half-written schedule


def parse_when(s):
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise PublishError(f"time {s!r} has no UTC offset; write it like 2026-10-06T19:00:00+05:30")
    return dt


def assign_dates(posts, start, hhmm):
    h, m = (int(x) for x in hhmm.split(":"))
    day = datetime.strptime(start, "%Y-%m-%d").replace(hour=h, minute=m, tzinfo=IST)
    for i, p in enumerate(posts):
        p["when"] = (day + timedelta(days=i)).isoformat()


# ---------- prepare / shift ----------

def captions_by_slug():
    """Captions come from the copy doc, in the same order as slides.json."""
    text = (ROOT / "ai-carousels-oct-2026.txt").read_text()
    caps = [c.strip() for c in re.findall(r"^Caption\n(.+?)\n\nSources", text, flags=re.M | re.S)]
    slugs = [p["slug"] for p in json.loads((ROOT / "slides.json").read_text())["posts"]]
    if len(caps) != len(slugs):
        raise PublishError(f"found {len(caps)} captions for {len(slugs)} posts in ai-carousels-oct-2026.txt")
    return dict(zip(slugs, caps))


def cmd_prepare(args):
    from PIL import Image  # local-only dependency; GitHub Actions never runs prepare

    caps = captions_by_slug()
    old = {p["slug"]: p for p in load_schedule()["posts"]} if SCHEDULE.exists() else {}
    posts = []
    for slug in DEFAULT_ORDER:
        pngs = sorted((ROOT / "out" / slug).glob("slide-*.png"))
        if not pngs:
            raise PublishError(f"no slides in out/{slug}. Run build.py first.")
        if len(pngs) > MAX_SLIDES:
            raise PublishError(f"{slug} has {len(pngs)} slides; the API allows {MAX_SLIDES}")
        dest = PUBLISH_DIR / slug
        dest.mkdir(parents=True, exist_ok=True)
        for stale in dest.glob("*.jpg"):
            stale.unlink()
        files = []
        for png in pngs:
            jpg = dest / (png.stem + ".jpg")
            # The API accepts JPEG only. Flatten to RGB so no alpha channel sneaks in.
            Image.open(png).convert("RGB").save(jpg, "JPEG", quality=JPEG_QUALITY, optimize=True)
            if jpg.stat().st_size > 8 * 1024 * 1024:
                raise PublishError(f"{jpg} is over Instagram's 8 MB image limit")
            files.append(f"{slug}/{jpg.name}")
        prev = old.get(slug, {})
        posts.append({
            "slug": slug,
            "when": prev.get("when", ""),
            "caption": caps[slug],
            "images": files,
            "status": prev.get("status", "pending"),
            "media_id": prev.get("media_id"),
            "permalink": prev.get("permalink"),
            "last_error": prev.get("last_error"),
        })
    if args.start or not all(p["when"] for p in posts):
        pending = [p for p in posts if p["status"] != "published"]
        assign_dates(pending, args.start or (datetime.now(IST) + timedelta(days=1)).strftime("%Y-%m-%d"), args.time)
    save_schedule({"posts": posts})
    print(f"prepared {len(posts)} posts in {PUBLISH_DIR.name}/ and schedule.json")
    cmd_status(args)


def cmd_shift(args):
    sched = load_schedule()
    movable = [p for p in sched["posts"] if p["status"] in ("pending", "missed", "failed")]
    assign_dates(movable, args.start, args.time)
    for p in movable:
        p["status"], p["last_error"], p["attempts"] = "pending", None, 0
    save_schedule(sched)
    cmd_status(args)


def cmd_status(_args):
    for p in load_schedule()["posts"]:
        extra = p.get("permalink") or p.get("last_error") or ""
        print(f"{p['when'][:16].replace('T', ' ')}  {p['status']:<9}  {p['slug']:<22} {extra}")


# ---------- HTTP ----------

def http(method, url, params=None, tries=4):
    """Call the API. Retries network errors, 429 and 5xx with backoff; raises on 4xx with Meta's message."""
    data = None
    if params is not None and method == "GET":
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    elif params is not None:
        data = urllib.parse.urlencode(params).encode()
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method), timeout=60) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as err:
            body = err.read().decode(errors="replace")
            if err.code == 429 or err.code >= 500:
                last = f"HTTP {err.code}: {body[:300]}"
            else:
                try:
                    msg = json.loads(body)["error"]["message"]
                except Exception:
                    msg = body[:300]
                raise PublishError(f"Instagram API {err.code}: {msg}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
            last = f"network: {err}"
        time.sleep(min(60, 5 * 2 ** attempt))
    raise PublishError(f"gave up after {tries} tries ({last})")


def token():
    t = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if not t:
        raise PublishError("IG_ACCESS_TOKEN is not set (GitHub: Settings > Secrets > Actions)")
    return t


def api(method, path, **params):
    params["access_token"] = token()
    return http(method, f"{API}/{path}", params)


def image_base():
    b = os.environ.get("IMAGE_BASE_URL", "").strip().rstrip("/")
    if not b.startswith("https://"):
        raise PublishError("IMAGE_BASE_URL must be an https URL to the publish/ folder")
    return b


def check_image(url):
    """Instagram must be able to download each slide as a JPEG. Check before creating anything."""
    req = urllib.request.Request(url, method="GET", headers={"Range": "bytes=0-2"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                head = r.read(3)
                if head[:2] != b"\xff\xd8":
                    raise PublishError(f"{url} is not a JPEG")
                return
        except urllib.error.HTTPError as err:
            if err.code < 500:
                raise PublishError(f"{url} returned HTTP {err.code}. Is the repo public and pushed?")
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(5 * (attempt + 1))
    raise PublishError(f"{url} could not be fetched")


# ---------- publishing ----------

def account():
    me = api("GET", "me", fields="user_id,username,account_type")
    if me.get("account_type") not in ("BUSINESS", "MEDIA_CREATOR", "CREATOR"):
        raise PublishError(f"@{me.get('username')} is a {me.get('account_type')} account; "
                           "switch to Creator or Business in the Instagram app")
    return me


def already_posted(ig_id, caption):
    """Self-heal: find a post with this caption published by an earlier, interrupted run."""
    key = caption.strip()[:80]
    for m in api("GET", f"{ig_id}/media", fields="id,caption,permalink", limit=15).get("data", []):
        if (m.get("caption") or "").strip()[:80] == key:
            return m
    return None


def wait_ready(container_id):
    deadline = time.time() + CONTAINER_WAIT_SECS
    while True:
        code = api("GET", container_id, fields="status_code").get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise PublishError(f"container {container_id} status {code}")
        if time.time() > deadline:
            raise PublishError(f"container {container_id} still {code} after {CONTAINER_WAIT_SECS}s")
        time.sleep(10)


def publish(post, me, base):
    ig_id = me["user_id"]
    urls = [f"{base}/{f}" for f in post["images"]]
    for u in urls:
        check_image(u)
    children = []
    for u in urls:
        c = api("POST", f"{ig_id}/media", image_url=u, is_carousel_item="true")["id"]
        children.append(c)
    for c in children:
        wait_ready(c)
    carousel = api("POST", f"{ig_id}/media", media_type="CAROUSEL", children=",".join(children),
                   caption=post["caption"])["id"]
    wait_ready(carousel)
    media_id = api("POST", f"{ig_id}/media_publish", creation_id=carousel)["id"]
    link = api("GET", media_id, fields="permalink").get("permalink")
    return media_id, link


def due_posts(sched, now):
    return [p for p in sched["posts"] if p["status"] == "pending" and parse_when(p["when"]) <= now]


def cmd_run(args):
    sched = load_schedule()
    now = datetime.now(timezone.utc)
    due = due_posts(sched, now)
    if not due:
        nxt = min((p for p in sched["posts"] if p["status"] == "pending"),
                  key=lambda p: parse_when(p["when"]), default=None)
        print("nothing due" + (f"; next: {nxt['slug']} at {nxt['when']}" if nxt else "; schedule finished"))
        return 0
    post = min(due, key=lambda p: parse_when(p["when"]))  # one per run
    late = (now - parse_when(post["when"])).total_seconds() / 3600
    if late > MAX_LATE_HOURS:
        post["status"] = "missed"
        post["last_error"] = f"{late:.1f}h late (limit {MAX_LATE_HOURS}h). Run: ig_publish.py shift --start <date>"
        save_schedule(sched)
        raise PublishError(f"{post['slug']} missed its slot: {post['last_error']}")
    if args.dry_run:
        print(f"would publish {post['slug']} ({len(post['images'])} slides), due {post['when']}")
        return 0
    me = account()
    found = already_posted(me["user_id"], post["caption"])
    if found:
        post.update(status="published", media_id=found["id"], permalink=found.get("permalink"), last_error=None)
        save_schedule(sched)
        print(f"{post['slug']} was already live, marked published: {found.get('permalink')}")
        return 0
    try:
        media_id, link = publish(post, me, image_base())
    except PublishError as err:
        # Stays "pending" so the next run (15 min later) retries; after MAX_ATTEMPTS it stops and waits for you.
        post["attempts"] = post.get("attempts", 0) + 1
        post["last_error"] = str(err)[:300]
        if post["attempts"] >= MAX_ATTEMPTS:
            post["status"] = "failed"
        save_schedule(sched)
        raise
    post.update(status="published", media_id=media_id, permalink=link, last_error=None)
    save_schedule(sched)
    print(f"published {post['slug']}: {link}")
    return 0


def cmd_check(_args):
    me = account()
    print(f"account: @{me['username']} ({me['account_type']})")
    q = api("GET", f"{me['user_id']}/content_publishing_limit", fields="quota_usage,config").get("data", [{}])[0]
    print(f"quota used in last 24h: {q.get('quota_usage')} of {q.get('config', {}).get('quota_total')}")
    base = image_base()
    sched = load_schedule()
    n = 0
    for p in sched["posts"]:
        if p["status"] == "published":
            continue
        for f in p["images"]:
            check_image(f"{base}/{f}")
            n += 1
    print(f"all {n} unpublished images reachable as JPEG")
    return 0


def cmd_refresh(_args):
    r = http("GET", "https://graph.instagram.com/refresh_access_token",
             {"grant_type": "ig_refresh_token", "access_token": token()})
    days = int(r.get("expires_in", 0)) // 86400
    if days < 1:
        raise PublishError(f"refresh returned no lifetime: {r}")
    out = os.environ.get("NEW_TOKEN_FILE")
    if out:  # the workflow saves it back to the repo secret; never printed in logs
        Path(out).write_text(r["access_token"])
    print(f"token refreshed, valid for {days} more days")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--start")
    p.add_argument("--time", default="19:00")
    s = sub.add_parser("shift")
    s.add_argument("--start", required=True)
    s.add_argument("--time", default="19:00")
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true")
    sub.add_parser("check")
    sub.add_parser("status")
    sub.add_parser("refresh-token")
    args = ap.parse_args()
    fn = {"prepare": cmd_prepare, "shift": cmd_shift, "run": cmd_run, "check": cmd_check,
          "status": cmd_status, "refresh-token": cmd_refresh}[args.cmd]
    return fn(args) or 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PublishError as err:
        sys.exit(f"ig_publish: {err}")
