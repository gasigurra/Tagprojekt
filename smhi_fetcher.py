import requests
import sqlite3
import time
from datetime import datetime, timezone

DATABASE_PATH = "train_predictions.db"
SMHI_BASE_URL = "https://opendata-download-metobs.smhi.se/api/version/1.0"
# Hämtar latest-day för att undvika överbelastning
PERIOD = "latest-day" 

PARAM_MAP = {
    "temperature": ("smhi_temp_id", 1),
    "wind_speed": ("smhi_wind_id", 4),
    "precipitation": ("smhi_precip_id", 7),
    "snow_depth": ("smhi_snow_id", 8)
}

def get_unique_smhi_stations(column_name):
    """Hämtar enbart de unika sensorer vi faktiskt behöver från SMHI."""
    conn = sqlite3.connect(DATABASE_PATH, timeout=15)
    cursor = conn.cursor()
    cursor.execute(f"SELECT DISTINCT {column_name} FROM stations_mapping WHERE {column_name} IS NOT NULL")
    stations = [row[0] for row in cursor.fetchall()]
    conn.close()
    return stations

def fetch_data(param_id, station_id):
    url = f"{SMHI_BASE_URL}/parameter/{param_id}/station/{station_id}/period/{PERIOD}/data.json"
    response = requests.get(url)
    if response.status_code == 200:
        try: return response.json()
        except ValueError: return None
    return None

def round_to_hour_iso(timestamp_ms):
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return dt.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")

def fetch_nationwide_weather():
    conn = sqlite3.connect(DATABASE_PATH, timeout=15)
    cursor = conn.cursor()

    for param_name, (col_name, param_id) in PARAM_MAP.items():
        unique_stations = get_unique_smhi_stations(col_name)
        print(f"Hämtar {param_name} för {len(unique_stations)} unika mätstationer...")
        
        for i, smhi_id in enumerate(unique_stations):
            # Hitta alla TRV-stationer som delar denna sensor
            cursor.execute(f"SELECT station_signature FROM stations_mapping WHERE {col_name} = ?", (smhi_id,))
            trv_signatures = [row[0] for row in cursor.fetchall()]
            
            data = fetch_data(param_id, smhi_id)
            time.sleep(0.2) # SÄKERHETSGRÄNS: Max 5 anrop per sekund
            
            if not data or "value" not in data: continue
            
            # Spara vädret på ALLA tågstationer som delar sensorn
            for entry in data["value"]:
                val = entry.get("value")
                if val in (None, ""): continue
                try:
                    val = float(val)
                    ts = round_to_hour_iso(entry["date"])
                    for trv_sig in trv_signatures:
                        cursor.execute(f'''
                            INSERT INTO weather_observations (station_signature, timestamp_hour, {param_name})
                            VALUES (?, ?, ?)
                            ON CONFLICT(station_signature, timestamp_hour)
                            DO UPDATE SET {param_name} = excluded.{param_name}
                        ''', (trv_sig, ts, val))
                except (TypeError, ValueError): pass
            
        conn.commit()
    conn.close()
    print("✅ Rikstäckande väder hämtat och sparat!")

if __name__ == "__main__":
    fetch_nationwide_weather()