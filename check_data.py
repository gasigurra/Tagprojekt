import sqlite3
import pandas as pd

conn = sqlite3.connect("train_predictions.db")
# Vi hämtar datan
df = pd.read_sql_query("SELECT * FROM train_observations WHERE canceled = 0", conn)

# 1. Analys av extrema fall (dina tidigare printar)
early = df[df['delay_minutes'] < -60].sort_values('delay_minutes')
print("--- Extrema tidiga tåg (>60 min tidiga) ---")
print(early[['train_number','station_signature','scheduled_arrival','actual_arrival','operator']])

late = df[df['delay_minutes'] > 100].sort_values('delay_minutes', ascending=False)
print("\n--- Extrema sena tåg (>100 min sena) ---")
print(late[['train_number','station_signature','scheduled_arrival','actual_arrival','operator']])

# 2. Gruppering baserat på om operatören är 'Okänd'
# Vi skapar en ny temporär kolumn för grupperingen
df['is_unknown'] = df['operator'] == 'Okänd'

# Här kör vi grupperingen som du ville ha den
print("\n--- Analys: Kända vs Okända operatörer ---")
operator_stats = df.groupby('is_unknown')['delay_minutes'].agg(['count', 'mean', 'std'])

# Vi döper om indexet för att göra det lättare att läsa
operator_stats.index = ['Kända operatörer', 'Okänd operatör']
print(operator_stats)

conn.close()