"""
update_traffic_density.py
----------------------------
BAKGRUND: train_observations.traffic_density sparas alltid som 0 vid
insamling (train_api.py) eftersom det korrekta värdet kräver att se
ALLA tåg vid en station, inklusive de i senare hämtningschunkar.

UPPDATERAD (se feature_engineering.py): appen läser INTE längre den här
kolumnen för sina liveprediktioner - den räknar traffic_density live via
feature_engineering.compute_traffic_density_live(), som garanterat
använder samma fönsterdefinition som träningen. Anledningen: det här
skriptet använde tidigare ett centrerat fönster (center=True, dvs. det
räknade även tåg som anlände EFTER det aktuella tåget), trots att
docstringen påstod att det var "samma logik" som träningens
closed='left'-fönster (bara historiska tåg). Det var alltså en egen,
tredje definition - inte den fix för train/serving-skew den utgav sig
för att vara.

Det här skriptet finns kvar för att hålla den PERSISTERADE kolumnen
korrekt för den som vill fråga databasen direkt (manuell analys,
dashboards etc.) - men varken app.py eller träningspipelinen (som räknar
om trafiktäthet från grunden i data_preparation.py oavsett) är längre
beroende av att detta skript körs.

Kör EFTER train_api.py i pipelinen (se run_all.py) och/eller manuellt när
ni vill uppdatera en befintlig databas.
"""

import sqlite3

import pandas as pd

from feature_engineering import TRAFFIC_WINDOW_MINUTES

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

    # closed='left': samma halvöppna fönster [t-15min, t) som
    # data_preparation.add_accurate_traffic_density använder vid träning -
    # bara tåg som redan anlänt räknas, tåget självt räknas inte med.
    rolling_counts = (
        df.set_index('scheduled_utc')
        .groupby('station_signature', sort=False)['train_number']
        .rolling(f'{TRAFFIC_WINDOW_MINUTES}min', closed='left').count()
        .reset_index(drop=True)
    )

    if len(rolling_counts) != len(df):
        raise RuntimeError(
            f"update_traffic_density: radantal matchar inte "
            f"({len(rolling_counts)} vs {len(df)}) - avbryter."
        )

    df['traffic_density'] = rolling_counts.fillna(0).astype(int)

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