import sqlite3
import pandas as pd

DATABASE_PATH = "train_predictions.db"

def add_incident_flag(df_trains, df_incidents):
    """Ger oss textkategorin (t.ex. Olycka) istället för bara 1/0."""
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

        def get_incident_type(arrival_time):
            active = station_incidents[
                (station_incidents['start_time'] <= arrival_time) & 
                (station_incidents['end_time'].isna() | (station_incidents['end_time'] >= arrival_time))
            ]
            if not active.empty:
                return active['severity_level'].iloc[0] # "Olycka", "Signalfel" osv.
            return "Ingen"

        df_trains.loc[mask, 'incident_type'] = df_trains.loc[mask, 'scheduled_utc'].apply(get_incident_type)

    return df_trains

def add_previous_station_delay(df_trains):
    """Hittar förseningen från samma tåg på förra stationen inom samma unika resa."""
    df_sorted = df_trains.sort_values(['train_number', 'scheduled_utc']).copy()

    # 1. Skapa ett unikt rese-ID genom att kombinera tågnummer och datum
    df_sorted['trip_id'] = (
        df_sorted['train_number'].astype(str) + "_" + 
        df_sorted['scheduled_utc'].dt.date.astype(str)
    )

    # 2. Gruppera på det unika rese-ID:t istället för bara tågnumret
    df_sorted['prev_delay'] = df_sorted.groupby('trip_id')['delay_minutes'].shift(1)
    df_sorted['prev_time'] = df_sorted.groupby('trip_id')['scheduled_utc'].shift(1)

    # 3. Beräkna tidsskillnaden i timmar
    time_diff_hours = (df_sorted['scheduled_utc'] - df_sorted['prev_time']).dt.total_seconds() / 3600

    # 4. Godkänn bara förseningen om det är samma resa OCH stoppavståndet är < 6 timmar
    df_sorted['previous_station_delay'] = df_sorted['prev_delay'].where(time_diff_hours < 6, 0)

    return df_sorted.drop(columns=['trip_id', 'prev_delay', 'prev_time'])

def add_accurate_traffic_density(df_trains):
    """Beräknar trafiktäthet på en komplett tidslinje utan blinda fläckar."""
    df = df_trains.copy()
    
    # Sortera efter station och tidsstämpel för att ha en garanterad ordning
    df = df.sort_values(by=['station_signature', 'scheduled_utc']).reset_index(drop=True)
    
    # 💡 LÖSNINGEN: Genom att använda .reset_index(drop=True) stannar vi helt 
    # i Pandas-ekosystemet och NumPy blandas aldrig in!
    rolling_counts = (
        df.set_index('scheduled_utc')
        .groupby('station_signature', sort=False)['train_number']
        .rolling('15min', center=True).count()
        .reset_index(drop=True)
    )
    
    # Nu är det garanterat en ren Pandas Series med 100% kompatibla funktioner
    df['traffic_density'] = (rolling_counts - 1).clip(lower=0).fillna(0).astype(int)
    
    return df

def load_and_prepare_data():
    print("Laddar data från databasen...")
    conn = sqlite3.connect(DATABASE_PATH, timeout=15)
    df_train = pd.read_sql_query("SELECT * FROM train_observations", conn)
    df_weather = pd.read_sql_query("SELECT * FROM weather_observations", conn)
    df_incidents = pd.read_sql_query("SELECT * FROM track_works", conn)
    conn.close()

    if df_train.empty or df_weather.empty:
        print("⚠️ Saknar tåg eller väder i databasen.")
        return None

    # Tidszonsharmonisering
    df_train['scheduled_utc'] = pd.to_datetime(df_train['scheduled_arrival'], utc=True)
    df_train['join_hour'] = df_train['scheduled_utc'].dt.round('h')
    df_weather['join_hour'] = pd.to_datetime(df_weather['timestamp_hour']).dt.tz_localize('UTC')

    print("Beräknar störningsflagga (incident_type)...")
    df_train = add_incident_flag(df_train, df_incidents)

    print("Beräknar kedjeeffekt (previous_station_delay)...")
    df_train = add_previous_station_delay(df_train)

    print("Beräknar exakt trafiktäthet (traffic_density) utan blinda skarvar...")
    df_train = add_accurate_traffic_density(df_train)

    print("Slår ihop tabellerna...")
    df_merged = pd.merge(df_train, df_weather, how='left', on=['station_signature', 'join_hour'])

    # Städar upp och förhindrar geografiskt väderläckage genom att gruppera ffill per station
    df_merged = df_merged.sort_values(by=['station_signature', 'join_hour'])
    df_merged['snow_depth'] = df_merged.groupby('station_signature')['snow_depth'].ffill().fillna(0.0)
    
    df_merged['traffic_density'] = df_merged['traffic_density'].fillna(0)
    df_merged['is_single_track'] = df_merged['is_single_track'].fillna(0)
    
    df_merged['train_type'] = df_merged['train_type'].fillna('Okänt')
    df_merged['operator'] = df_merged['operator'].fillna('Okänt')
    df_merged['incident_type'] = df_merged['incident_type'].fillna('Ingen')
    df_merged['previous_station_delay'] = df_merged['previous_station_delay'].fillna(0)

    # Filtrerar bort ogiltiga
    df_cleaned = df_merged[df_merged['canceled'] == 0].copy()
    df_cleaned = df_cleaned.dropna(subset=['temperature', 'wind_speed', 'precipitation', 'delay_minutes'])

    cols_to_drop = ['id_x', 'id_y', 'id', 'actual_arrival', 'scheduled_utc', 'timestamp_hour', 'canceled']
    df_cleaned = df_cleaned.drop(columns=[col for col in cols_to_drop if col in df_cleaned.columns])

    print(f"\nTotalt antal giltiga rader: {len(df_cleaned)}")
    return df_cleaned

if __name__ == "__main__":
    final_data = load_and_prepare_data()