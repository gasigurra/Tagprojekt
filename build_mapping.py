import requests
import sqlite3
import math
import re
import os
import time
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.environ.get("TRAFIKVERKET_API_KEY")
DATABASE_PATH = "train_predictions.db"

# SMHI:s parametrar
PARAM_MAP = {
    "temp": 1,
    "wind": 4,
    "precip": 7,
    "snow": 8
}

def haversine_distance(lat1, lon1, lat2, lon2):
    """Beräknar avstånd i kilometer mellan två GPS-koordinater."""
    R = 6371.0 
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def fetch_smhi_stations(param_id):
    """Hämtar aktiva stationer för en SPECIFIK parameter."""
    url = f"https://opendata-download-metobs.smhi.se/api/version/1.0/parameter/{param_id}.json"
    response = requests.get(url)
    if response.status_code != 200: return []
    
    data = response.json()
    return [{
        "id": s["key"],
        "name": s["name"],
        "lat": s["latitude"],
        "lon": s["longitude"]
    } for s in data.get("station", []) if s.get("active", False)]

def fetch_trafikverket_stations():
    """Hämtar alla Sveriges tågstationer och deras koordinater."""
    query = f"""
    <REQUEST>
      <LOGIN authenticationkey="{API_KEY}" />
      <QUERY objecttype="TrainStation" schemaversion="1.4">
        <FILTER><EQ name="Advertised" value="true" /></FILTER>
        <INCLUDE>LocationSignature</INCLUDE>
        <INCLUDE>AdvertisedLocationName</INCLUDE>
        <INCLUDE>Geometry.WGS84</INCLUDE>
      </QUERY>
    </REQUEST>
    """
    response = requests.post("https://api.trafikinfo.trafikverket.se/v2/data.json", data=query, headers={'Content-Type': 'text/xml'})
    stations = []
    try:
        results = response.json()['RESPONSE']['RESULT'][0]['TrainStation']
        for s in results:
            geom = s.get("Geometry", {}).get("WGS84", "")
            match = re.search(r"POINT \(([\d.]+) ([\d.]+)\)", geom)
            if match:
                stations.append({
                    "sig": s["LocationSignature"],
                    "name": s["AdvertisedLocationName"],
                    "lon": float(match.group(1)),
                    "lat": float(match.group(2))
                })
    except Exception as e:
        print("Kunde inte hämta Trafikverkets stationer:", e)
    return stations

def build_and_save_mapping():
    print("1. Hämtar hela Sveriges järnvägsnät...")
    trv_stations = fetch_trafikverket_stations()
    
    print("2. Hämtar SMHI:s stationsnätverk (kan ta en stund)...")
    smhi_networks = {}
    for key, pid in PARAM_MAP.items():
        smhi_networks[key] = fetch_smhi_stations(pid)
        time.sleep(0.2)  # SÄKERHETSGRÄNS: Max 5 anrop per sekund mot SMHI
        
    conn = sqlite3.connect(DATABASE_PATH, timeout=15)
    cursor = conn.cursor()
    
    print(f"3. Beräknar avstånd och mappar {len(trv_stations)} tågstationer...")
    
    for trv in trv_stations:
        best_matches = {}
        # Hitta den absolut närmaste SMHI-stationen för VARJE sensor
        for param_key, smhi_stations in smhi_networks.items():
            closest_id = None
            min_dist = float('inf')
            for smhi in smhi_stations:
                dist = haversine_distance(trv["lat"], trv["lon"], smhi["lat"], smhi["lon"])
                if dist < min_dist:
                    min_dist = dist
                    closest_id = smhi["id"]
            best_matches[param_key] = closest_id
            
        cursor.execute('''
            INSERT OR REPLACE INTO stations_mapping 
            (station_signature, station_name, lat, lon, smhi_temp_id, smhi_wind_id, smhi_precip_id, smhi_snow_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (trv["sig"], trv["name"], trv["lat"], trv["lon"], 
              best_matches["temp"], best_matches["wind"], best_matches["precip"], best_matches["snow"]))
        
    conn.commit()
    conn.close()
    print("Sverigekartan är färdig och sparad i databasen!")

if __name__ == "__main__":
    if not API_KEY:
        print("Saknar API-nyckel i .env!")
    else:
        build_and_save_mapping()