import sqlite3
import os


def create_database(db_path='train_predictions.db', confirm_delete=True):
    if os.path.exists(db_path):
        if confirm_delete:
            print(f"⚠️  '{db_path}' finns redan och innehåller sannolikt insamlad data.")
            answer = input("Skriv JA (versaler) för att radera och börja om från noll: ")
            if answer.strip() != "JA":
                print("Avbrutet. Databasen rördes inte.")
                return
        os.remove(db_path)
        
    # timeout=15 förhindrar "database is locked" vid hög belastning
    conn = sqlite3.connect(db_path, timeout=15)
    cursor = conn.cursor()

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS train_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        train_number TEXT NOT NULL,
        station_signature TEXT NOT NULL,
        scheduled_arrival DATETIME NOT NULL,
        actual_arrival DATETIME,
        delay_minutes INTEGER,
        canceled BOOLEAN DEFAULT 0,
        operator TEXT,
        train_type TEXT,
        traffic_density INTEGER,
        is_single_track BOOLEAN DEFAULT 0,
        previous_station_delay INTEGER,
        UNIQUE(train_number, station_signature, scheduled_arrival)
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS weather_observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        station_signature TEXT NOT NULL,
        timestamp_hour DATETIME NOT NULL,
        temperature FLOAT,
        precipitation FLOAT,
        wind_speed FLOAT,
        snow_depth FLOAT,
        UNIQUE(station_signature, timestamp_hour)
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS track_works (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        incident_id TEXT NOT NULL,
        affected_station TEXT NOT NULL,
        start_time DATETIME NOT NULL,
        end_time DATETIME,
        severity_level TEXT,
        UNIQUE(incident_id, affected_station)
    )
    ''')

    cursor.execute('''
    CREATE TABLE IF NOT EXISTS stations_mapping (
        station_signature TEXT PRIMARY KEY,
        station_name TEXT,
        lat FLOAT,
        lon FLOAT,
        smhi_temp_id INTEGER,
        smhi_wind_id INTEGER,
        smhi_precip_id INTEGER,
        smhi_snow_id INTEGER
    )
    ''')

    cursor.execute('CREATE INDEX idx_train_time ON train_observations(scheduled_arrival);')
    cursor.execute('CREATE INDEX idx_train_number ON train_observations(train_number);')
    # Ingen tidigare index täckte station_signature ensamt (UNIQUE-indexet
    # har train_number som första kolumn, hjälper inte "WHERE
    # station_signature = ?"-frågor). Behövs av app.py:s live-uppslag
    # (feature_engineering.compute_traffic_density_live m.fl.) för att slippa
    # full tabellscan på en tabell som i praktiken växer sig stor.
    cursor.execute('CREATE INDEX idx_train_station ON train_observations(station_signature, scheduled_arrival);')
    cursor.execute('CREATE INDEX idx_weather_time ON weather_observations(timestamp_hour);')
    cursor.execute('CREATE INDEX idx_trackworks_station ON track_works(affected_station);')

    conn.commit()
    print("Databasen (v4) har skapats från noll. Redo för rikstäckande drift!")
    conn.close()


if __name__ == '__main__':
    create_database()