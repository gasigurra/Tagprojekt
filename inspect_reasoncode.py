"""
inspect_reasoncode.py
----------------------
Diagnosskript: Hämtar en handfull TrainMessage-poster och skriver ut
hela strukturen (inkl. ReasonCode och TrafficImpact) så vi kan se
exakt vilka fält som finns innan vi bygger om track_works-tabellen.

Skriver ingenting till databasen - bara utforskande.
"""

import json
import os
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.environ.get("TRAFIKVERKET_API_KEY")
TRAFIKVERKET_URL = "https://api.trafikinfo.trafikverket.se/v2/data.json"


def build_query(time_from, limit=15):
    # Inga INCLUDE-taggar alls -> be APIet om ALLA fält som default.
    # Det gör att vi ser exakt vilka fältnamn som gäller för schemaversion 1.7
    # just nu, istället för att gissa (EventId gav "Invalid query attribute",
    # vilket tyder på att schemat ändrats sedan train_api.py skrevs).
    return f"""
    <REQUEST>
      <LOGIN authenticationkey="{API_KEY}" />
      <QUERY objecttype="TrainMessage" schemaversion="1.7" limit="{limit}">
        <FILTER><GT name="StartDateTime" value="{time_from}" /></FILTER>
      </QUERY>
    </REQUEST>
    """


def main():
    if not API_KEY:
        print("Saknar API-nyckel i .env!")
        return

    # Kolla senaste 3 dagarna - räcker för att se variation i meddelandetyper
    time_from = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")

    response = requests.post(
        TRAFIKVERKET_URL,
        data=build_query(time_from),
        headers={"Content-Type": "text/xml"},
    )

    if response.status_code != 200:
        print(f"HTTP-fel: {response.status_code}")
        print(response.text[:1000])
        return

    try:
        messages = response.json()["RESPONSE"]["RESULT"][0]["TrainMessage"]
    except KeyError as e:
        print("Kunde inte hitta TrainMessage i svaret:", e)
        print(json.dumps(response.json(), indent=2, ensure_ascii=False)[:2000])
        return

    if not messages:
        print("Inga meddelanden hittades för det här tidsfönstret. Testa ett bredare intervall.")
        return

    print(f"Hittade {len(messages)} meddelanden. Visar full struktur:\n")
    for m in messages:
        print(json.dumps(m, indent=2, ensure_ascii=False))
        print("-" * 60)


if __name__ == "__main__":
    main()