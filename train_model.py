import json
import os
from datetime import datetime, timezone

import joblib
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.compose import ColumnTransformer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from data_preparation import load_and_prepare_split_data
from feature_engineering import FEATURE_ENGINEERING_VERSION

NUMERIC_FEATURES = [
    'temperature', 'wind_speed', 'precipitation', 'snow_depth',
    'hour_of_day', 'day_of_week',
    'traffic_density', 'is_single_track', 'previous_station_delay',
]

CATEGORICAL_FEATURES = ['station_signature', 'train_type', 'operator', 'incident_type']

MODEL_PATH = "model.joblib"
METADATA_PATH = "model_metadata.json"


def _baseline_mae(y_train, y_test):
    """Naiv baslinje: förutsäg alltid träningsdatans genomsnittliga
    försening. Om RandomForest-modellen inte slår den här baslinjen
    tydligt är den inte värd att lita på, oavsett hur bra MAE/MSE
    "låter" i isolation."""
    mean_pred = y_train.mean()
    baseline_predictions = pd.Series(mean_pred, index=y_test.index)
    return mean_absolute_error(y_test, baseline_predictions)


def _print_feature_importances(model, top_n=15):
    """Skriver ut de features RandomForest faktiskt använder mest.
    Bra för att upptäcka döda features (som is_single_track, som just nu
    aldrig sätts av någon insamlingsscript och därför borde hamna nära 0
    här - om den gör det, är det en signal att antingen implementera den
    riktigt eller ta bort den från modellen)."""
    try:
        feature_names = model.named_steps['preprocessor'].get_feature_names_out()
        importances = model.named_steps['regressor'].feature_importances_
    except AttributeError:
        print("(Kunde inte extrahera feature-namn/importances för denna modelltyp.)")
        return

    imp_df = pd.DataFrame({'feature': feature_names, 'importance': importances})
    imp_df = imp_df.sort_values('importance', ascending=False).head(top_n)

    print(f"\n📊 Viktigaste features (topp {top_n}):")
    for _, row in imp_df.iterrows():
        print(f"   {row['feature']:<35} {row['importance']:.4f}")


def train_and_evaluate_model(save=True):
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
        verbose_feature_names_out=False,
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
    baseline_mae = _baseline_mae(y_train, y_test)

    print(f"\n   Baslinje (förutsäger alltid träningsdatans medelförsening): MAE {baseline_mae:.2f} min")
    print(f"✅ MODELLENS MSE: {mse:.2f} | MAE: {mae:.2f} minuter")
    if mae >= baseline_mae:
        print("⚠️  VARNING: Modellen slår INTE den naiva baslinjen. Något är sannolikt fel "
              "i features eller träning - lita inte på den här modellen ännu.")
    else:
        improvement = 100 * (1 - mae / baseline_mae) if baseline_mae else 0
        print(f"   -> {improvement:.0f}% bättre MAE än baslinjen.")

    _print_feature_importances(model)

    if save:
        joblib.dump(model, MODEL_PATH)
        metadata = {
            "trained_at_utc": datetime.now(timezone.utc).isoformat(),
            "feature_engineering_version": FEATURE_ENGINEERING_VERSION,
            "mae_minutes": round(float(mae), 3),
            "mse": round(float(mse), 3),
            "baseline_mae_minutes": round(float(baseline_mae), 3),
            "n_train_rows": int(len(df_train)),
            "n_test_rows": int(len(df_test)),
            "numeric_features": NUMERIC_FEATURES,
            "categorical_features": CATEGORICAL_FEATURES,
        }
        with open(METADATA_PATH, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        print(f"\n💾 Modell sparad till {MODEL_PATH} (metadata i {METADATA_PATH}).")

    return model


def load_or_train_model():
    """Används av app.py. Laddar en sparad modell från disk om den finns
    OCH matchar den feature-logik som körs just nu (FEATURE_ENGINEERING_VERSION) -
    annars tränas en ny modell från grunden.

    Det här löser ett separat problem från cachningen i app.py
    (@st.cache_resource, som bara håller modellen i minnet under en
    körande process): utan den här funktionen tränades RandomForest om
    från noll varje gång appen/processen startade om, vilket både är
    onödigt långsamt och gör det omöjligt att "frysa" en modellversion
    för jämförelse."""
    if os.path.exists(MODEL_PATH) and os.path.exists(METADATA_PATH):
        try:
            with open(METADATA_PATH, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            if metadata.get("feature_engineering_version") != FEATURE_ENGINEERING_VERSION:
                print("ℹ️ Sparad modell använder en gammal feature-definition "
                      f"(v{metadata.get('feature_engineering_version')} != v{FEATURE_ENGINEERING_VERSION}). "
                      "Tränar om.")
            else:
                print(f"📦 Laddar sparad modell från {MODEL_PATH} "
                      f"(tränad {metadata.get('trained_at_utc')}, MAE {metadata.get('mae_minutes')} min).")
                return joblib.load(MODEL_PATH)
        except Exception as e:
            print(f"⚠️ Kunde inte ladda sparad modell ({e}). Tränar om.")

    return train_and_evaluate_model()


if __name__ == "__main__":
    trained_model = train_and_evaluate_model()