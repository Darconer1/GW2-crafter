#!/usr/bin/env python3
"""
GW2 Preis-Update Script
Lädt Preise von der GW2 API und speichert sie in der Datenbank
"""

import requests
import json
import sqlite3
import os
from datetime import datetime

# Item IDs (aus App.py)
COOLDOWN_IDS = {
    "Deldrimor-Stahlbarren": 46738,
    "Elonischer Lederquadrat": 46739,
    "Chiffon-Ballen": 46740,
    "Geistreichen-Holzplanke": 46741
}

RAW_MAT_IDS = {
    "Mithrilerz": 19684,
    "Eisenerz": 19697,
    "Platinerz": 19702,
    "Dicker Lederabschnitt": 19728,
    "Dünner Lederabschnitt": 19718,
    "Grober Lederabschnitt": 19719,
    "Rauher Lederabschnitt": 19725,
    "Seidenrest": 19748,
    "Wollrest": 19739,
    "Baumwollrest": 19741,
    "Leinenrest": 19743,
    "Altes Holzblock": 19722,
    "Geschmeidiges Holzblock": 19710,
    "Abgelagertes Holzblock": 19709,
    "Hartes Holzblock": 19713
}

MF_MATERIAL_PARE = {
    "Blut": {"t5": 24294, "t6": 24295, "name": "Kraftvolles Blut"},
    "Knochen": {"t5": 24341, "t6": 24358, "name": "Antiker Knochen"},
    "Klaue": {"t5": 24350, "t6": 24351, "name": "Scheußliche Klaue"},
    "Fangzahn": {"t5": 24276, "t6": 24271, "name": "Scheußlicher Fangzahn"},
    "Schuppe": {"t5": 24283, "t6": 24289, "name": "Gepanzerte Schuppe"},
    "Giftbeutel": {"t5": 24277, "t6": 24280, "name": "Wirksamer Giftbeutel"},
    "Totem": {"t5": 24299, "t6": 24300, "name": "Verziertes Totem"},
    "Staub": {"t5": 24274, "t6": 24275, "name": "Kristalliner Staub"}
}

ECTO_ID = 19721
ENCRYPTION_ID = 75919

# Alle Item IDs sammeln
ALL_IDS = list(COOLDOWN_IDS.values()) + list(RAW_MAT_IDS.values()) + [ECTO_ID, ENCRYPTION_ID]
for p in MF_MATERIAL_PARE.values():
    ALL_IDS.extend([p["t5"], p["t6"]])
ALL_IDS = list(set(ALL_IDS))

DB_FILE = "price_history.db"
HISTORY_FILE = "price_history.json"

def init_db():
    """Initialisiert die Datenbank"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS prices (
        item_id INTEGER,
        timestamp TEXT,
        sell INTEGER,
        buy INTEGER
    )
    """)
    conn.commit()
    conn.close()
    print("✅ Datenbank initialisiert")

def log_prices_to_db(item_id, sell, buy):
    """Speichert Preise in der Datenbank"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO prices (item_id, timestamp, sell, buy) VALUES (?, ?, ?, ?)",
              (int(item_id), datetime.now().isoformat(), int(sell or 0), int(buy or 0)))
    # Alte Einträge (älter als 120 Tage) löschen
    c.execute("DELETE FROM prices WHERE timestamp < datetime('now','-120 days')")
    conn.commit()
    conn.close()

def fetch_live_prices(item_ids):
    """Ruft Live-Preise von der GW2 API ab"""
    if not item_ids:
        return {}
    
    ids_str = ",".join(map(str, item_ids))
    url = f"https://api.guildwars2.com/v2/commerce/prices?ids={ids_str}"
    headers = {"User-Agent": "GW2-Crafter-UpdateBot/1.0"}
    
    try:
        print(f"📡 Rufe GW2 API auf: {len(item_ids)} Items...")
        response = requests.get(url, headers=headers, timeout=15)
        
        if response.status_code in [200, 206]:
            data = response.json()
            print(f"✅ API erfolgreich: {len(data)} Items geladen")
            return {int(item["id"]): item for item in data}
        else:
            print(f"❌ API Fehler: Status {response.status_code}")
            return {}
    except Exception as e:
        print(f"❌ Verbindungsfehler: {e}")
        return {}

def update_price_history_json(live_data):
    """Aktualisiert die JSON-Datei mit neuen Preisen"""
    try:
        # Existierende Historie laden
        history = {}
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:  # Nur laden wenn nicht leer
                        history = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                print("⚠️ Beschädigte JSON-Datei - starte neu")
                history = {}
        
        # Neue Preise hinzufügen
        for item_id, item_data in live_data.items():
            str_id = str(item_id)
            if str_id not in history:
                history[str_id] = {"name": "", "data": []}
            
            sell_price = item_data.get("sells", {}).get("unit_price", 0)
            buy_price = item_data.get("buys", {}).get("unit_price", 0)
            
            if sell_price > 0 or buy_price > 0:
                history[str_id]["data"].append({
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "sell": sell_price,
                    "buy": buy_price
                })
                
                # Max 100 Einträge pro Item halten
                if len(history[str_id]["data"]) > 100:
                    history[str_id]["data"].pop(0)
        
        # Speichern
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
        
        print(f"✅ JSON-Historie aktualisiert")
    except Exception as e:
        print(f"❌ JSON-Update Fehler: {e}")

def main():
    """Hauptfunktion"""
    print("=" * 60)
    print(f"🔄 GW2 Preis-Update gestartet: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)
    
    # Datenbank initialisieren
    init_db()
    
    # Preise abrufen
    live_data = fetch_live_prices(ALL_IDS)
    
    if live_data:
        # In DB speichern
        for item_id, item_data in live_data.items():
            sell_price = item_data.get("sells", {}).get("unit_price", 0)
            buy_price = item_data.get("buys", {}).get("unit_price", 0)
            log_prices_to_db(item_id, sell_price, buy_price)
        
        print(f"💾 {len(live_data)} Items in Datenbank gespeichert")
        
        # JSON aktualisieren
        update_price_history_json(live_data)
        
        print("\n✅ Update erfolgreich abgeschlossen!")
    else:
        print("\n❌ Keine Daten erhalten - Update fehlgeschlagen")

if __name__ == "__main__":
    main()
