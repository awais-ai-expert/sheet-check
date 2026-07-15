#!/usr/bin/env python3
"""
Watches a public "anyone with the link can view" Google Sheet for changes
and posts a Discord webhook notification describing exactly what changed:
the cell, its column header, and the full old/new row for context.

How it works
------------
1. Downloads the sheet as CSV (no API key needed, since it's link-viewable).
2. Compares it to the snapshot saved from the previous run (snapshot.csv).
3. Builds a Discord message describing added/removed/changed rows.
4. Saves the new snapshot so the next run has something to diff against.

This script is meant to be run on a schedule (e.g. by GitHub Actions).
It is stateless except for snapshot.csv, which must persist between runs
(the accompanying GitHub Actions workflow commits it back to the repo).
"""

import csv
import io
import os
import sys
import requests

# ---------------------------------------------------------------------------
# CONFIG — fill these in (or set as environment variables / GitHub secrets)
# ---------------------------------------------------------------------------

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "PUT_YOUR_SPREADSHEET_ID_HERE")
GID = os.environ.get("SHEET_GID", "0")  # the tab's gid, "0" = first tab
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

SNAPSHOT_FILE = "snapshot.csv"
MAX_ROWS_REPORTED = 10  # cap how many changed rows we detail in one message

CSV_URL = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/export?format=csv&gid={GID}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def col_letter(index: int) -> str:
    """0-based column index -> spreadsheet-style letter (0 -> A, 1 -> B, ...)."""
    letters = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def fetch_current_rows() -> list[list[str]]:
    resp = requests.get(CSV_URL, timeout=30)
    resp.raise_for_status()
    # Google sometimes serves an HTML login/error page instead of CSV if the
    # link isn't actually public — catch that early with a clear error.
    if resp.text.lstrip().startswith("<"):
        raise RuntimeError(
            "Got HTML instead of CSV — check that the sheet is shared as "
            "'Anyone with the link can view' and that SPREADSHEET_ID/GID are correct."
        )
    reader = csv.reader(io.StringIO(resp.text))
    return list(reader)


def load_previous_rows() -> list[list[str]] | None:
    if not os.path.exists(SNAPSHOT_FILE):
        return None
    with open(SNAPSHOT_FILE, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def save_current_rows(rows: list[list[str]]) -> None:
    with open(SNAPSHOT_FILE, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


def diff_rows(old: list[list[str]], new: list[list[str]]) -> list[dict]:
    """Return a list of change descriptions, one per affected row."""
    header = new[0] if new else []
    changes = []
    max_len = max(len(old), len(new))

    for i in range(1, max_len):  # skip header row (index 0)
        old_row = old[i] if i < len(old) else None
        new_row = new[i] if i < len(new) else None

        if old_row == new_row:
            continue

        if old_row is None:
            changes.append({
                "type": "added",
                "row_number": i + 1,
                "new_row": new_row,
                "header": header,
            })
        elif new_row is None:
            changes.append({
                "type": "removed",
                "row_number": i + 1,
                "old_row": old_row,
                "header": header,
            })
        else:
            changed_cols = []
            for c in range(max(len(old_row), len(new_row))):
                old_val = old_row[c] if c < len(old_row) else ""
                new_val = new_row[c] if c < len(new_row) else ""
                if old_val != new_val:
                    col_name = header[c] if c < len(header) else f"Column {col_letter(c)}"
                    changed_cols.append({
                        "cell": f"{col_letter(c)}{i + 1}",
                        "column_name": col_name,
                        "old_value": old_val,
                        "new_value": new_val,
                    })
            changes.append({
                "type": "modified",
                "row_number": i + 1,
                "old_row": old_row,
                "new_row": new_row,
                "header": header,
                "changed_cols": changed_cols,
            })

    return changes


def format_row(header: list[str], row: list[str]) -> str:
    parts = []
    for i, val in enumerate(row):
        name = header[i] if i < len(header) else col_letter(i)
        parts.append(f"**{name}**: {val or '_(empty)_'}")
    return " | ".join(parts) if parts else "_(empty row)_"


def build_discord_payload(changes: list[dict]) -> dict:
    embeds = []
    for change in changes[:MAX_ROWS_REPORTED]:
        header = change["header"]

        if change["type"] == "modified":
            cell_summary = ", ".join(
                f"{c['column_name']} ({c['cell']}): `{c['old_value']}` -> `{c['new_value']}`"
                for c in change["changed_cols"]
            )
            embeds.append({
                "title": f"Row {change['row_number']} changed",
                "color": 0xF1C40F,
                "fields": [
                    {"name": "Changed cells", "value": cell_summary[:1024] or "—", "inline": False},
                    {"name": "Full row (before)", "value": format_row(header, change["old_row"])[:1024], "inline": False},
                    {"name": "Full row (after)", "value": format_row(header, change["new_row"])[:1024], "inline": False},
                ],
            })
        elif change["type"] == "added":
            embeds.append({
                "title": f"Row {change['row_number']} added",
                "color": 0x2ECC71,
                "fields": [
                    {"name": "New row", "value": format_row(header, change["new_row"])[:1024], "inline": False},
                ],
            })
        else:  # removed
            embeds.append({
                "title": f"Row {change['row_number']} removed",
                "color": 0xE74C3C,
                "fields": [
                    {"name": "Removed row", "value": format_row(header, change["old_row"])[:1024], "inline": False},
                ],
            })

    content = f"**Sheet changed** — {len(changes)} row(s) affected"
    if len(changes) > MAX_ROWS_REPORTED:
        content += f" (showing first {MAX_ROWS_REPORTED})"

    return {"content": content, "embeds": embeds}


def send_discord_notification(payload: dict) -> None:
    if not DISCORD_WEBHOOK_URL:
        print("No DISCORD_WEBHOOK_URL set — skipping notification. Payload was:")
        print(payload)
        return
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    current_rows = fetch_current_rows()
    previous_rows = load_previous_rows()

    if previous_rows is None:
        print("No previous snapshot found — saving baseline, no notification sent.")
        save_current_rows(current_rows)
        return 0

    changes = diff_rows(previous_rows, current_rows)

    if not changes:
        print("No changes detected.")
        return 0

    print(f"{len(changes)} row(s) changed — sending Discord notification.")
    payload = build_discord_payload(changes)
    send_discord_notification(payload)

    save_current_rows(current_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
