#!/usr/bin/env python3
"""
discogs_dedupe.py - remove duplicate copies from your Discogs collection,
or undo a batch of accidental additions (like "Add all to collection").

By default this is a DRY RUN: it lists what it would remove, saves that list
to a CSV, and changes nothing. Add --apply to actually remove them.

--since / --until limit it to copies added in that window (local time,
both ends inclusive). A plain date covers the whole day.
Anything added outside the window is never removed.

Two modes:
  Dedupe (default): for each release, copies added in the window are removed
    if you already had a copy from before the window. If a release ONLY has
    copies from the window, the earliest one is kept.
  Undo (--undo): remove EVERY copy added in the window, duplicate or not.
    Puts things back the way they were before the window started.

Examples:
  python3 discogs_dedupe.py --since 2026-10-24 --until 2026-10-24
      dry run, dedupe copies added on Oct 24 (also shows the busiest minutes,
      so you can find the exact time of a bulk add)
  python3 discogs_dedupe.py --since "2026-10-24 14:02" --until "2026-10-24 14:03" --undo
      dry run, undo everything added from 2:02 through 2:03 pm
  ...then add --apply to either one to actually do it (asks you to confirm first).

Token: discogs.com > Settings > Developers > "Generate new token".
Put it in the DISCOGS_TOKEN environment variable, or the script will ask for it.
No extra packages needed (standard library only).
"""
import argparse
import csv
import getpass
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta

API = os.environ.get("DISCOGS_API", "https://api.discogs.com")
USER_AGENT = "discogs-dedupe/1.2 +https://github.com/TostadoGotCooked/discogs-dedupe"
# seconds between removals; Discogs allows about 60 requests a minute
DELETE_PAUSE = float(os.environ.get("DISCOGS_DELETE_PAUSE", "1.1"))


