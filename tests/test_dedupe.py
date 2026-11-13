"""
Tests for discogs_dedupe.py. Run from the repo root:
    python3 -m unittest discover tests -v
They use a fake local Discogs server and pass in any time zone.
"""
import csv
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = ROOT / "discogs_dedupe.py"
sys.path.insert(0, str(ROOT))

import discogs_dedupe as dd  # noqa: E402
from fake_discogs import TOKEN, FakeDiscogs, instance  # noqa: E402

EDT = timezone(timedelta(hours=-4))
EST = timezone(timedelta(hours=-5))
OLD = "2025-03-01T10:00:00-05:00"


def iso(dt):
    return dt.isoformat()


def local_minute(dt):
    """The 'YYYY-MM-DD HH:MM' string a user in this machine's time zone would type for dt."""
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def scenario():
    """
    An accidental "Add all to collection" on Oct 24 2026 at about 2:02-2:03 pm EDT,
    surrounded by things that were added on purpose.
    """
    coll, iid = [], 1000
    click = datetime(2026, 10, 24, 14, 2, 0, tzinfo=EDT)
    # 110 purchases already in the collection, re-added by the click (enough for 3 pages)
    for n in range(110):
        coll.append(instance(5000 + n, iid, 1, OLD, f"Artist{n}", f"Album{n}")); iid += 1
        coll.append(instance(5000 + n, iid, 1, iso(click + timedelta(seconds=n)), f"Artist{n}", f"Album{n}")); iid += 1
    # 3 purchases that weren't in the collection any more (sold on), added by the click
    for n in range(3):
        coll.append(instance(7000 + n, iid, 1, iso(click + timedelta(seconds=100)), f"SoldOn{n}", f"Flipped{n}")); iid += 1
    # owned twice on purpose, re-added once by the click
    for d in (OLD, "2025-06-01T10:00:00-04:00", iso(click + timedelta(seconds=30))):
        coll.append(instance(9001, iid, 2, d, "TwoCopies", "Owned Twice")); iid += 1
    # added on purpose the same morning
    coll.append(instance(9004, iid, 1, iso(datetime(2026, 10, 24, 10, 0, tzinfo=EDT)), "Legit", "Same Morning")); iid += 1
    # bought after the click
    coll.append(instance(9005, iid, 1, iso(datetime(2026, 11, 3, 18, 0, tzinfo=EST)), "NewBuy", "November Record")); iid += 1
    # second copy bought on purpose after the click
    coll.append(instance(9006, iid, 1, OLD, "BoughtAgain", "Second Copy")); iid += 1
    coll.append(instance(9006, iid, 1, iso(datetime(2026, 11, 5, 18, 0, tzinfo=EST)), "BoughtAgain", "Second Copy")); iid += 1
    # an ordinary single copy
    coll.append(instance(9003, iid, 1, OLD, "Single", "Just One")); iid += 1
    return coll, click


class PlanTests(unittest.TestCase):
    """Pure logic, no server."""

    def setUp(self):
        self.coll, self.click = scenario()
        self.since = self.click - timedelta(minutes=2)
        self.until = self.click + timedelta(minutes=3)

    def ids(self, rows):
        return sorted(r["id"] for r in rows)

    def test_dedupe_in_window(self):
        remove, _, new_only, _ = dd.plan_removals(self.coll, self.since, self.until)
        self.assertEqual(len(remove), 111)  # 110 re-adds + the TwoCopies re-add
        self.assertNotIn(9006, self.ids(remove))  # November second copy is outside the window
        self.assertEqual(self.ids(new_only), [7000, 7001, 7002])

    def test_undo_in_window(self):
        remove, _, _, gone = dd.plan_removals(self.coll, self.since, self.until, undo=True)
        self.assertEqual(len(remove), 114)
        self.assertEqual(self.ids(gone), [7000, 7001, 7002])
        for keep in (9004, 9005, 9006, 9003):
            self.assertNotIn(keep, self.ids(remove))

    def test_open_ended_window_catches_later_second_copy(self):
        remove, _, _, _ = dd.plan_removals(self.coll, self.since)
        self.assertIn(9006, self.ids(remove))

    def test_no_window_keeps_earliest_copy_of_everything(self):
        remove, by_release, _, _ = dd.plan_removals(self.coll)
        self.assertEqual(len(self.coll) - len(remove), len(by_release))
        kept = {c["instance_id"] for c in self.coll} - {r["instance_id"] for r in remove}
        for copies in by_release.values():
            earliest = min(copies, key=lambda c: (dd.added(c), c["instance_id"]))
            self.assertIn(earliest["instance_id"], kept)

    def test_parse_time_until_is_inclusive(self):
        self.assertEqual(dd.parse_time("2026-10-24"), datetime(2026, 10, 24).astimezone())
        self.assertEqual(dd.parse_time("2026-10-24", end=True), datetime(2026, 10, 25).astimezone())
        self.assertEqual(dd.parse_time("2026-10-24 14:02", end=True), datetime(2026, 10, 24, 14, 3).astimezone())


