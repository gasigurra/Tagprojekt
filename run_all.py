import os
import subprocess
import sys
import time


def run_script(script_name, description):
    """Kör ett skript och väntar tills det är 100% klart innan nästa startar."""
    print(f"\n▶️  STARTAR: {description} ({script_name})...")
    start_t = time.time()

    # Denna rad låser Python: Inget annat händer förrän skriptet är helt färdigt
    result = subprocess.run([sys.executable, script_name])

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

    # STEG 2: API & Datahämtning
    run_script("smhi_fetcher.py", "3. Hämtar senaste vädret från SMHI")
    run_script(
        "train_api.py", "4. Hämtar tåg & trafikstörningar från Trafikverket"
    )

    # --- HÄR ÄR DEN NYA RADEN SOM KÖR MODELLEN SYNLTIGT ---
    run_script(
        "train_model.py", "5. Tränar och utvärderar AI-modellen (MSE & MAE)"
    )
    # ------------------------------------------------------

    total_time = round(time.time() - total_start, 1)
    print("\n--------------------------------------------------")
    print(f"🎉 All data är nedladdad och sparad! (Totaltid: {total_time}s)")
    print("🚀 STEG 3: Startar Streamlit-hemsidan...")
    print("--------------------------------------------------\n")

    # STEG 3: Appen (Startar absolut sist!)
    try:
        subprocess.run([sys.executable, "-m", "streamlit", "run", "app.py"])
    except KeyboardInterrupt:
        print("\n🛑 Streamlit stängdes ner.")


if __name__ == "__main__":
    main()