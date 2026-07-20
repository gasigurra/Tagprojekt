"""
migrate_add_index.py
----------------------
Engångsskript för BEFINTLIGA databaser (skapade innan idx_train_number
fanns med i database_setup.py). Kör INTE database_setup.py på en
databas ni redan samlat in data i - det raderar allt. Kör det här
skriptet istället, det bara lägger till ett saknat index.

Säkert att köra flera gånger (CREATE INDEX IF NOT EXISTS).
"""

import sqlite3

DATABASE_PATH = "train_predictions.db"


def migrate():
    conn = sqlite3.connect(DATABASE_PATH, timeout=15)
    cursor = conn.cursor()

    cursor.execute("CREATE INDEX IF NOT EXISTS idx_train_number ON train_observations(train_number);")
    conn.commit()
    conn.close()
    print("✅ Index idx_train_number finns nu på train_observations(train_number).")


if __name__ == "__main__":
    migrate()
