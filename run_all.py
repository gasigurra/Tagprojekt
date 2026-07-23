import os
import subprocess
import sys
import time


def run_script(script_name, description):
    """Kör ett skript och väntar tills det är 100% klart innan nästa startar."""
    print(f"\n▶️  STARTAR: {description} ({script_name})...")
    start_t = time.time()

    # Denna rad låser Python: Inget annat händer förrän skriptet är helt färdigt
    # -u: ovillkorligt oframskjuten stdout. Utan den kan print()-utskrifter
    # från skriptet fastna i en buffert och aldrig synas i terminalen förrän
    # processen avslutas (särskilt vanligt genom nästlade subprocess-anrop
    # på Windows) - det är därför MSE/MAE/feature-importance-utskriften från
    # train_model.py kunde se ut att "saknas" trots att koden kör den.
    result = subprocess.run([sys.executable, "-u", script_name])

    if result.returncode != 0:
        print(f"❌ FEL: {script_name} kraschade! Avbryter kedjan.")
        sys.exit(1)  # Avbryt hela programmet så inte hemsidan startar med trasig data

    elapsed = round(time.time() - start_t, 1)
    print(f"✅ KLAR: {description} (tog {elapsed}s)")


def main():
    print("🚂 =================================================")
    print("     STARTAR TÅGPROJEKTET - STRIKT ORDNING")
    print("==================================================\n")

    total_start = time.time()

    # STEG 1: Databasen
    # Vi kollar först om kartan (stations_mapping) redan är byggd i databasen
    db_exists = os.path.exists("train_predictions.db")

    if not db_exists:
        print("⚠️ Ingen databas hittades. Bygger upp allt från grunden!")
        run_script("database_setup.py", "1. Skapar tomma databastabeller")
        run_script(
            "build_mapping.py", "2. Bygger Sverigekartan (SMHI <-> Trafikverket)"
        )
    else:
        print("ℹ️ Databas och karta finns redan. Hoppar över nollställning.")
        # Säkerställer att befintliga databaser (skapade innan idx_train_number
        # fanns med) ändå får det viktiga indexet, utan att röra någon data.
        run_script("migrate_add_index.py", "1b. Säkerställer att index finns (ofarligt)")

    # STEG 2: API & Datahämtning
    run_script("smhi_fetcher.py", "3. Hämtar senaste vädret från SMHI")
    run_script(
        "train_api.py", "4. Hämtar tåg & trafikstörningar från Trafikverket"
    )

    # STEG 2b: Rätta trafiktäthet retroaktivt
    # train_api.py sparar alltid 0 i traffic_density (det korrekta värdet
    # kräver att se ALLA tåg vid en station, inklusive de i senare chunkar).
    # OBS: app.py läser INTE längre denna kolumn - den räknar traffic_density
    # live via feature_engineering.py, garanterat med samma definition som
    # träningen. Det här steget håller bara den sparade kolumnen korrekt för
    # den som vill fråga databasen direkt (manuell analys, dashboards).
    run_script(
        "update_traffic_density.py", "4b. Räknar om trafiktäthet retroaktivt (för manuell analys - påverkar inte appens prediktioner)"
    )

    total_time = round(time.time() - total_start, 1)
    print("\n--------------------------------------------------")
    print(f"🎉 All data är nedladdad och sparad! (Totaltid: {total_time}s)")
    print("🚀 STEG 3: Startar AI-modellen och Streamlit-hemsidan...")
    print("--------------------------------------------------\n")

    # STEG 3: Appen (Startar absolut sist!)
    try:
        subprocess.run([sys.executable, "-u", "-m", "streamlit", "run", "app.py"])
    except KeyboardInterrupt:
        print("\n🛑 Streamlit stängdes ner.")


if __name__ == "__main__":
    main()