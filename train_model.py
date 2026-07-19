import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from data_preparation import load_and_prepare_data

NUMERIC_FEATURES = [
    'temperature', 'wind_speed', 'precipitation', 'snow_depth',
    'hour_of_day', 'day_of_week',
    'traffic_density', 'is_single_track', 'previous_station_delay',
]

CATEGORICAL_FEATURES = ['train_type', 'operator', 'incident_type']

def train_and_evaluate_model():
    print("1. Laddar och tvättar data...")
    df = load_and_prepare_data()

    if df is None or len(df) == 0:
        return None

    # --- 1. Sortera kronologiskt för att undvika dataläckage ---
    df = df.sort_values(by='join_hour').reset_index(drop=True)
    # -----------------------------------------------------------

    df['hour_of_day'] = df['join_hour'].dt.hour
    df['day_of_week'] = df['join_hour'].dt.weekday
    df = df.drop(columns=['join_hour'])

    y = df['delay_minutes']
    X = df[NUMERIC_FEATURES + CATEGORICAL_FEATURES]

    # --- 2. Kronologisk uppdelning (80 % träningsdata, 20 % testdata) ---
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, shuffle=False
    )
    # ------------------------------------------------------------------

    preprocessor = ColumnTransformer(
        transformers=[
            ('cat', OneHotEncoder(handle_unknown='ignore'), CATEGORICAL_FEATURES),
        ],
        remainder='passthrough',
    )

    # Vi behåller alla processorkärnor igång med n_jobs=-1 för snabbare träning
    model = Pipeline(steps=[
        ('preprocessor', preprocessor),
        ('regressor', RandomForestRegressor(n_estimators=100, random_state=42, n_jobs=-1)),
    ])

    print("2. Tränar modellen på historisk data (de första 80 %)...")
    model.fit(X_train, y_train)

    # --- 3. Beräkna och skriv ut BÅDE MSE och MAE på okänd testdata ---
    predictions = model.predict(X_test)
    
    mae = mean_absolute_error(y_test, predictions)
    mse = mean_squared_error(y_test, predictions)
    print(f"✅ MODELLENS MSE: {mse:.2f} | MAE: {mae:.2f} minuter (på okänd testdata)")
    # ------------------------------------------------------------------

    # Returnerar modellen tränad enbart på 80 % av datan
    return model

if __name__ == "__main__":
    trained_model = train_and_evaluate_model()