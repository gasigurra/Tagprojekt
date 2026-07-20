import streamlit as st
import pandas as pd
import sqlite3

from train_model import train_and_evaluate_model

DATABASE_PATH = "train_predictions.db"


@st.cache_resource
def get_model():
    return train_and_evaluate_model()


@st.cache_data(ttl=3600)
def get_dropdown_options():
    """
    Hämtar faktiska värden ur databasen istället för hårdkodade gissningar.
    De hårdkodade listorna som fanns tidigare (t.ex. "Mälartåg", "Signalfel")
    matchade inte de riktiga kategorierna modellen tränats på - att välja
    ett värde som modellen aldrig sett gör att OneHotEncoder tyst ignorerar
    valet (handle_unknown='ignore'), vilket ger användaren en falsk känsla
    av att deras val påverkar prediktionen.
    """
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        operators = pd.read_sql_query(
            "SELECT DISTINCT operator FROM train_observations "
            "WHERE operator IS NOT NULL AND operator != 'Okänd' "
            "ORDER BY operator",
            conn,
        )['operator'].tolist()

        train_types = pd.read_sql_query(
            "SELECT DISTINCT train_type FROM train_observations "
            "WHERE train_type IS NOT NULL AND train_type != 'Okänd' "
            "ORDER BY train_type",
            conn,
        )['train_type'].tolist()

        incident_types = pd.read_sql_query(
            "SELECT DISTINCT severity_level FROM track_works "
            "WHERE severity_level IS NOT NULL "
            "ORDER BY severity_level",
            conn,
        )['severity_level'].tolist()
    finally:
        conn.close()

    # Säkra fallbacks om databasen är tom eller nystartad
    if not operators:
        operators = ["Okänt"]
    if not train_types:
        train_types = ["Okänt"]

    incident_types = ["Ingen"] + [t for t in incident_types if t != "Ingen"]

    return operators, train_types, incident_types


def check_has_incident(conn, station_signature, arrival_time_utc):
    """Kollar vilken olycka (text) som pågick live vid stationen."""
    df_incidents = pd.read_sql_query(
        "SELECT * FROM track_works WHERE affected_station = ?", conn, params=(station_signature,)
    )
    if df_incidents.empty: return "Ingen"

    df_incidents['start_time'] = pd.to_datetime(df_incidents['start_time'], utc=True)
    df_incidents['end_time'] = pd.to_datetime(df_incidents['end_time'], utc=True)

    active = df_incidents[
        (df_incidents['start_time'] <= arrival_time_utc) & 
        (df_incidents['end_time'].isna() | (df_incidents['end_time'] >= arrival_time_utc))
    ]
    if not active.empty:
        return active['severity_level'].iloc[0]
    return "Ingen"

