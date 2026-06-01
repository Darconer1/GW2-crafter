import os
import json
import requests
from datetime import datetime

# HIER DEINE DISCORD WEBHOOK URL EINTRAGEN
DISCORD_WEBHOOK_URL = "DEIN_DISCORD_WEBHOOK_URL_HIER"
API_BASE = "https://api.guildwars2.com/v2"

def load_json(filename, default_factory):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            try: return json.load(f)
            except: return default_factory()
    return default_factory()

def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def send_discord_alert(message):
    if DISCORD_WEBHOOK_URL == "DEIN_DISCORD_WEBHOOK_URL_HIER":
        print(f"Discord Alert (Nicht gesendet, kein Webhook): {message}")
        return
    payload = {"content": message}
    try: requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e: print(f"Fehler beim Senden an Discord: {e}")

def format_money(copper):
    copper = int(copper)
    g, s, c = copper // 10000, (copper % 10000) // 100, copper % 100
    if g > 0: return f"{g}g {s}s {c}c"
    elif s > 0: return f"{s}s {c}c"
    else: return f"{c}c"

def run_tracker():
    config = load_json("config.json", dict)
    history = load_json("price_history.json", dict)
    
    # Alle Item-IDs aus der verschachtelten Favoriten-JSON sammeln
    item_ids = []
    item_id_to_name = {}
    for category, items in config.items():
        if isinstance(items, dict):
            for name, item_id in items.items():
                item_ids.append(item_id)
                item_id_to_name[item_id] = name
                
    if not item_ids:
        print("Keine Favoriten in config.json gefunden.")
        return

    # 1. Live-Preise von der GW2 API holen
    ids_str = ",".join(map(str, item_ids))
    res = requests.get(f"{API_BASE}/commerce/prices?ids={ids_str}")
    if res.status_code != 200:
        print("Fehler beim Abruf der GW2 API Preise.")
        return
    
    live_prices = res.json()
    timestamp = datetime.now().isoformat()
    
    # 2. Preise auswerten und Historie pflegen
    for p in live_prices:
        item_id = p["id"]
        sell_price = p["sells"]["unit_price"]
        buy_price = p["buys"]["unit_price"]
        name = item_id_to_name.get(item_id, f"Item {item_id}")
        
        if str(item_id) not in history:
            history[str(item_id)] = {"name": name, "data": []}
            
        # Neuen Datenpunkt hinzufügen
        history[str(item_id)]["data"].append({
            "timestamp": timestamp,
            "sell": sell_price,
            "buy": buy_price
        })
        
        # Begrenze die Historie auf die letzten 500 Einträge, um die Datei klein zu halten
        if len(history[str(item_id)]["data"]) > 500:
            history[str(item_id)]["data"].pop(0)
            
        # --- PREIS-ANALYSE ---
        all_past_sells = [d["sell"] for d in history[str(item_id)]["data"] if d["sell"] > 0]
        
        if len(all_past_sells) >= 5:  # Braucht ein paar Datenpunkte für einen soliden Schnitt
            avg_sell = sum(all_past_sells) / len(all_past_sells)
            
            # Schwellenwerte berechnen (z.B. 15% Abweichung)
            low_threshold = avg_sell * 0.85
            high_threshold = avg_sell * 1.15
            
            if sell_price <= low_threshold:
                send_discord_alert(f"📉 **EINKAUFS-ALARM:** `{name}` ist aktuell extrem GÜNSTIG! \nLive-Preis: {format_money(sell_price)} (Schnitt: {format_money(avg_sell)})")
            elif sell_price >= high_threshold:
                send_discord_alert(f"📈 **VERKAUFS-ALARM:** `{name}` ist aktuell extrem TEUER! \nLive-Preis: {format_money(sell_price)} (Schnitt: {format_money(avg_sell)})")

    save_json("price_history.json", history)
    print(f"Preise erfolgreich um {timestamp} aktualisiert.")

if __name__ == "__main__":
    run_tracker()
