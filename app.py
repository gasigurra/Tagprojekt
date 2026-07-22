import streamlit as st
import pandas as pd
import sqlite3

from train_model import load_or_train_model
from feature_engineering import (
    get_join_hour,
    check_incident_type_live,
    compute_traffic_density_live,
    compute_previous_station_delay_live,
)

@st.cache_resource
def get_model():
    return load_or_train_model()

model = get_model()

st.title("🚆 Tågförseningar: AI-Prediktion (Rikstäckande)")
st.divider()

tab1, tab2 = st.tabs(["🚆 Sök specifik avgång", "🧮 Förseningskalkylator"])

with tab1:
    st.header("Sök på tågnummer")
    train_no = st.text_input("Tågnummer (t.ex. 924):")

    if st.button("🔮 Förutse försening", type="primary", use_container_width=True):
        if not train_no:
            st.warning("Vänligen ange ett tågnummer.")
        elif model is None:
            st.error("Kunde inte ladda modellen.")
        else:
            try:
                conn = sqlite3.connect("train_predictions.db")
                train_df = pd.read_sql_query(
                    "SELECT * FROM train_observations WHERE train_number = ? ORDER BY scheduled_arrival DESC LIMIT 1", 
                    conn, params=(train_no,)
                )

                if train_df.empty:
                    st.error(f"Hittade inget tåg med nummer {train_no}.")
                else:
                    station = train_df['station_signature'].iloc[0]
                    scheduled_utc = pd.to_datetime(train_df['scheduled_arrival'].iloc[0], utc=True)

                    st.info(f"**Tåg {train_no} (Station: {station})**")

                    # join_hour golvas till timmen (samma regel som träningen
                    # använder via feature_engineering.get_join_hour) - se
                    # den funktionen för varför golv och inte närmaste timme.
                    join_hour = get_join_hour(scheduled_utc)
                    weather_df = pd.read_sql_query(
                        "SELECT * FROM weather_observations WHERE station_signature = ?", 
                        conn, params=(station,)
                    )
                    
                    if not weather_df.empty:
                        weather_df['join_hour'] = pd.to_datetime(weather_df['timestamp_hour']).dt.tz_localize('UTC')
                        match = weather_df[weather_df['join_hour'] == join_hour]
                    else:
                        match = pd.DataFrame()

                    if match.empty:
                        st.warning("⚠️ Ingen vädermätning hittades för denna timme - använder schablonvärden, prediktionen kan vara mindre träffsäker.")
                        temp, wind, precip, snow = 15.0, 2.0, 0.0, 0.0
                    else:
                        temp = match['temperature'].iloc[0]
                        wind = match['wind_speed'].iloc[0]
                        precip = match['precipitation'].iloc[0]
                        snow = match['snow_depth'].iloc[0]

                    # traffic_density och previous_station_delay räknas LIVE
                    # med exakt samma definitioner som träningspipelinen
                    # använder (feature_engineering.py), istället för att
                    # läsas direkt ur databasen: den kolumnen för
                    # traffic_density kunde vara inaktuell/annorlunda
                    # definierad, och previous_station_delay skrevs aldrig
                    # dit av något skript och var därför alltid NULL.
                    traffic_density = compute_traffic_density_live(conn, station, scheduled_utc)
                    previous_station_delay = compute_previous_station_delay_live(conn, train_no, scheduled_utc)
                    is_single_track = train_df['is_single_track'].iloc[0]
                    train_type = train_df['train_type'].iloc[0] or "Okänt"
                    operator = train_df['operator'].iloc[0] or "Okänt"

                    incident_type = check_incident_type_live(conn, station, scheduled_utc)
                    if incident_type != "Ingen":
                        st.warning(f"⚠️ Aktiv störning ({incident_type})")

                    input_data = pd.DataFrame({
                        'station_signature': [station],
                        'temperature': [temp], 'wind_speed': [wind], 'precipitation': [precip], 'snow_depth': [snow],
                        'hour_of_day': [scheduled_utc.hour], 'day_of_week': [scheduled_utc.weekday()],
                        'traffic_density': [traffic_density], 'is_single_track': [is_single_track],
                        'incident_type': [incident_type], 'previous_station_delay': [previous_station_delay],
                        'train_type': [train_type], 'operator': [operator]
                    })

                    prediction = model.predict(input_data)[0]

                    st.subheader("📊 AI:ns bedömning:")
                    if prediction <= 0: 
                        st.success(f"Tåget förväntas vara i tid! ({prediction:.1f} min)")
                    elif prediction < 5: 
                        st.warning(f"Lätt försening förväntas. ({prediction:.1f} min)")
                    else: 
                        st.error(f"KRAFTIG FÖRSENING! ({prediction:.1f} minuter)")

            except Exception as e:
                st.error(f"Fel: {e}")
            finally:
                if 'conn' in locals(): 
                    conn.close()

with tab2:
    st.write("Justera värden!")
    col1, col2 = st.columns(2)

    with col1:
        temp = st.slider("Temperatur (°C)", -30.0, 40.0, 20.0)
        wind = st.slider("Vindhastighet (m/s)", 0.0, 35.0, 2.0)
        precip = st.number_input("Nederbörd (mm/h)", 0.0, 50.0, 0.0)
        snow = st.number_input("Snödjup (m)", 0.0, 2.0, 0.0)

    with col2:
        day_of_week = st.selectbox("Veckodag (0=Mån, 6=Sön)", [0, 1, 2, 3, 4, 5, 6])
        hour_of_day = st.slider("Klockslag", 0, 23, 8)

    st.divider()
    col3, col4 = st.columns(2)

    with col3:
        station_signature = st.text_input("Stationssignatur (t.ex. Cst, G, M)", "Cst")
        traffic_density = st.slider("Trafiktäthet", 0, 15, 1)
        is_single_track = st.checkbox("Enkelspår")

    with col4:
        incident_type = st.selectbox("Störningstyp", ["Ingen", "Banarbete", "Signalfel", "Olycka", "Fordonsfel"])
        train_type = st.selectbox("Tågtyp", ["Pendeltåg", "Snabbtåg", "Okänt"])
        operator = st.selectbox("Operatör", ["SJ", "Mälartåg", "Okänt"])
        previous_station_delay = st.slider("Försening förra station (min)", -10, 60, 0)

    if st.button("🔮 Förutse", use_container_width=True):
        if model:
            input_data = pd.DataFrame({
                'station_signature': [station_signature],
                'temperature': [temp], 'wind_speed': [wind], 'precipitation': [precip], 'snow_depth': [snow],
                'hour_of_day': [hour_of_day], 'day_of_week': [day_of_week],
                'traffic_density': [traffic_density], 'is_single_track': [int(is_single_track)],
                'incident_type': [incident_type], 'previous_station_delay': [previous_station_delay],
                'train_type': [train_type], 'operator': [operator]
            })
            pred = model.predict(input_data)[0]
            if pred <= 0: 
                st.success(f"Tid! ({pred:.1f} min)")
            elif pred < 5: 
                st.warning(f"Lätt försening ({pred:.1f} min)")
            else: 
                st.error(f"KRAFTIG FÖRSENING! ({pred:.1f} min)")