import hashlib
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
    """Begär ALLA tåg i landet under en viss tidsram."""
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
    except (KeyError, ValueError, IndexError): 
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
    """Hämtar störningar, banarbeten och signalfel från Trafikverkets Situation-objekt."""
    print("Hämtar rikstäckande störningar (Situation) från Trafikverket...")
    time_from = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%S")

    # OBS: både Situation.Id och Deviation.Id har nu bekräftats vara ogiltiga
    # fält (två separata 400-fel, verifierade mot riktiga svar). Snarare än
    # att fortsätta gissa fältnamn ett i taget bygger vi ett eget stabilt
    # ID från Header+StartTime nedan (se hash-koden längre ner) istället för
    # att be Trafikverket om ett Id-fält alls.
    query = f"""
    <REQUEST>
      <LOGIN authenticationkey="{API_KEY}" />
      <QUERY objecttype="Situation" schemaversion="1.5">
        <FILTER>
          <GT name="Deviation.StartTime" value="{time_from}" />
        </FILTER>
        <INCLUDE>Deviation.Header</INCLUDE>
        <INCLUDE>Deviation.MessageType</INCLUDE>
        <INCLUDE>Deviation.AffectedLocation</INCLUDE>
        <INCLUDE>Deviation.StartTime</INCLUDE>
        <INCLUDE>Deviation.EndTime</INCLUDE>
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
            print(f"⚠️ HTTP-fel ({response.status_code}) vid hämtning av störningar.")
            print(f"   Svar från Trafikverket: {response.text[:1000]}")
            return

        data = response.json()
        situations = data.get('RESPONSE', {}).get('RESULT', [{}])[0].get('Situation', [])

    except Exception as e:
        print(f"⚠️ Kunde inte tolka störningar: {e}")
        return

    inserted = 0
    with sqlite3.connect(DATABASE_PATH, timeout=15) as conn:
        cursor = conn.cursor()

        for sit in situations:
            deviations = sit.get('Deviation', [])
            if isinstance(deviations, dict):
                deviations = [deviations]

            for dev in deviations:
                affected_raw = dev.get('AffectedLocation', [])
                if isinstance(affected_raw, str):
                    affected_stations = [affected_raw]
                elif isinstance(affected_raw, list):
                    affected_stations = [
                        loc.get('LocationSignature', loc) if isinstance(loc, dict) else loc 
                        for loc in affected_raw
                    ]
                else:
                    affected_stations = []

                start_time = dev.get('StartTime')
                end_time = dev.get('EndTime')
                severity_level = dev.get('MessageType', dev.get('Header', 'Störning'))
                header = dev.get('Header', '')

                # Trafikverket ger oss inget användbart Id-fält (se kommentar
                # vid frågan ovan), så vi bygger ett eget som är STABILT över
                # upprepade körningar - annars skulle UNIQUE(incident_id,
                # affected_station) inte kunna deduplicera, och samma
                # störning skulle sparas som "ny" varje gång pipelinen körs.
                incident_id = hashlib.md5(f"{header}|{start_time}".encode("utf-8")).hexdigest()[:16]

                for station in affected_stations:
                    try:
                        cursor.execute('''
                            INSERT OR IGNORE INTO track_works
                            (incident_id, affected_station, start_time, end_time, severity_level)
                            VALUES (?, ?, ?, ?, ?)
                        ''', (incident_id, station, start_time, end_time, severity_level))
                        if cursor.rowcount > 0:
                            inserted += 1
                    except sqlite3.Error:
                        pass

    print(f"  -> Sparade {inserted} nya störningar i databasen.")

if __name__ == '__main__':
    if not API_KEY:
        print("Saknar API-nyckel i .env!")
    else:
        # Hämtar de senaste 24 timmarna (6 chunks om 4h) istället för 14 dagar
        for offset in range(24, 0, -4):
            fetch_and_save_trains(offset)
            time.sleep(0.2)
        
        time.sleep(0.2)
        fetch_messages()