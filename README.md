# discogs-dedupe

Remove duplicate copies from your Discogs collection, or undo an accidental bulk add.

Discogs has no undo. If you hit **"Add all to collection"** on your Purchases page, import the same CSV twice, or otherwise end up with two of everything, the website makes you remove copies one by one. This script does it in one go through the Discogs API.

- **Dry run by default.** It shows what it would remove and changes nothing until you add `--apply`.
- **Works only inside a time window you choose.** Nothing added before `--since` or after `--until` is ever touched.
- **Finds the exact minute for you.** A bulk add shows up as a big spike in the "Busiest minutes" list.
- **Confirms before removing**, and saves a CSV of every removed copy so you can re-add anything by hand.
- Single file, Python 3.8+, no packages to install.

## Setup

1. Install Python 3.8 or newer if you don't have it (Windows: `python --version` in PowerShell opens the Microsoft Store if it's missing).
2. Download `discogs_dedupe.py`.
3. Get a personal access token: discogs.com → **Settings → Developers → Generate new token**.

The script asks for the token when it runs, and doesn't show it while you type or paste. You can also set it ahead of time:

```bash
export DISCOGS_TOKEN="your-token"           # Linux / macOS
```
```powershell
$env:DISCOGS_TOKEN = "your-token"           # Windows PowerShell
```

On Windows, use `python` (or `py`) where the examples say `python3`.

## Usage

### 1. Dry run for the day it happened

```bash
python3 discogs_dedupe.py --since 2026-10-24 --until 2026-10-24
```

Output includes something like:

```
Busiest minutes in that window (a bulk add shows up as a big spike):
  2026-10-24 14:02 EDT   281 copies
  2026-10-24 14:03 EDT   96 copies
  2026-10-24 10:00 EDT   1 copies
```

and the full list of copies it would remove is saved to a CSV.

### 2. Pick a mode and apply

**Dedupe** (default): within the window, remove a copy if you already had that release from before the window. If a release *only* has copies from inside the window, the earliest one is kept.

```bash
python3 discogs_dedupe.py --since 2026-10-24 --until 2026-10-24 --apply
```

**Undo** (`--undo`): remove *every* copy added in the window, duplicate or not. This puts the collection back the way it was. Use it when the bulk add also brought in things you no longer own (sold, traded). Narrow the window to the spike so nothing you added on purpose that day gets caught:

```bash
python3 discogs_dedupe.py --since "2026-10-24 14:02" --until "2026-10-24 14:03" --undo
python3 discogs_dedupe.py --since "2026-10-24 14:02" --until "2026-10-24 14:03" --undo --apply
```

Both `--apply` runs ask you to type `yes` first. Add `--yes` to skip that.

### Options

| Option | Meaning |
|---|---|
| `--since "YYYY-MM-DD [HH:MM]"` | Only touch copies added on/after this time |
| `--until "YYYY-MM-DD [HH:MM]"` | ...and on/before this time (inclusive; a plain date means the whole day) |
| `--undo` | Remove every copy in the window, not just duplicates |
| `--apply` | Actually remove (otherwise it's a dry run) |
| `--yes` | Skip the confirmation prompt |

Times are in your computer's local time zone, the same zone the "Busiest minutes" list uses.

> **Always give it a window.** With no `--since`, the script treats *every* release you have more than one copy of as a mistake, including second copies you own on purpose.

## Good to know

- Removal runs at about one copy per second to stay under Discogs' API rate limit (about 60 requests a minute). If you stop it partway, run it again and it carries on.
- Every run writes `discogs_<mode>_<plan|removed>_<timestamp>.csv` with release IDs, so anything removed by mistake can be found and re-added from its release page.
- Removals go through the API immediately. Refresh your collection page to see them.

## Tests

The tests run against a small fake Discogs API on your own machine and never contact Discogs.

```bash
python3 -m unittest discover tests -v
```

## License

MIT. Not affiliated with or endorsed by Discogs.
