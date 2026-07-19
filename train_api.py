import os
import requests
import sqlite3
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv
from collections import defaultdict

load_dotenv()
API_KEY = os.environ.get("TRAFIKVERKET_API_KEY")
TRAFIKVERKET_URL = "https://api.trafikinfo.trafikverket.se/v2/data.json"
DATABASE_PATH = "train_predictions.db"

def build_train_chunk_query(time_from, time_to):
    """Begär ALLA tåg i landet under en viss tidsram för att inte krascha API:et."""
    return f"""
    <REQUEST>
      <LOGIN authenticationkey="{API_KEY}" />
      <QUERY objecttype="TrainAnnouncement" schemaversion="1.9">
        <FILTER>
          <EQ name="ActivityType" value="Ankomst" />
          <GT name="AdvertisedTimeAtLocation" value="{time_from}" />
          <LT name="AdvertisedTimeAtLocation" value="{time_to}" />
        </FILTER>
        <INCLUDE>AdvertisedTrainIdent</INCLUDE>
        <INCLUDE>LocationSignature</INCLUDE>
        <INCLUDE>AdvertisedTimeAtLocation</INCLUDE>
        <INCLUDE>TimeAtLocation</INCLUDE>
        <INCLUDE>Canceled</INCLUDE>
        <INCLUDE>TrainOwner</INCLUDE>
        <INCLUDE>ProductInformation</INCLUDE>
      </QUERY>
    </REQUEST>
    """

def calculate_density_in_memory(trains):
    """Grupperar tåg i RAM-minnet istället för att överbelasta SQLite."""
    station_groups = defaultdict(list)
    for t in trains:
        if t.get('AdvertisedTimeAtLocation'):
            dt = datetime.fromisoformat(t['AdvertisedTimeAtLocation'].replace("Z", "+00:00"))
            station_groups[t.get('LocationSignature')].append((t, dt))
            
    for sig, group in station_groups.items():
        for t1, dt1 in group:
            density = 0
            for t2, dt2 in group:
                if abs((dt1 - dt2).total_seconds()) <= 900:
                    density += 1
            t1['traffic_density'] = density - 1
            
    return trains

