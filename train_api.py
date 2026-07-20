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

def extract_train_type(product_information):
    """
    Tolkar ProductInformation defensivt. Fältet kan innehålla antingen
    rena strängar ELLER objekt (t.ex. {"Description": "..."}) beroende
    på Trafikverkets svar - vi har inte kunnat verifiera exakt form med
    riktig data än, så vi hanterar båda fallen istället för att riskera
    att spara en stringifierad dict som kategori (vilket ger falskt hög
    kardinalitet i modellen).
    """
    if not product_information:
        return "Okänd"

    first = product_information[0]

    if isinstance(first, dict):
        # Prova de mest troliga fältnamnen för en läsbar beskrivning
        for key in ("Description", "Name", "Code"):
            value = first.get(key)
            if value and not str(value).isdigit():
                return str(value)
        return "Okänd"

    if isinstance(first, str) and not first.isdigit():
        return first

    return "Okänd"

def fetch_and_save_trains(hours_offset):
    now = datetime.now()
    time_from = (now - timedelta(hours=hours_offset)).strftime("%Y-%m-%dT%H:%M:%S")
    time_to = (now - timedelta(hours=hours_offset-4)).strftime("%Y-%m-%dT%H:%M:%S")
    
    print(f"Hämtar tåg för intervallet {time_from} till {time_to}...")
    try:
        response = requests.post(
            TRAFIKVERKET_URL, 
            data=build_train_chunk_query(time_from, time_to), 
            headers={'Content-Type': 'text/xml'},
            timeout=30
        )
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Nätverksfel vid hämtning av tåg ({time_from}): {e}")
        return

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
        train_type = extract_train_type(train.get('ProductInformation'))
        
        delay = None
        if sched_str and actual_str and not canceled:
            sched = datetime.fromisoformat(sched_str.replace("Z", "+00:00"))
            actual = datetime.fromisoformat(actual_str.replace("Z", "+00:00"))
            # round() istället för int(): int() trunkerar alltid mot noll
            # (4.9 min blir 4 istället för 5), vilket ger ett litet men
            # systematiskt bias nedåt över hela datasetet.
            delay = round((actual - sched).total_seconds() / 60)

        try:
            # traffic_density sparas som 0 här eftersom det korrekta värdet
            # kräver att se ALLA tåg vid stationen (även de som hämtas i
            # senare chunkar). Den riktiga beräkningen görs av
            # update_traffic_density.py, som körs efter hela hämtningen
            # är klar (se run_all.py). Kör INTE appen mot traffic_density
            # innan det skriptet körts, annars är värdet missvisande 0.
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
    """
    Hämtar fel och incidenter. Kollar 14 dagar bakåt.

    KÄND BEGRÄNSNING (2026-07): TrainMessage-objekttypen är borttagen ur
    Trafikverkets API - detta bekräftat genom uttömmande testning (alla
    schemaversioner, alla fält, med/utan filter ger samma "finns inte"-fel).
    RailwayEvent (efterträdaren, namnrymd ols.open) går heller inte att
    fråga mot v2/data.json-endpointen med någon testad variant. Tills det
    är löst (via Trafikverkets support eller konsolens egen testfunktion)
    kommer den här funktionen alltid misslyckas på ett kontrollerat sätt
    utan att krascha resten av kedjan. track_works fylls INTE på med ny
    data just nu - incident_type kommer vara "Ingen" för alla nya rader.
    """
    print("Hämtar rikstäckande signalfel och banarbeten...")
    time_from = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%S")
    
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
        
        if response.status_code != 200:
            print(f"⚠️ Varning: HTTP-fel från Trafikverket för TrainMessage ({response.status_code}).")
            print("ℹ️ Serverns svar:", response.text[:300])
            print("👉 Hoppar över banarbeten just nu – AI-modellen fortsätter träna på tåg och väder!")
            return

        data = response.json()
        
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