def call(method, path, token, tries=6):
    """Make one API request, waiting out rate limits. Returns (status, json_or_None)."""
    url = API + path
    for attempt in range(tries):
        req = urllib.request.Request(url, method=method, headers={
            "Authorization": f"Discogs token={token}",
            "User-Agent": USER_AGENT,
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
                remaining = resp.headers.get("X-Discogs-Ratelimit-Remaining")
                if remaining is not None and remaining.isdigit() and int(remaining) <= 2:
                    print("  (near the rate limit, pausing 60s)")
                    time.sleep(60)
                return resp.status, (json.loads(body) if body else None)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print("  (rate limited, waiting 60s)")
                time.sleep(60)
                continue
            if e.code in (500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            detail = e.read().decode("utf-8", "replace")[:300]
            raise SystemExit(f"Discogs API error {e.code} on {method} {path}: {detail}")
        except urllib.error.URLError as e:
            if attempt < tries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            raise SystemExit(f"Network error talking to Discogs: {e.reason}")
    raise SystemExit(f"Gave up on {method} {path} after {tries} tries")


def parse_time(text, end=False):
    """
    'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' in local time -> timezone-aware datetime.
    With end=True, returns the exclusive end of that day/minute (so --until is inclusive).
    """
    for fmt, step in (("%Y-%m-%d %H:%M", timedelta(minutes=1)),
                      ("%Y-%m-%dT%H:%M", timedelta(minutes=1)),
                      ("%Y-%m-%d", timedelta(days=1))):
        try:
            t = datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
        return (t + step if end else t).astimezone()
    raise SystemExit(f'Could not read "{text}". Use YYYY-MM-DD or "YYYY-MM-DD HH:MM".')


def added(item):
    """date_added as a timezone-aware datetime."""
    return datetime.fromisoformat(item["date_added"].replace("Z", "+00:00"))


def in_window(item, since, until):
    t = added(item)
    return t >= since and (until is None or t < until)


def label(item):
    info = item.get("basic_information", {})
    artists = ", ".join(a.get("name", "") for a in info.get("artists", [])) or "?"
    return artists, info.get("title", "?")


def show(item):
    artist, title = label(item)
    when = added(item).astimezone().strftime("%Y-%m-%d %H:%M")
    return f"{artist} - {title}  (release {item['id']}, copy {item['instance_id']}, added {when})"


def fetch_collection(username, token):
    items, page, pages = [], 1, 1
    while page <= pages:
        _, data = call("GET", f"/users/{username}/collection/folders/0/releases"
                              f"?per_page=100&page={page}&sort=added&sort_order=asc", token)
        pages = data["pagination"]["pages"]
        items.extend(data["releases"])
        print(f"  page {page}/{pages} ({len(items)} copies so far)")
        page += 1
    return items


def plan_removals(items, since=None, until=None, undo=False):
    """
    Returns (to_remove, by_release, new_only, gone).
      new_only: releases with copies in the window but none from before it
      gone:     releases whose every copy is in the window (undo removes them entirely)
    """
    by_release = defaultdict(list)
    for it in items:
        by_release[it["id"]].append(it)

    remove, new_only, gone = [], [], []
    for copies in by_release.values():
        copies.sort(key=lambda c: (added(c), c["instance_id"]))
        if since is None:
            remove.extend(copies[1:])
            continue
        before = [c for c in copies if added(c) < since]
        window = [c for c in copies if in_window(c, since, until)]
        if not window:
            continue
        if not before:
            new_only.append(window[0])
            if len(window) == len(copies):
                gone.append(window[0])
        if undo:
            remove.extend(window)
        else:
            remove.extend(window if before else window[1:])

    remove.sort(key=lambda c: (label(c), c["instance_id"]))
    new_only.sort(key=label)
    gone.sort(key=label)
    return remove, by_release, new_only, gone


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # -sig so Excel reads accents right
        w = csv.writer(f)
        w.writerow(["release_id", "instance_id", "folder_id", "date_added", "artist", "title"])
        for r in rows:
            artist, title = label(r)
            w.writerow([r["id"], r["instance_id"], r["folder_id"], r["date_added"], artist, title])


def main():
    ap = argparse.ArgumentParser(description="Remove duplicate copies from your Discogs collection.")
    ap.add_argument("--since", metavar='"YYYY-MM-DD [HH:MM]"',
                    help="only touch copies added on/after this local date or time")
    ap.add_argument("--until", metavar='"YYYY-MM-DD [HH:MM]"',
                    help="...and on/before this local date or time (inclusive)")
    ap.add_argument("--undo", action="store_true",
                    help="remove EVERY copy added in the window, not just duplicates")
    ap.add_argument("--apply", action="store_true", help="actually remove (default is a dry run)")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt with --apply")
    args = ap.parse_args()

    if (args.undo or args.until) and not args.since:
        raise SystemExit("--undo and --until need --since too, "
                         "e.g. --since 2026-10-24 --until 2026-10-24")
    since = parse_time(args.since) if args.since else None
    until = parse_time(args.until, end=True) if args.until else None
    if since and until and until <= since:
        raise SystemExit("--until is before --since.")
    mode = "undo" if args.undo else "dedupe"
    window_desc = (f"{args.since} through {args.until}" if until else f"{args.since} onward") if since else ""

    token = os.environ.get("DISCOGS_TOKEN") or getpass.getpass("Discogs personal access token: ").strip()
    if not token:
        raise SystemExit("No token given.")

    _, me = call("GET", "/oauth/identity", token)
    username = me["username"]
    print(f"Logged in as {username}. Reading collection...")
    items = fetch_collection(username, token)

    remove, by_release, new_only, gone = plan_removals(items, since, until, args.undo)
    dup_releases = sum(1 for c in by_release.values() if len(c) > 1)
    print(f"\n{len(items)} copies total, {len(by_release)} distinct releases, "
          f"{dup_releases} releases with more than one copy.")

    if since:
        in_win = [it for it in items if in_window(it, since, until)]
        print(f"{len(in_win)} copies were added {window_desc}.")
        per_minute = Counter(added(c).astimezone().strftime("%Y-%m-%d %H:%M %Z") for c in in_win)
        if per_minute:
            print("Busiest minutes in that window (a bulk add shows up as a big spike):")
            for minute, n in per_minute.most_common(5):
                print(f"  {minute}   {n} copies")

    print(f"\nMode: {mode}. Plan: remove {len(remove)} copies, leaving {len(items) - len(remove)}.\n")
    for r in remove[:25]:
        print(f"  - {show(r)}")
    if len(remove) > 25:
        print(f"  ...and {len(remove) - 25} more")

    if args.undo and gone:
        print(f"\n{len(gone)} of these releases have no other copy, so they'll leave your collection completely:")
        listing = gone
    elif not args.undo and new_only:
        print(f"\nNote: {len(new_only)} releases in that window weren't in your collection before it. "
              f"Dedupe keeps one copy of each; --undo would remove them:")
        listing = new_only
    else:
        listing = []
    for r in listing[:15]:
        print(f"  * {show(r)}")
    if len(listing) > 15:
        print(f"  ...and {len(listing) - 15} more")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    plan_file = f"discogs_{mode}_{'removed' if args.apply else 'plan'}_{stamp}.csv"
    write_csv(plan_file, remove)
    print(f"\nFull list of copies to remove saved to {plan_file}")

    if not remove:
        print("Nothing to remove.")
        return
    if not args.apply:
        print("\nDry run only, nothing was changed. Re-run with --apply to remove these.")
        return

    if not args.yes:
        try:
            answer = input(f"\nRemove {len(remove)} copies from {username}'s collection? Type yes: ")
        except EOFError:
            answer = ""
        if answer.strip().lower() != "yes":
            print("Cancelled, nothing changed.")
            return

    done = 0
    for r in remove:
        call("DELETE", f"/users/{username}/collection/folders/{r['folder_id']}"
                       f"/releases/{r['id']}/instances/{r['instance_id']}", token)
        done += 1
        if done % 25 == 0 or done == len(remove):
            print(f"  removed {done}/{len(remove)}")
        time.sleep(DELETE_PAUSE)
    print(f"\nDone. Removed {done} copies. The list is in {plan_file} if you need to re-add any.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nStopped. Anything already removed stays removed; re-run to finish.")