def fetch_and_save_trains(hours_offset):
    now = datetime.now()
    time_from = (now - timedelta(hours=hours_offset)).strftime("%Y-%m-%dT%H:%M:%S")
    time_to = (now - timedelta(hours=hours_offset-4)).strftime("%Y-%m-%dT%H:%M:%S")
    
    print(f"Hämtar tåg för intervallet {time_from} till {time_to}...")
    response = requests.post(TRAFIKVERKET_URL, data=build_train_chunk_query(time_from, time_to), headers={'Content-Type': 'text/xml'})
    
    if response.status_code != 200: return
    try: trains = response.json()['RESPONSE']['RESULT'][0]['TrainAnnouncement']
    except KeyError: return

    trains = calculate_density_in_memory(trains)

    conn = sqlite3.connect(DATABASE_PATH, timeout=15)
    cursor = conn.cursor()
    inserted = 0

    for train in trains:
        ident = train.get('AdvertisedTrainIdent')
        station = train.get('LocationSignature')
        sched_str = train.get('AdvertisedTimeAtLocation')
        actual_str = train.get('TimeAtLocation')
        canceled = train.get('Canceled', False)
        
        operator = train.get('TrainOwner', 'Okänd')
        prod = train.get('ProductInformation')
        train_type = prod[0] if prod and not str(prod[0]).isdigit() else 'Okänd'
        
        delay = None
        if sched_str and actual_str and not canceled:
            sched = datetime.fromisoformat(sched_str.replace("Z", "+00:00"))
            actual = datetime.fromisoformat(actual_str.replace("Z", "+00:00"))
            delay = int((actual - sched).total_seconds() / 60)

        try:
            cursor.execute('''
                INSERT OR IGNORE INTO train_observations 
                (train_number, station_signature, scheduled_arrival, actual_arrival, delay_minutes, canceled, operator, train_type, traffic_density)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (ident, station, sched_str, actual_str, delay, 1 if canceled else 0, operator, train_type, train.get('traffic_density', 0)))
            if cursor.rowcount > 0: inserted += 1
        except sqlite3.Error: pass

    conn.commit()
    conn.close()
    print(f"  -> Sparade {inserted} nya tåg.")

import json

def fetch_messages():
    """Hämtar fel och incidenter. Kollar 14 dagar bakåt."""
    print("Hämtar rikstäckande signalfel och banarbeten...")
    time_from = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%S")
    
    query = f"""
    <REQUEST>
      <LOGIN authenticationkey="{API_KEY}" />
      <QUERY objecttype="TrainMessage" schemaversion="1.7">
        <FILTER><GT name="StartDateTime" value="{time_from}" /></FILTER>
        <INCLUDE>EventId</INCLUDE>
        <INCLUDE>AffectedLocation</INCLUDE>
        <INCLUDE>Header</INCLUDE>
        <INCLUDE>StartDateTime</INCLUDE>
        <INCLUDE>EndDateTime</INCLUDE>
      </QUERY>
    </REQUEST>
    """
    
    # 1. Hantera nätverksfel och lägg till timeout
    try:
        response = requests.post(
            TRAFIKVERKET_URL, 
            data=query, 
            headers={'Content-Type': 'text/xml'},
            timeout=30  # Förhindrar att programmet hänger sig
        )
    except requests.exceptions.RequestException as e:
        print(f"❌ Nätverksfel vid anrop till Trafikverket (TrainMessage): {e}")
        raise  # Kasta vidare felet så att run_all.py märker att skriptet kraschade!

    # 2. Hantera HTTP-statusfel
    if response.status_code != 200:
        print(f"❌ HTTP-fel från Trafikverket ({response.status_code}):")
        print(response.text[:1000])  # Skriv ut API:ets felmeddelande
        response.raise_for_status()  # Kastar en HTTPError

    # 3. Hantera JSON- och strukturfel
    try:
        data = response.json()
        messages = data['RESPONSE']['RESULT'][0]['TrainMessage']
    except (KeyError, ValueError) as e:
        print(f"❌ Kunde inte tolka svaret från Trafikverket ({type(e).__name__}): {e}")
        # Om Trafikverket skickat ett felmeddelande i JSON-format, skriv ut det:
        print("Svarsinnehåll:", json.dumps(response.json(), indent=2, ensure_ascii=False)[:1500] if response.text else "Tomt svar")
        raise RuntimeError("Felaktig svarsstruktur från Trafikverkets API (TrainMessage).") from e
    
    # 4. Hantera databasinfogning säkert med context manager
    inserted = 0
    try:
        # Med 'with' stängs databasen automatiskt, och ändringar görs bara om inget kraschar
        with sqlite3.connect(DATABASE_PATH, timeout=15) as conn:
            cursor = conn.cursor()
            for m in messages:
                if not m.get('AffectedLocation'): 
                    continue
                for station in m['AffectedLocation']:
                    cursor.execute('''
                        INSERT OR IGNORE INTO track_works 
                        (incident_id, affected_station, start_time, end_time, severity_level)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (
                        m.get('EventId'), 
                        station, 
                        m.get('StartDateTime'), 
                        m.get('EndDateTime'), 
                        m.get('Header')
                    ))
                    if cursor.rowcount > 0:
                        inserted += 1
    except sqlite3.Error as e:
        print(f"❌ Databasfel vid sparande av banarbeten: {e}")
        raise  # Kasta felet vidare
        
    print(f"  -> Sparade {inserted} nya störningar/banarbeten.")

if __name__ == '__main__':
    if not API_KEY:
        print("Saknar API-nyckel i .env!")
    else:
        # Tanka ner 14 dagar i 4-timmars-chunks för hela Sverige
        for offset in range(336, 0, -4):
            fetch_and_save_trains(offset)
            time.sleep(0.2)  # SÄKERHETSGRÄNS: Pausar mellan chunks
        
        time.sleep(0.2)
        fetch_messages()