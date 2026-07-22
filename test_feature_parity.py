"""
test_feature_parity.py
------------------------
Regressionstest: garanterar att de features som träningspipelinen
(data_preparation.py) räknar ut i BULK för hela datasetet är EXAKT samma
värden som app.py räknar ut LIVE för en enskild förfrågan, givet samma
underliggande data i databasen.

Det här är precis den typ av test som skulle ha fångat de tre
train/serving-skew-buggarna som fanns tidigare, INNAN de nådde produktion:
  1. previous_station_delay lästes alltid som 0 i appen (kolumnen
     skrevs aldrig av något insamlingsskript).
  2. join_hour (vilken timmes väder som slås upp) golvades i appen men
     avrundades till närmaste timme vid träning.
  3. traffic_density fick olika värde beroende på om det räknades under
     träning (closed='left') eller av update_traffic_density.py
     (center=True).

Kör med:  pytest test_feature_parity.py -v
eller:    python test_feature_parity.py
"""

import os
import sqlite3
import tempfile

import pandas as pd
import pytest

from database_setup import create_database
from data_preparation import transform_features
from feature_engineering import (
    get_join_hour,
    check_incident_type_live,
    compute_traffic_density_live,
    compute_previous_station_delay_live,
)


@pytest.fixture
def db_conn():
    """Skapar en tom, temporär databas med RIKTIGA schemat från
    database_setup.py (inte en handskriven kopia i testet) - så testet
    kan aldrig tyst hamna i otakt med det faktiska schemat."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)  # create_database ska skapa filen från grunden

    create_database(db_path=path, confirm_delete=False)
    conn = sqlite3.connect(path)
    yield conn
    conn.close()
    os.remove(path)


def _insert_train(conn, train_number, station, scheduled, actual, delay, canceled=0,
                   operator="SJ", train_type="Snabbtåg"):
    conn.execute(
        """
        INSERT INTO train_observations
        (train_number, station_signature, scheduled_arrival, actual_arrival,
         delay_minutes, canceled, operator, train_type, traffic_density)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        (train_number, station, scheduled, actual, delay, canceled, operator, train_type),
    )


