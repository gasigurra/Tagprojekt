import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from data_preparation import load_and_prepare_split_data

NUMERIC_FEATURES = [
    'temperature', 'wind_speed', 'precipitation', 'snow_depth',
    'hour_of_day', 'day_of_week',
    'traffic_density', 'is_single_track', 'previous_station_delay',
]

CATEGORICAL_FEATURES = ['station_signature', 'train_type', 'operator', 'incident_type']

def train_and_evaluate_model():
    print("1. Laddar och gör en vattentät kronologisk split...")
    df_train, df_test = load_and_prepare_split_data(split_ratio=0.8)

    if df_train is None or df_test is None:
        return None

    for df in [df_train, df_test]:
        df['hour_of_day'] = df['scheduled_utc'].dt.hour
        df['day_of_week'] = df['scheduled_utc'].dt.weekday

    y_train = df_train['delay_minutes']
    X_train = df_train[NUMERIC_FEATURES + CATEGORICAL_FEATURES]

    y_test = df_test['delay_minutes']
    X_test = df_test[NUMERIC_FEATURES + CATEGORICAL_FEATURES]

    preprocessor = ColumnTransformer(
        transformers=[
            ('cat', OneHotEncoder(handle_unknown='ignore'), CATEGORICAL_FEATURES),
        ],
        remainder='passthrough',
    )

    model = Pipeline(steps=[
        ('preprocessor', preprocessor),
        ('regressor', RandomForestRegressor(n_estimators=100, random_state=42, n_jobs=-1)),
    ])

    print("2. Tränar modellen enbart på träningsdatan...")
    model.fit(X_train, y_train)

    print("3. Utvärderar på den helt oberoende testdatan...")
    predictions = model.predict(X_test)
    
    mae = mean_absolute_error(y_test, predictions)
    mse = mean_squared_error(y_test, predictions)
    print(f"\n✅ MODELLENS MSE: {mse:.2f} | MAE: {mae:.2f} minuter")

    return model

if __name__ == "__main__":
    trained_model = train_and_evaluate_model()