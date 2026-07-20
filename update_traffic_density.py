"""
update_traffic_density.py
----------------------------
LÖSER: train_observations.traffic_density sparas alltid som 0 vid
insamling (train_api.py) eftersom det korrekta värdet kräver att se
ALLA tåg vid en station, inklusive de i senare hämtningschunkar.
data_preparation.py räknar ut det korrekt, men bara för
träningspipelinen - app.py:s "Sök specifik avgång"-flik läste
tidigare alltid 0 direkt ur databasen (train/serving-skew).

Det här skriptet gör samma beräkning en gång för HELA
train_observations-tabellen och skriver tillbaka det riktiga värdet,
så att appens liveprediktioner matchar det modellen faktiskt lärt sig.

Kör detta EFTER train_api.py i pipelinen (se run_all.py) och/eller
manuellt när ni vill uppdatera en befintlig databas.
"""

import sqlite3

import pandas as pd

DATABASE_PATH = "train_predictions.db"


def update_traffic_density():
    conn = sqlite3.connect(DATABASE_PATH, timeout=15)

    df = pd.read_sql_query(
        "SELECT id, station_signature, scheduled_arrival, train_number "
        "FROM train_observations WHERE canceled = 0",
        conn,
    )

    if df.empty:
        print("Inga tåg i databasen - hoppar över uppdatering av trafiktäthet.")
        conn.close()
        return

    df['scheduled_utc'] = pd.to_datetime(df['scheduled_arrival'], utc=True)
    df = df.sort_values(by=['station_signature', 'scheduled_utc']).reset_index(drop=True)

    # Samma logik och samma säkerhetskontroll som data_preparation.py:
    # groupby(sort=False) + rolling bevarar radordningen inom varje grupp,
    # vilket gör den positionella återkopplingen till df säker - men vi
    # verifierar det ändå explicit istället för att bara lita på det.
    rolling_counts = (
        df.set_index('scheduled_utc')
        .groupby('station_signature', sort=False)['train_number']
        .rolling('15min', center=True).count()
        .reset_index(drop=True)
    )

    if len(rolling_counts) != len(df):
        raise RuntimeError(
            f"update_traffic_density: radantal matchar inte "
            f"({len(rolling_counts)} vs {len(df)}) - avbryter."
        )

    df['traffic_density'] = (rolling_counts - 1).clip(lower=0).fillna(0).astype(int)

    cursor = conn.cursor()
    updated = 0
    for row in df.itertuples(index=False):
        cursor.execute(
            "UPDATE train_observations SET traffic_density = ? WHERE id = ?",
            (int(row.traffic_density), int(row.id)),
        )
        updated += cursor.rowcount

    conn.commit()
    conn.close()
    print(f"✅ Uppdaterade traffic_density för {updated} tåg i train_observations.")


if __name__ == "__main__":
    update_traffic_density()
