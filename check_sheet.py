#!/usr/bin/env python3
"""
Watches ALL tabs in a public Google Sheets workbook (CSV export by gid)
and posts a Discord webhook notification when anything changes.

What it does
------------
- Downloads each tab as CSV using its gid
- Compares against the previous snapshot for that tab
- Detects added / removed / modified rows
- Sends one Discord message with all changes
- Saves fresh snapshots into ./snapshots/

Assumptions
-----------
- The sheet exports work in incognito without login
- The first row is a header row
- The first column is a good row key (SKU / model / product code)
"""

from __future__ import annotations

import csv
import io
import json
import os
import sys
from typing import Any

import requests

# ---------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------

SPREADSHEET_ID = "1YGdn3FfglAeS55nV0J8gdOz73aKSLYOG"

SHEETS: dict[str, int] = {
    "TIS": 1169399085,
    "SEI": 797692450,
    "HB": 1636742515,
    "EA": 1096959905,
    "CIT": 408498169,
    "PP": 694419106,
    "TH": 1244444846,
    "MK": 1014432734,
    "DZ": 1614488420,
    "CK": 1471969372,
    "AE": 1134283471,
    "CL": 994677874,
    "BE": 693571795,
    "PL": 1296006560,
    "CP": 645231443,
    "DW": 758046971,
}

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
SNAPSHOT_DIR = "snapshots"
MAX_CHANGES_REPORTED = 12

# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def csv_url_for_gid(gid: int) -> str:
    return f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/export?format=csv&gid={gid}"


def normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def col_letter(index: int) -> str:
    letters = ""
    index += 1
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def ensure_snapshot_dir() -> None:
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)


def snapshot_path(sheet_name: str) -> str:
    safe = "".join(ch for ch in sheet_name if ch.isalnum() or ch in ("-", "_")).strip()
    return os.path.join(SNAPSHOT_DIR, f"{safe}.json")


def fetch_sheet_rows(gid: int) -> list[list[str]]:
    url = csv_url_for_gid(gid)
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()

    text = resp.text.lstrip()
    if text.startswith("<"):
        raise RuntimeError(
            f"Got HTML instead of CSV for gid={gid}. "
            f"Check that the sheet export is public."
        )

    reader = csv.reader(io.StringIO(resp.text))
    rows = []
    for row in reader:
        cleaned = [normalize(v) for v in row]
        rows.append(cleaned)
    return rows