def _insert_weather(conn, station, hour_str, temperature, wind_speed, precipitation, snow_depth):
    conn.execute(
        """
        INSERT INTO weather_observations
        (station_signature, timestamp_hour, temperature, wind_speed, precipitation, snow_depth)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (station, hour_str, temperature, wind_speed, precipitation, snow_depth),
    )


def _insert_incident(conn, incident_id, station, start, end, severity):
    conn.execute(
        """
        INSERT INTO track_works (incident_id, affected_station, start_time, end_time, severity_level)
        VALUES (?, ?, ?, ?, ?)
        """,
        (incident_id, station, start, end, severity),
    )


def test_feature_parity_between_training_and_live_app(db_conn):
    conn = db_conn

    # En resa: tåg 924 stannar i M kl 09:00 (redan 12 min sen), sen i Cst
    # kl 09:40 - det är Cst-raden vi jämför bulk vs. live för.
    _insert_train(conn, "924", "M", "2026-03-10T09:00:00", "2026-03-10T09:12:00", 12)
    _insert_train(conn, "924", "Cst", "2026-03-10T09:40:00", "2026-03-10T09:47:00", 7)

    # Ett tåg strax innan i Cst (inom 15 min) - ska räknas mot traffic_density.
    _insert_train(conn, "111", "Cst", "2026-03-10T09:35:00", "2026-03-10T09:35:00", 0)
    # Ett tåg för långt innan (utanför 15-minutersfönstret) - ska INTE räknas.
    _insert_train(conn, "222", "Cst", "2026-03-10T09:10:00", "2026-03-10T09:10:00", 0)

    # Väder sparat på hela timmen 09:00 (golvet av 09:40 är 09:00).
    _insert_weather(conn, "M", "2026-03-10 09:00:00", temperature=-3.0, wind_speed=3.0, precipitation=0.0, snow_depth=0.1)
    _insert_weather(conn, "Cst", "2026-03-10 09:00:00", temperature=-2.5, wind_speed=4.0, precipitation=0.0, snow_depth=0.1)
    # En "framtida" 10:00-mätning finns också - ska INTE användas för en
    # 09:40-ankomst. Om join_hour någonsin råkar avrunda uppåt igen
    # (round('h') istället för floor('h')) kommer det här testet plocka
    # upp 99.0 istället för -2.5 och slå fel.
    _insert_weather(conn, "Cst", "2026-03-10 10:00:00", temperature=99.0, wind_speed=99.0, precipitation=99.0, snow_depth=99.0)

    # Aktiv störning i Cst vid ankomsttiden 09:40.
    _insert_incident(conn, "SIT1", "Cst", "2026-03-10T09:30:00", "2026-03-10T10:30:00", "Signalfel")

    conn.commit()

    # ---- BULK-sidan: så som train_model.py/data_preparation.py faktiskt kör det ----
    df_trains = pd.read_sql_query("SELECT * FROM train_observations", conn)
    df_weather = pd.read_sql_query("SELECT * FROM weather_observations", conn)
    df_incidents = pd.read_sql_query("SELECT * FROM track_works", conn)

    df_trains['scheduled_utc'] = pd.to_datetime(df_trains['scheduled_arrival'], utc=True)
    df_trains['join_hour'] = get_join_hour(df_trains['scheduled_utc'])
    df_weather['join_hour'] = pd.to_datetime(df_weather['timestamp_hour']).dt.tz_localize('UTC')

    prepared = transform_features(df_trains, df_weather, df_incidents)
    bulk_row = prepared[
        (prepared['train_number'] == "924") & (prepared['station_signature'] == "Cst")
    ].iloc[0]

    # ---- LIVE-sidan: så som app.py faktiskt kör det ----
    scheduled_utc = pd.to_datetime("2026-03-10T09:40:00", utc=True)
    live_join_hour = get_join_hour(scheduled_utc)
    live_incident = check_incident_type_live(conn, "Cst", scheduled_utc)
    live_traffic_density = compute_traffic_density_live(conn, "Cst", scheduled_utc)
    live_prev_delay = compute_previous_station_delay_live(conn, "924", scheduled_utc)

    weather_match = df_weather[
        (df_weather['station_signature'] == "Cst") & (df_weather['join_hour'] == live_join_hour)
    ].iloc[0]

    # ---- Jämförelse: live MÅSTE matcha exakt vad bulk-pipelinen räknade ut ----
    assert live_join_hour == pd.Timestamp("2026-03-10 09:00:00", tz='UTC')

    assert weather_match['temperature'] == bulk_row['temperature'] == pytest.approx(-2.5)
    assert live_incident == bulk_row['incident_type'] == "Signalfel"
    assert live_traffic_density == bulk_row['traffic_density'] == 1
    assert live_prev_delay == bulk_row['previous_station_delay'] == 12


def test_previous_station_delay_ignores_gap_over_six_hours(db_conn):
    """Om det gått för lång tid sedan förra stationen (>= 6h) ska
    previous_station_delay vara 0, inte den gamla förseningen - annars
    riskerar man att koppla ihop två orelaterade resor."""
    conn = db_conn
    _insert_train(conn, "500", "M", "2026-03-10T02:00:00", "2026-03-10T02:20:00", 20)
    conn.commit()

    scheduled_utc = pd.to_datetime("2026-03-10T09:00:00", utc=True)  # 7h senare
    assert compute_previous_station_delay_live(conn, "500", scheduled_utc) == 0


def test_previous_station_delay_zero_when_previous_stop_canceled(db_conn):
    """Om föregående station var inställd (delay_minutes = NULL) ska
    previous_station_delay bli 0, precis som fillna(0) ger vid träning -
    inte ett fel eller NaN som kraschar modellen."""
    conn = db_conn
    _insert_train(conn, "600", "M", "2026-03-10T08:00:00", None, None, canceled=1)
    conn.commit()

    scheduled_utc = pd.to_datetime("2026-03-10T08:30:00", utc=True)
    assert compute_previous_station_delay_live(conn, "600", scheduled_utc) == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))