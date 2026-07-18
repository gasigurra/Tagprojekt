"""
smhi_backfill.py
-----------------
Engångsskript som fyller weather_observations med SMHI:s historik (corrected-archive + latest-months).
UPPDATERAD: Klipper nu automatiskt bort all data som är äldre än 2 år för att spara utrymme.
"""

import argparse
import sqlite3
import time
from datetime import datetime, timedelta  # <-- UPPDATERAD: Lade till timedelta här

import requests

from smhi_fetcher import (
    DATABASE_PATH,
    PARAM_MAP,
    SMHI_BASE_URL,
    get_unique_smhi_stations,
    round_to_hour_iso,
)

REQUEST_TIMEOUT_ARCHIVE = 60
REQUEST_TIMEOUT_RECENT = 30
SLEEP_BETWEEN_REQUESTS = 0.3


def ensure_progress_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS backfill_progress (
            param_column TEXT NOT NULL,
            smhi_station_id TEXT NOT NULL,
            rows_written INTEGER,
            completed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (param_column, smhi_station_id)
        )
    """)


def already_done(cursor, col_name, smhi_id):
    cursor.execute(
        "SELECT 1 FROM backfill_progress WHERE param_column = ? AND smhi_station_id = ?",
        (col_name, str(smhi_id)),
    )
    return cursor.fetchone() is not None


def mark_done(cursor, col_name, smhi_id, rows_written):
    cursor.execute("""
        INSERT INTO backfill_progress (param_column, smhi_station_id, rows_written)
        VALUES (?, ?, ?)
        ON CONFLICT(param_column, smhi_station_id)
        DO UPDATE SET rows_written = excluded.rows_written, completed_at = CURRENT_TIMESTAMP
    """, (col_name, str(smhi_id), rows_written))


def parse_corrected_archive_csv(raw_bytes):
    text = None
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            text = raw_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return []

    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        if line.startswith("Datum") and "Tid (UTC)" in line:
            header_idx = i
            break
    if header_idx is None:
        return []

    rows = []
    for line in lines[header_idx + 1 :]:
        if not line.strip():
            continue
        parts = line.split(";")
        if len(parts) < 3:
            continue
        date_str, time_str, value_str = (
            parts[0].strip(),
            parts[1].strip(),
            parts[2].strip(),
        )
        if not value_str:
            continue
        try:
            value = float(value_str.replace(",", "."))
        except ValueError:
            continue
        try:
            dt = datetime.strptime(
                f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S"
            )
        except ValueError:
            continue
        dt_hour = dt.replace(minute=0, second=0, microsecond=0)
        rows.append((dt_hour.strftime("%Y-%m-%d %H:%M:%S"), value))
    return rows


def fetch_archive_rows(param_id, station_id):
    url = f"{SMHI_BASE_URL}/parameter/{param_id}/station/{station_id}/period/corrected-archive/data.csv"
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_ARCHIVE)
    except requests.RequestException as e:
        print(f"    ⚠️  Nätverksfel (arkiv) för station {station_id}: {e}")
        return []
    if response.status_code != 200:
        return []
    return parse_corrected_archive_csv(response.content)


def fetch_recent_rows(param_id, station_id):
    url = f"{SMHI_BASE_URL}/parameter/{param_id}/station/{station_id}/period/latest-months/data.json"
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_RECENT)
    except requests.RequestException as e:
        print(
            f"    ⚠️  Nätverksfel (senaste mån) för station {station_id}: {e}"
        )
        return []
    if response.status_code != 200:
        return []
    try:
        data = response.json()
    except ValueError:
        return []

    rows = []
    for entry in data.get("value", []):
        val = entry.get("value")
        if val in (None, ""):
            continue
        try:
            value = float(val)
            ts = round_to_hour_iso(entry["date"])
            rows.append((ts, value))
        except (TypeError, ValueError, KeyError):
            continue
    return rows


def run_backfill(limit_stations=None, force=False, dry_run=False):
    conn = sqlite3.connect(DATABASE_PATH, timeout=15)
    cursor = conn.cursor()
    ensure_progress_table(cursor)
    conn.commit()

    grand_total = 0

    # UPPDATERAD: Skapa gränsen för exakt 2 år (730 dagar) bakåt i tiden
    cutoff_str = (datetime.now() - timedelta(days=730)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    print(f"⏳ Tidsspärr aktiv: Sparar ENBART väderdata från {cutoff_str} och framåt.")

    for param_name, (col_name, param_id) in PARAM_MAP.items():
        stations = get_unique_smhi_stations(col_name)
        if limit_stations:
            stations = stations[:limit_stations]

        print(
            f"\n=== {param_name} ({len(stations)} unika SMHI-stationer) ==="
        )

        for idx, smhi_id in enumerate(stations, start=1):
            if not force and already_done(cursor, col_name, smhi_id):
                print(
                    f"  [{idx}/{len(stations)}] station {smhi_id}: redan klar, hoppar över "
                    f"(kör med --force för att göra om)"
                )
                continue

            cursor.execute(
                f"SELECT station_signature FROM stations_mapping WHERE {col_name} = ?",
                (smhi_id,),
            )
            trv_signatures = [row[0] for row in cursor.fetchall()]
            if not trv_signatures:
                continue

            archive_rows = fetch_archive_rows(param_id, smhi_id)
            time.sleep(SLEEP_BETWEEN_REQUESTS)
            recent_rows = fetch_recent_rows(param_id, smhi_id)
            time.sleep(SLEEP_BETWEEN_REQUESTS)

            all_rows = archive_rows + recent_rows

            # UPPDATERAD: Här klipper vi bort allt som är äldre än 2 år!
            all_rows = [row for row in all_rows if row[0] >= cutoff_str]

            if dry_run:
                print(
                    f"  [{idx}/{len(stations)}] station {smhi_id}: "
                    f"giltiga obs efter filtrering -> {len(all_rows)} st "
                    f"-> {len(trv_signatures)} trafikplatser (dry-run, skriver ej)"
                )
                continue

            rows_written = 0
            for ts, value in all_rows:
                for trv_sig in trv_signatures:
                    cursor.execute(
                        f"""
                        INSERT INTO weather_observations (station_signature, timestamp_hour, {param_name})
                        VALUES (?, ?, ?)
                        ON CONFLICT(station_signature, timestamp_hour)
                        DO UPDATE SET {param_name} = excluded.{param_name}
                    """,
                        (trv_sig, ts, value),
                    )
                    rows_written += 1

            mark_done(cursor, col_name, smhi_id, rows_written)
            conn.commit()
            grand_total += rows_written

            print(
                f"  [{idx}/{len(stations)}] station {smhi_id}: {len(all_rows)} observationer "
                f"-> {len(trv_signatures)} trafikplatser ({rows_written} rader skrivna)"
            )

    conn.close()

    if dry_run:
        print("\n✅ Dry-run klar. Inget har skrivits till databasen.")
    else:
        print(
            f"\n✅ Backfill klar. Totalt {grand_total} rader skrivna/uppdaterade i weather_observations."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fyller weather_observations med SMHI:s historik (de senaste 2 åren)."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Begränsa till N SMHI-stationer per parameter (bra för ett första testkörning).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Hämta om även stationer som redan är markerade som klara i backfill_progress.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Hämta och räkna rader, men skriv ingenting till databasen.",
    )
    args = parser.parse_args()

    run_backfill(
        limit_stations=args.limit, force=args.force, dry_run=args.dry_run
    )