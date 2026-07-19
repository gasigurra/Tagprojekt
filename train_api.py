import os
import requests
import sqlite3
import time
from datetime import datetime, timedelta
from dotenv import load_dotenv

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

def fetch_and_save_trains(hours_offset):
    now = datetime.now()
    time_from = (now - timedelta(hours=hours_offset)).strftime("%Y-%m-%dT%H:%M:%S")
    time_to = (now - timedelta(hours=hours_offset-4)).strftime("%Y-%m-%dT%H:%M:%S")
    
    print(f"Hämtar tåg för intervallet {time_from} till {time_to}...")
    response = requests.post(
        TRAFIKVERKET_URL, 
        data=build_train_chunk_query(time_from, time_to), 
        headers={'Content-Type': 'text/xml'},
        timeout=30
    )
    
    if response.status_code != 200: 
        return
    try: 
        trains = response.json()['RESPONSE']['RESULT'][0]['TrainAnnouncement']
    except (KeyError, ValueError): 
        return

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
            # Vi sparar in 0 i traffic_density här - den korrekta beräkningen 
            # görs nu i data_preparation.py över hela tidslinjen utan blinda fläckar!
            cursor.execute('''
                INSERT OR IGNORE INTO train_observations 
                (train_number, station_signature, scheduled_arrival, actual_arrival, delay_minutes, canceled, operator, train_type, traffic_density)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            ''', (ident, station, sched_str, actual_str, delay, 1 if canceled else 0, operator, train_type))
            if cursor.rowcount > 0: 
                inserted += 1
        except sqlite3.Error: 
            pass

    conn.commit()
    conn.close()
    print(f"  -> Sparade {inserted} nya tåg.")

def fetch_messages():
    """Hämtar fel och incidenter. Kollar 14 dagar bakåt."""
    print("Hämtar rikstäckande signalfel och banarbeten...")
    time_from = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%S")
    
    # Vi har uppgraderat schemaversion från 1.7 till 1.8 (eller 1.9)
    query = f"""
    <REQUEST>
      <LOGIN authenticationkey="{API_KEY}" />
      <QUERY objecttype="TrainMessage" schemaversion="1.8">
        <FILTER><GT name="StartDateTime" value="{time_from}" /></FILTER>
      </QUERY>
    </REQUEST>
    """
    
    try:
        response = requests.post(
            TRAFIKVERKET_URL, 
            data=query, 
            headers={'Content-Type': 'text/xml'},
            timeout=30
        )
        
        # Om Trafikverket svarar med ett HTTP-fel (t.ex. 400 eller 500)
        if response.status_code != 200:
            print(f"⚠️ Varning: HTTP-fel från Trafikverket för TrainMessage ({response.status_code}).")
            print("ℹ️ Serverns svar:", response.text[:300])
            print("👉 Hoppar över banarbeten just nu – AI-modellen fortsätter träna på tåg och väder!")
            return

        data = response.json()
        
        # Kolla om Trafikverket skickade ett internt felmeddelande i JSON-svaret
        if "ERROR" in str(data):
            print("⚠️ Varning: Trafikverkets API returnerade ett schemafel för TrainMessage.")
            print("ℹ️ Meddelande från servern:", str(data)[:300])
            print("👉 Hoppar över banarbeten just nu – AI-modellen fortsätter träna på tåg och väder!")
            return

        messages = data['RESPONSE']['RESULT'][0]['TrainMessage']
        
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Nätverksfel vid anrop till Trafikverket (TrainMessage): {e}")
        print("👉 Hoppar över banarbeten och fortsätter kedjan!")
        return
    except (KeyError, ValueError, IndexError) as e:
        print(f"⚠️ Kunde inte tolka JSON-svaret för TrainMessage: {e}")
        print("👉 Hoppar över banarbeten och fortsätter kedjan!")
        return
    
    inserted = 0
    try:
        with sqlite3.connect(DATABASE_PATH, timeout=15) as conn:
            cursor = conn.cursor()
            for m in messages:
                if not m.get('AffectedLocation'): 
                    continue
                
                # Fältsäker avläsning av ID: Kollar efter EventId eller Id
                incident_id = m.get('EventId', m.get('Id', str(m.get('StartDateTime', 'okänd'))))
                
                for station in m['AffectedLocation']:
                    cursor.execute('''
                        INSERT OR IGNORE INTO track_works 
                        (incident_id, affected_station, start_time, end_time, severity_level)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (
                        incident_id, 
                        station, 
                        m.get('StartDateTime'), 
                        m.get('EndDateTime'), 
                        m.get('Header', 'Banarbete')
                    ))
                    if cursor.rowcount > 0:
                        inserted += 1
    except sqlite3.Error as e:
        print(f"❌ Databasfel vid sparande av banarbeten: {e}")
        raise
        
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