model = get_model()
KNOWN_OPERATORS, KNOWN_TRAIN_TYPES, KNOWN_INCIDENT_TYPES = get_dropdown_options()

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
                conn = sqlite3.connect(DATABASE_PATH)
                train_df = pd.read_sql_query("SELECT * FROM train_observations WHERE train_number = ? ORDER BY scheduled_arrival DESC LIMIT 1", conn, params=(train_no,))

                if train_df.empty:
                    st.error(f"Hittade inget tåg med nummer {train_no}.")
                else:
                    station = train_df['station_signature'].iloc[0]
                    scheduled_utc = pd.to_datetime(train_df['scheduled_arrival'].iloc[0], utc=True)

                    st.info(f"**Tåg {train_no} (Station: {station})**")

                    join_hour = scheduled_utc.floor('h')
                    weather_df = pd.read_sql_query("SELECT * FROM weather_observations WHERE station_signature = ?", conn, params=(station,))
                    
                    if not weather_df.empty:
                        weather_df['join_hour'] = pd.to_datetime(weather_df['timestamp_hour']).dt.tz_localize('UTC')
                        match = weather_df[weather_df['join_hour'] == join_hour]
                    else:
                        match = pd.DataFrame()

                    if match.empty:
                        temp, wind, precip, snow = 15.0, 2.0, 0.0, 0.0
                    else:
                        # OBS: match kan innehålla en rad även om enskilda
                        # kolumner är NULL (varje vädertyp hämtas separat i
                        # smhi_fetcher.py). RandomForest accepterar inte NaN,
                        # så vi faller tillbaka per fält istället för att
                        # bara kolla om hela raden saknas.
                        temp = match['temperature'].iloc[0]
                        wind = match['wind_speed'].iloc[0]
                        precip = match['precipitation'].iloc[0]
                        snow = match['snow_depth'].iloc[0]

                        if pd.isna(temp): temp = 15.0
                        if pd.isna(wind): wind = 2.0
                        if pd.isna(precip): precip = 0.0
                        if pd.isna(snow): snow = 0.0

                    # OBS: traffic_density kommer bara vara korrekt om
                    # update_traffic_density.py körts efter senaste
                    # train_api.py-hämtningen (se run_all.py). Annars är
                    # värdet 0 för alla nya tåg.
                    # OBS: 'x or 0' fångar INTE NaN (NaN är truthy i Python),
                    # bara None/0. Nullable INTEGER-kolumner kan komma tillbaka
                    # som NaN via pandas om kolumnen har blandade NULL/int-värden.
                    traffic_density = train_df['traffic_density'].iloc[0]
                    if pd.isna(traffic_density): traffic_density = 0

                    is_single_track = train_df['is_single_track'].iloc[0]
                    if pd.isna(is_single_track): is_single_track = 0

                    train_type = train_df['train_type'].iloc[0]
                    if pd.isna(train_type) or not train_type: train_type = "Okänt"

                    operator = train_df['operator'].iloc[0]
                    if pd.isna(operator) or not operator: operator = "Okänt"

                    previous_station_delay = train_df['previous_station_delay'].iloc[0]
                    if pd.isna(previous_station_delay): previous_station_delay = 0
                    
                    incident_type = check_has_incident(conn, station, scheduled_utc)
                    if incident_type != "Ingen":
                        st.warning(f"⚠️ Aktiv störning ({incident_type})")

                    input_data = pd.DataFrame({
                        'temperature': [temp], 'wind_speed': [wind], 'precipitation': [precip], 'snow_depth': [snow],
                        'hour_of_day': [scheduled_utc.hour], 'day_of_week': [scheduled_utc.weekday()],
                        'traffic_density': [traffic_density], 'is_single_track': [is_single_track],
                        'incident_type': [incident_type], 'previous_station_delay': [previous_station_delay],
                        'train_type': [train_type], 'operator': [operator]
                    })

                    prediction = model.predict(input_data)[0]

                    st.subheader("📊 AI:ns bedömning:")
                    if prediction <= 0: st.success(f"Tåget förväntas vara i tid! ({prediction:.1f} min)")
                    elif prediction < 5: st.warning(f"Lätt försening förväntas. ({prediction:.1f} min)")
                    else: st.error(f"KRAFTIG FÖRSENING! ({prediction:.1f} minuter)")

            except Exception as e:
                st.error(f"Fel: {e}")
            finally:
                if 'conn' in locals(): conn.close()

with tab2:
    st.write("Justera världen!")
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
        traffic_density = st.slider("Trafiktäthet", 0, 15, 1)
        is_single_track = st.checkbox("Enkelspår")
        incident_type = st.selectbox("Störningstyp", KNOWN_INCIDENT_TYPES)

    with col4:
        train_type = st.selectbox("Tågtyp", KNOWN_TRAIN_TYPES)
        operator = st.selectbox("Operatör", KNOWN_OPERATORS)
        previous_station_delay = st.slider("Försening förra station (min)", -10, 60, 0)

    if st.button("🔮 Förutse", use_container_width=True):
        if model:
            input_data = pd.DataFrame({
                'temperature': [temp], 'wind_speed': [wind], 'precipitation': [precip], 'snow_depth': [snow],
                'hour_of_day': [hour_of_day], 'day_of_week': [day_of_week],
                'traffic_density': [traffic_density], 'is_single_track': [int(is_single_track)],
                'incident_type': [incident_type], 'previous_station_delay': [previous_station_delay],
                'train_type': [train_type], 'operator': [operator]
            })
            pred = model.predict(input_data)[0]
            if pred <= 0: st.success(f"Tid! ({pred:.1f} min)")
            elif pred < 5: st.warning(f"Lätt försening ({pred:.1f} min)")
            else: st.error(f"KRAFTIG FÖRSENING! ({pred:.1f} min)")