class EndToEndTests(unittest.TestCase):
    """Runs the real script against the fake server."""

    def setUp(self):
        self.coll, self.click = scenario()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_script(self, fake, *args):
        env = dict(os.environ, DISCOGS_API=fake.url, DISCOGS_TOKEN=TOKEN, DISCOGS_DELETE_PAUSE="0",
                   NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.tmp.name, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)

    def csv_rows(self, prefix):
        files = sorted(Path(self.tmp.name).glob(prefix + "*.csv"))
        self.assertTrue(files, f"no {prefix}*.csv written")
        with open(files[-1], encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    def window(self, start_min, end_min):
        return ["--since", local_minute(self.click + timedelta(minutes=start_min)),
                "--until", local_minute(self.click + timedelta(minutes=end_min))]

    def test_dry_run_changes_nothing(self):
        with FakeDiscogs(self.coll) as fake:
            out = self.run_script(fake, *self.window(-2, 3))
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(fake.deleted, [])
        self.assertIn("Plan: remove 111 copies", out.stdout)
        self.assertIn("Busiest minutes", out.stdout)
        self.assertIn("page 3/3", out.stdout)
        self.assertEqual(len(self.csv_rows("discogs_dedupe_plan_")), 111)

    def test_dedupe_apply(self):
        with FakeDiscogs(self.coll) as fake:
            out = self.run_script(fake, *self.window(-2, 3), "--apply", "--yes")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(len(fake.deleted), 111)
            live = fake.live()
        # every release still has exactly the copies it had before the click
        by_release = {}
        for c in live:
            by_release.setdefault(c["id"], []).append(c)
        self.assertEqual(len(by_release[9001]), 2)
        self.assertEqual(len(by_release[9006]), 2)
        for rid in (7000, 7001, 7002, 9003, 9004, 9005):
            self.assertEqual(len(by_release[rid]), 1)
        self.assertEqual(len(self.csv_rows("discogs_dedupe_removed_")), 111)

    def test_undo_apply_restores_pre_click_state(self):
        before_click = [c for c in self.coll
                        if not (self.click <= dd.added(c) < self.click + timedelta(minutes=2))]
        with FakeDiscogs(self.coll) as fake:
            out = self.run_script(fake, *self.window(0, 1), "--undo", "--apply", "--yes")
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(len(fake.deleted), 114)
            self.assertEqual(sorted(c["instance_id"] for c in fake.live()),
                             sorted(c["instance_id"] for c in before_click))

    def test_apply_without_confirmation_cancels(self):
        with FakeDiscogs(self.coll) as fake:
            out = self.run_script(fake, *self.window(-2, 3), "--apply")  # nobody types "yes"
            self.assertEqual(fake.deleted, [])
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Cancelled, nothing changed.", out.stdout)

    def test_bad_arguments(self):
        with FakeDiscogs(self.coll) as fake:
            self.assertIn("need --since", self.run_script(fake, "--undo").stderr)
            self.assertIn("before --since", self.run_script(fake, "--since", "2026-10-25",
                                                            "--until", "2026-10-24").stderr)
            self.assertIn("Could not read", self.run_script(fake, "--since", "yesterday").stderr)
            self.assertEqual(fake.deleted, [])


if __name__ == "__main__":
    unittest.main()
