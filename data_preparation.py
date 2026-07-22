# 1. SORTERA KRONOLOGISKT
import sqlite3
import pandas as pd

from feature_engineering import get_incident_type, get_join_hour

DATABASE_PATH = "train_predictions.db"

def add_incident_flag(df_trains, df_incidents):
    """Matcherar aktiva störningar mot tågets planerade ankomsttid.

    Matchningsregeln (get_incident_type) kommer från feature_engineering.py
    och är EXAKT samma funktion som app.py anropar live via
    check_incident_type_live - de kan inte längre gå isär av misstag."""
    df_trains = df_trains.copy()
    df_trains['incident_type'] = "Ingen"

    if df_incidents.empty:
        return df_trains

    df_incidents = df_incidents.copy()
    df_incidents['start_time'] = pd.to_datetime(df_incidents['start_time'], utc=True)
    df_incidents['end_time'] = pd.to_datetime(df_incidents['end_time'], utc=True)

    for station in df_trains['station_signature'].unique():
        station_incidents = df_incidents[df_incidents['affected_station'] == station]
        if station_incidents.empty:
            continue

        mask = df_trains['station_signature'] == station
        df_trains.loc[mask, 'incident_type'] = df_trains.loc[mask, 'scheduled_utc'].apply(
            lambda arrival_time: get_incident_type(station_incidents, arrival_time)
        )

    return df_trains

def add_previous_station_delay(df_trains):
    """Kopplar ihop förra stationens försening inom samma unika resa."""
    df_sorted = df_trains.sort_values(['train_number', 'scheduled_utc']).copy()

    df_sorted['trip_id'] = (
        df_sorted['train_number'].astype(str) + "_" + 
        df_sorted['scheduled_utc'].dt.date.astype(str)
    )

    df_sorted['prev_delay'] = df_sorted.groupby('trip_id')['delay_minutes'].shift(1)
    df_sorted['prev_time'] = df_sorted.groupby('trip_id')['scheduled_utc'].shift(1)

    time_diff_hours = (df_sorted['scheduled_utc'] - df_sorted['prev_time']).dt.total_seconds() / 3600
    df_sorted['previous_station_delay'] = df_sorted['prev_delay'].where(time_diff_hours < 6, 0)

    return df_sorted.drop(columns=['trip_id', 'prev_delay', 'prev_time'])

def add_accurate_traffic_density(df_trains):
    """Räknar enbart tåg som anlänt bakåt i tiden (closed='left') för att undvika läckage."""
    df = df_trains.copy()
    df = df.sort_values(by=['station_signature', 'scheduled_utc']).reset_index(drop=True)
    
    rolling_counts = (
        df.set_index('scheduled_utc')
        .groupby('station_signature', sort=False)['train_number']
        .rolling('15min', closed='left').count()
        .reset_index(drop=True)
    )
    
    df['traffic_density'] = rolling_counts.fillna(0).astype(int)
    return df

def transform_features(df_trains, df_weather, df_incidents):
    """Kör alla feature-transformationer isolerat på ett enskilt dataset."""
    df = df_trains.copy()
    
    df = add_incident_flag(df, df_incidents)
    df = add_previous_station_delay(df)
    df = add_accurate_traffic_density(df)

    df_merged = pd.merge(df, df_weather, how='left', on=['station_signature', 'join_hour'])

    df_merged = df_merged.sort_values(by=['station_signature', 'join_hour'])
    df_merged['snow_depth'] = df_merged.groupby('station_signature')['snow_depth'].ffill().fillna(0.0)
    
    df_merged['traffic_density'] = df_merged['traffic_density'].fillna(0)
    df_merged['is_single_track'] = df_merged['is_single_track'].fillna(0)
    df_merged['train_type'] = df_merged['train_type'].fillna('Okänt')
    df_merged['operator'] = df_merged['operator'].fillna('Okänt')
    df_merged['incident_type'] = df_merged['incident_type'].fillna('Ingen')
    df_merged['previous_station_delay'] = df_merged['previous_station_delay'].fillna(0)

    df_cleaned = df_merged[df_merged['canceled'] == 0].copy()
    df_cleaned = df_cleaned.dropna(subset=['temperature', 'wind_speed', 'precipitation', 'delay_minutes'])

    cols_to_drop = ['id_x', 'id_y', 'id', 'actual_arrival', 'timestamp_hour', 'canceled']
    df_cleaned = df_cleaned.drop(columns=[col for col in cols_to_drop if col in df_cleaned.columns])

    return df_cleaned

def load_and_prepare_split_data(split_ratio=0.8):
    """Laddar rådata, gör kronologisk split FÖRST, och transformerar sedan train och test separat."""
    print("Laddar rådata från databasen...")
    conn = sqlite3.connect(DATABASE_PATH, timeout=15)
    df_train_raw = pd.read_sql_query("SELECT * FROM train_observations", conn)
    df_weather_raw = pd.read_sql_query("SELECT * FROM weather_observations", conn)
    df_incidents_raw = pd.read_sql_query("SELECT * FROM track_works", conn)
    conn.close()

    if df_train_raw.empty or df_weather_raw.empty:
        print("⚠️ Saknar tåg eller väder i databasen.")
        return None, None

    df_train_raw['scheduled_utc'] = pd.to_datetime(df_train_raw['scheduled_arrival'], utc=True)
    # get_join_hour golvar till timmen (se feature_engineering.py för varför
    # det måste vara golv och inte närmaste timme) - samma funktion som
    # app.py anropar live, så väderuppslaget kan inte längre gå isär.
    df_train_raw['join_hour'] = get_join_hour(df_train_raw['scheduled_utc'])
    df_weather_raw['join_hour'] = pd.to_datetime(df_weather_raw['timestamp_hour']).dt.tz_localize('UTC')

    df_train_raw = df_train_raw.sort_values(by='scheduled_utc').reset_index(drop=True)

    split_idx = int(len(df_train_raw) * split_ratio)
    raw_train = df_train_raw.iloc[:split_idx].copy()
    raw_test = df_train_raw.iloc[split_idx:].copy()

    print(f"Bygger features separat för Train ({len(raw_train)} rader) och Test ({len(raw_test)} rader)...")
    
    df_train_prepared = transform_features(raw_train, df_weather_raw, df_incidents_raw)
    df_test_prepared = transform_features(raw_test, df_weather_raw, df_incidents_raw)

    return df_train_prepared, df_test_prepared