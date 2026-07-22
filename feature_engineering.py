"""
feature_engineering.py
-----------------------
Delade beräkningar för prediktionsfeatures - används BÅDE av träningspipelinen
(data_preparation.py, som kör dem i bulk över hela datasetet) och av appen
(app.py, som kör dem "live" för en enskild förfrågan).

VARFÖR DEN HÄR FILEN FINNS:
Tidigare fanns liknande logik duplicerad på två ställen - en bulk-variant för
träning, en live-variant i appen - och de hade tyst börjat divergera:

  1. join_hour (vilken timmes väder som slås upp) räknades ut med round('h')
     vid träning men floor('h') i appen.
  2. traffic_density räknades med closed='left' (bara historiska tåg) vid
     träning, men den efterhandsrättande update_traffic_density.py använde
     center=True (även "framtida" tåg) - en annan definition än träningens.
  3. previous_station_delay räknades ut korrekt vid träning men skrevs
     aldrig tillbaka till databasen, så appen läste alltid NULL -> 0.

Genom att BÅDA sidor anropar exakt samma funktioner här kan de per
definition inte gå isär igen. Om du ändrar en definition, ändra den här -
både träning och app får automatiskt samma nya beteende.
"""

import pandas as pd

TRAFFIC_WINDOW_MINUTES = 15
PREVIOUS_STATION_MAX_GAP_HOURS = 6

# Bumpa den här när en definition ovan ändras. train_model.py sparar värdet
# i model_metadata.json och app.py vägrar ladda en sparad modell vars
# feature-definitioner inte matchar koden som körs just nu.
FEATURE_ENGINEERING_VERSION = "2"


def get_join_hour(timestamp):
    """
    Standardiserad avrundning av en tidsstämpel till den timme vars
    vädermätning ska användas.

    Vi använder GOLV (floor), inte närmaste timme (round): en tågankomst
    kl 14:40 ska matchas mot vädermätningen från 14:00, inte 15:00 - 15:00-
    mätningen existerade inte ens än vid tiden för ankomsten. Att använda
    round() vid träning gav tidigare modellen tillgång till väder "från
    framtiden" som en live-prediktion aldrig skulle ha tillgång till.

    Fungerar både på ett enskilt pandas.Timestamp (live, app.py) och på en
    hel pandas.Series (bulk, data_preparation.py).
    """
    if isinstance(timestamp, pd.Series):
        return timestamp.dt.floor("h")
    return timestamp.floor("h")


def get_incident_type(df_incidents_for_station, arrival_time_utc):
    """
    Avgör vilken störningstyp (om någon) som var aktiv vid EN station vid en
    given tidpunkt. df_incidents_for_station måste redan vara filtrerad till
    en enskild station (både bulk- och live-anroparen gör det filtreringen
    själva, se add_incident_flag i data_preparation.py resp.
    check_incident_type_live nedan).
    """
    if df_incidents_for_station.empty:
        return "Ingen"

    active = df_incidents_for_station[
        (df_incidents_for_station["start_time"] <= arrival_time_utc)
        & (
            df_incidents_for_station["end_time"].isna()
            | (df_incidents_for_station["end_time"] >= arrival_time_utc)
        )
    ]
    if not active.empty:
        return active["severity_level"].iloc[0]
    return "Ingen"


def check_incident_type_live(conn, station_signature, arrival_time_utc):
    """Live-variant: hämtar störningar för EN station direkt från databasen
    och återanvänder exakt samma matchningsregel som bulk-sidan."""
    df_incidents = pd.read_sql_query(
        "SELECT * FROM track_works WHERE affected_station = ?",
        conn,
        params=(station_signature,),
    )
    if df_incidents.empty:
        return "Ingen"

    df_incidents["start_time"] = pd.to_datetime(df_incidents["start_time"], utc=True)
    df_incidents["end_time"] = pd.to_datetime(df_incidents["end_time"], utc=True)
    return get_incident_type(df_incidents, arrival_time_utc)


def compute_traffic_density_live(conn, station_signature, scheduled_utc):
    """
    Live-variant av add_accurate_traffic_density i data_preparation.py:
    räknar tåg vid samma station i de senaste TRAFFIC_WINDOW_MINUTES
    minuterna FÖRE ankomsten - samma halvöppna fönster [t-15min, t) som
    rolling(..., closed='left') använder vid träning (dvs. tåget självt
    räknas INTE med).

    OBS - medveten kvarleva från träningslogiken: precis som i
    add_accurate_traffic_density räknas inställda tåg med här också
    (canceled-filtreringen sker efter täthetsberäkningen i
    transform_features). Det är inte ett oberoende designval i den här
    funktionen, utan en spegling av hur träningsdatan faktiskt beräknas -
    om det är rätt DEFINITION av "trafiktäthet" är en separat fråga värd
    att ta ställning till, men den hör inte hemma i en train/serving-
    parity-fix.
    """
    window_start = scheduled_utc - pd.Timedelta(minutes=TRAFFIC_WINDOW_MINUTES)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT COUNT(*) FROM train_observations
        WHERE station_signature = ?
          AND scheduled_arrival >= ?
          AND scheduled_arrival < ?
        """,
        (
            station_signature,
            window_start.strftime("%Y-%m-%dT%H:%M:%S"),
            scheduled_utc.strftime("%Y-%m-%dT%H:%M:%S"),
        ),
    )
    (count,) = cursor.fetchone()
    return int(count)


def compute_previous_station_delay_live(conn, train_number, scheduled_utc):
    """
    Live-variant av add_previous_station_delay i data_preparation.py:
    hittar den senaste tidigare stationen för SAMMA tågnummer, SAMMA
    kalenderdag (motsvarar trip_id = train_number + datum vid träning),
    inom PREVIOUS_STATION_MAX_GAP_HOURS timmar, och returnerar dess
    försening. Om ingen sådan station finns, om gapet är för stort, eller
    om den föregående stationen var inställd (delay_minutes NULL),
    returneras 0 - exakt samma fallback som fillna(0) ger vid träning.

    Det här är fixen för huvudbuggen: tidigare lästes den här kolumnen
    direkt ur databasen i app.py, men ingen skript skrev NÅGONSIN ett
    värde dit, så den var alltid NULL -> alltid 0, oavsett vad modellen
    faktiskt lärt sig av signalen under träning.
    """
    df = pd.read_sql_query(
        "SELECT scheduled_arrival, delay_minutes FROM train_observations WHERE train_number = ?",
        conn,
        params=(str(train_number),),
    )
    if df.empty:
        return 0

    df["scheduled_utc"] = pd.to_datetime(df["scheduled_arrival"], utc=True)
    earlier = df[df["scheduled_utc"] < scheduled_utc]
    if earlier.empty:
        return 0

    prev = earlier.sort_values("scheduled_utc").iloc[-1]

    if prev["scheduled_utc"].date() != scheduled_utc.date():
        return 0

    gap_hours = (scheduled_utc - prev["scheduled_utc"]).total_seconds() / 3600
    if gap_hours >= PREVIOUS_STATION_MAX_GAP_HOURS:
        return 0

    return 0 if pd.isna(prev["delay_minutes"]) else int(prev["delay_minutes"])