def load_previous_rows(sheet_name: str) -> list[list[str]] | None:
    path = snapshot_path(sheet_name)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_rows(sheet_name: str, rows: list[list[str]]) -> None:
    path = snapshot_path(sheet_name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


def format_row(header: list[str], row: list[str]) -> str:
    parts = []
    for i, val in enumerate(row):
        if val == "":
            continue
        name = header[i] if i < len(header) and header[i] else f"Column {col_letter(i)}"
        parts.append(f"**{name}**: {val}")
    return " | ".join(parts) if parts else "_(empty row)_"


def build_row_map(rows: list[list[str]]) -> tuple[list[str], dict[str, tuple[list[str], int]]]:
    header = rows[0] if rows else []
    data: dict[str, tuple[list[str], int]] = {}

    for row_number, row in enumerate(rows[1:], start=2):
        key = normalize(row[0]) if row else ""
        if not key:
            key = f"__row_{row_number}__"

        # Keep duplicates unique so they still show up in diffs.
        if key in data:
            key = f"{key}__dup_{row_number}"

        data[key] = (row, row_number)

    return header, data


def diff_rows(old_rows: list[list[str]], new_rows: list[list[str]], sheet_name: str) -> list[dict[str, Any]]:
    old_rows = old_rows or []
    new_rows = new_rows or []

    if not old_rows and not new_rows:
        return []

    old_header, old_map = build_row_map(old_rows)
    new_header, new_map = build_row_map(new_rows)
    header = new_header if new_header else old_header

    all_keys = sorted(set(old_map.keys()) | set(new_map.keys()))
    changes: list[dict[str, Any]] = []

    for key in all_keys:
        old_item = old_map.get(key)
        new_item = new_map.get(key)

        if old_item is None and new_item is not None:
            new_row, new_row_number = new_item
            changes.append({
                "type": "added",
                "sheet_name": sheet_name,
                "row_key": key,
                "row_number": new_row_number,
                "header": header,
                "new_row": new_row,
            })
            continue

        if new_item is None and old_item is not None:
            old_row, old_row_number = old_item
            changes.append({
                "type": "removed",
                "sheet_name": sheet_name,
                "row_key": key,
                "row_number": old_row_number,
                "header": header,
                "old_row": old_row,
            })
            continue

        old_row, _old_row_number = old_item
        new_row, new_row_number = new_item

        if old_row == new_row:
            continue

        changed_cols = []
        max_cols = max(len(old_row), len(new_row))

        for c in range(max_cols):
            old_val = old_row[c] if c < len(old_row) else ""
            new_val = new_row[c] if c < len(new_row) else ""

            if old_val != new_val:
                col_name = header[c] if c < len(header) and header[c] else f"Column {col_letter(c)}"
                changed_cols.append({
                    "cell": f"{col_letter(c)}{new_row_number}",
                    "column_name": col_name,
                    "old_value": old_val,
                    "new_value": new_val,
                })

        if changed_cols:
            changes.append({
                "type": "modified",
                "sheet_name": sheet_name,
                "row_key": key,
                "row_number": new_row_number,
                "header": header,
                "old_row": old_row,
                "new_row": new_row,
                "changed_cols": changed_cols,
            })

    return changes


def build_discord_payload(all_changes: list[dict[str, Any]]) -> dict[str, Any]:
    embeds = []

    for change in all_changes[:MAX_CHANGES_REPORTED]:
        sheet_name = change["sheet_name"]
        row_key = str(change.get("row_key", ""))
        row_number = change.get("row_number", "?")
        header = change["header"]

        if change["type"] == "modified":
            cell_summary = ", ".join(
                f"{c['column_name']} ({c['cell']}): `{c['old_value']}` -> `{c['new_value']}`"
                for c in change["changed_cols"]
            )
            embeds.append({
                "title": f"{sheet_name} • {row_key}",
                "description": f"Row {row_number} changed",
                "color": 0xF1C40F,
                "fields": [
                    {
                        "name": "Changed cells",
                        "value": cell_summary[:1024] or "—",
                        "inline": False,
                    },
                    {
                        "name": "Before",
                        "value": format_row(header, change["old_row"])[:1024],
                        "inline": False,
                    },
                    {
                        "name": "After",
                        "value": format_row(header, change["new_row"])[:1024],
                        "inline": False,
                    },
                ],
            })

        elif change["type"] == "added":
            embeds.append({
                "title": f"{sheet_name} • {row_key}",
                "description": f"Row {row_number} added",
                "color": 0x2ECC71,
                "fields": [
                    {
                        "name": "New row",
                        "value": format_row(header, change["new_row"])[:1024],
                        "inline": False,
                    },
                ],
            })

        elif change["type"] == "removed":
            embeds.append({
                "title": f"{sheet_name} • {row_key}",
                "description": f"Row {row_number} removed",
                "color": 0xE74C3C,
                "fields": [
                    {
                        "name": "Removed row",
                        "value": format_row(header, change["old_row"])[:1024],
                        "inline": False,
                    },
                ],
            })

    content = f"**Workbook changed** — {len(all_changes)} item(s) affected"
    if len(all_changes) > MAX_CHANGES_REPORTED:
        content += f" (showing first {MAX_CHANGES_REPORTED})"

    return {
        "content": content,
        "embeds": embeds,
    }


def send_discord(payload: dict[str, Any]) -> None:
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL is not set. Payload below:")
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
    resp.raise_for_status()


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> int:
    ensure_snapshot_dir()

    all_changes: list[dict[str, Any]] = []

    for sheet_name, gid in SHEETS.items():
        print(f"Checking {sheet_name} (gid={gid}) ...")
        current_rows = fetch_sheet_rows(gid)
        previous_rows = load_previous_rows(sheet_name)

        if previous_rows is None:
            print(f"  No snapshot yet for {sheet_name}. Saving baseline.")
            save_rows(sheet_name, current_rows)
            continue

        changes = diff_rows(previous_rows, current_rows, sheet_name)

        if changes:
            print(f"  {len(changes)} change(s) found.")
            all_changes.extend(changes)
        else:
            print("  No changes.")

        save_rows(sheet_name, current_rows)

    if not all_changes:
        print("No changes detected in any sheet.")
        return 0

    payload = build_discord_payload(all_changes)
    send_discord(payload)
    print(f"Sent Discord notification for {len(all_changes)} change(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
