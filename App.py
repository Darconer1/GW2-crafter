import streamlit as st
import requests
import pandas as pd
import os
import json
from datetime import datetime
import sqlite3
import statistics
import math

# Seiteneinstellungen
st.set_page_config(page_title="GW2 Gold-Optimierer", layout="wide", initial_sidebar_state="collapsed")

# --- HISTORISCHE DATENVERWALTUNG ---
HISTORY_FILE = "price_history.json"
DB_FILE = "price_history.db"

def load_price_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            try: return json.load(f)
            except: return {}
    return {}

def save_price_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

def update_history_entry(history, item_id, name, sell_price, buy_price):
    str_id = str(item_id)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if str_id not in history:
        history[str_id] = {"name": name, "data": []}
    
    if sell_price > 0 or buy_price > 0:
        history[str_id]["data"].append({
            "timestamp": timestamp,
            "sell": sell_price,
            "buy": buy_price
        })
    if len(history[str_id]["data"]) > 100:
        history[str_id]["data"].pop(0)


# --- SQLITE DB FÜR LANGZEIT-SPEICHER ---
def init_db():
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

def log_prices_to_db(item_id, sell, buy):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO prices (item_id, timestamp, sell, buy) VALUES (?, ?, ?, ?)",
              (int(item_id), datetime.now().isoformat(), int(sell or 0), int(buy or 0)))
    # Keep DB size small: delete entries older than 120 days (approx)
    c.execute("DELETE FROM prices WHERE timestamp < datetime('now','-120 days')")
    conn.commit()
    conn.close()

def fetch_db_prices(item_id, days=30):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT sell, buy, timestamp FROM prices WHERE item_id=? AND timestamp>=datetime('now',?-0) ORDER BY timestamp",
              (int(item_id), f'-{days} days'))
    rows = c.fetchall()
    conn.close()
    return rows

def fetch_db_prices_simple(item_id, days=30):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT sell, timestamp FROM prices WHERE item_id=? AND timestamp>=datetime('now',? ) ORDER BY timestamp",
              (int(item_id), f'-{days} days'))
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]

# --- HILFSFUNKTIONEN ---
def format_gw2_money(copper):
    if pd.isna(copper) or copper <= 0: return "0s 0c"
    copper = int(copper)
    gold, silver, rem_copper = copper // 10000, (copper % 10000) // 100, copper % 100
    if gold > 0: return f"{gold}g {silver}s {rem_copper}c"
    elif silver > 0: return f"{silver}s {rem_copper}c"
    else: return f"{rem_copper}c"

@st.cache_data(ttl=60)
def fetch_live_prices(item_ids):
    debug_log = []
    if not item_ids: return {}, debug_log

    ids_str = ",".join(map(str, item_ids))
    url = f"https://api.guildwars2.com/v2/commerce/prices?ids={ids_str}"
    
    # Ein sauberer Bot-Header, der oft besser durch Cloudflare kommt als ein Fake-Chrome
    headers = {"User-Agent": "GW2-Crafter-Streamlit-Bot/1.0 (Contact: via Streamlit)"}
    
    try:
        debug_log.append(f"Sende API-Anfrage an GW2 Server...")
        response = requests.get(url, headers=headers, timeout=10)
        debug_log.append(f"HTTP Status Code: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            debug_log.append(f"Erfolgreich geladen: {len(data)} Items.")
            return {int(item["id"]): item for item in data}, debug_log
        else:
            debug_log.append(f"API Fehler-Antwort: {response.text}")
    except Exception as e:
        debug_log.append(f"Verbindungsfehler (Timeout/Block): {e}")
        
    return {}, debug_log


# Versucht, Verlauf von der offiziellen API zu laden (falls verfügbar)
@st.cache_data(ttl=3600)
def fetch_price_history_api(item_id):
    url = f"https://api.guildwars2.com/v2/commerce/prices/{item_id}/history"
    headers = {"User-Agent": "GW2-Crafter-Streamlit-Bot/1.0"}
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None

def moving_average(item_id, days=30):
    # try sqlite first
    try:
        vals = fetch_db_prices_simple(item_id, days)
        vals = [v for v in vals if v and v>0]
        if len(vals) >= 3:
            return sum(vals)/len(vals), vals
    except Exception:
        pass

    # fallback to local JSON
    try:
        hist = price_history.get(str(item_id), {}).get('data', [])
        cutoff = datetime.now() - pd.Timedelta(days=days)
        vals = [d['sell'] for d in hist if pd.to_datetime(d['timestamp']) >= cutoff and d.get('sell',0) > 0]
        if vals:
            return sum(vals)/len(vals), vals
    except Exception:
        pass
    return None, []

def volatility(item_id, days=30):
    _, vals = moving_average(item_id, days)
    vals = [v for v in vals if v and v>0]
    if len(vals) < 2: return None
    try:
        sd = statistics.pstdev(vals)
        mean = sum(vals)/len(vals)
        return sd/mean if mean>0 else None
    except Exception:
        return None

def recommend_buy(item_id, current_price=None, days=30):
    if current_price is None:
        current_price = get_price(item_id, 'sells')
    ma, vals = moving_average(item_id, days)
    vol = volatility(item_id, days)
    if ma is None:
        return {'decision':'unknown','reason':'keine Historie'}
    # Entscheidung: wenn aktueller Preis deutlich < MA und nicht extrem volatil
    vol = vol or 0
    # adaptive threshold: 3 * volatility or 10% minimum
    threshold = max(0.10, 3*vol)
    if current_price < ma * (1 - threshold):
        return {'decision':'buy','reason':f'Preis {current_price} < MA{days} {int(ma)} (th={threshold:.2f})'}
    elif current_price > ma * (1 + threshold):
        return {'decision':'avoid','reason':f'Preis {current_price} > MA{days} {int(ma)}'}
    else:
        return {'decision':'hold','reason':'Preis im Normbereich'}

# --- DATEN-DEFINITIONEN (Korrigierte IDs) ---
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

ALL_IDS = list(COOLDOWN_IDS.values()) + list(RAW_MAT_IDS.values()) + [ECTO_ID, ENCRYPTION_ID]
for p in MF_MATERIAL_PARE.values():
    ALL_IDS.extend([p["t5"], p["t6"]])
ALL_IDS = list(set(ALL_IDS))

# --- APP-OBERFLÄCHE START ---
st.title("⚔️ GW2 Profit- & Handwerks-Optimierer")
st.caption("Live-Marktdatenanalyse inklusive Debugging.")

# Daten Abrufen & History füttern
live_data, debug_log = fetch_live_prices(ALL_IDS)

# Debug Log ausklappen, wenn Daten fehlen (Hilft uns enorm bei der Fehlersuche!)
with st.expander("🛠️ API Diagnose (Klicke für Details)", expanded=(not live_data)):
    for line in debug_log:
        st.code(line)

price_history = load_price_history()
if live_data:
    for name, idx in {**COOLDOWN_IDS, **RAW_MAT_IDS}.items():
        info = live_data.get(idx, {})
        update_history_entry(price_history, idx, name, info.get("sells", {}).get("unit_price", 0), info.get("buys", {}).get("unit_price", 0))
    save_price_history(price_history)

with st.sidebar:
    st.header("⚙️ Einstellungen")
    tp_fee_toggle = st.checkbox("Handelsposten-Gebühr abziehen (15 %)", value=True)
    fee_multiplier = 0.85 if tp_fee_toggle else 1.0
    st.subheader("💎 Geistersplitter-Wertung")
    relic_per_shard = st.number_input("Fraktal-Relikte pro Geistersplitter", value=28)

tab1, tab2, tab3, tab4 = st.tabs(["🕒 Daily Cooldowns", "📉 Fraktale", "🔮 Mystic Forge", "📊 Historie"])

def get_price(item_id, mode="buys"):
    return live_data.get(item_id, {}).get(mode, {}).get("unit_price", 0)

# --- TAB 1: DAILY COOLDOWN PLANER ---
with tab1:
    st.header("🕒 Tägliche Veredelung")
    cooldown_results = []
    
    for name, item_id in COOLDOWN_IDS.items():
        sell_price = get_price(item_id, "sells")
        
        # Komplett korrigierte Mathematik nach echten GW2 Rezepten (inkl. Händlerkosten für Thermokatalytisch/Kohle/Primordium)
        if name == "Deldrimor-Stahlbarren":
            craft_cost = (get_price(19684) * 50) + (get_price(19697) * 90) + (get_price(19702) * 40) + 1135
        elif name == "Elonischer Lederquadrat":
            craft_cost = (get_price(19728) * 50) + (get_price(19718) * 40) + (get_price(19719) * 20) + (get_price(19725) * 40) + 15
        elif name == "Chiffon-Ballen":
            craft_cost = (get_price(19748) * 100) + (get_price(19739) * 40) + (get_price(19741) * 20) + (get_price(19743) * 40) + 15
        else: # Geistreichen-Holzplanke
            craft_cost = (get_price(19722) * 50) + (get_price(19710) * 40) + (get_price(19709) * 30) + (get_price(19713) * 60) + 15

        revenue = sell_price * fee_multiplier
        profit = revenue - craft_cost if live_data else 0
        
        # Bewertung
        past_costs = [d["sell"] for d in price_history.get(str(item_id), {}).get("data", [])]
        avg_historic = sum(past_costs) / len(past_costs) if past_costs else craft_cost
        
        if not live_data: rec = "⚠️ API Fehler"
        elif craft_cost < avg_historic * 0.96: rec = "🟢 KAUFEN"
        elif craft_cost > avg_historic * 1.04: rec = "🔴 ABWARTEN"
        else: rec = "🟡 NORMAL"
            
        cooldown_results.append({
            "Gegenstand": name,
            "VK-Preis": format_gw2_money(sell_price),
            "Herstellkosten": format_gw2_money(craft_cost),
            "Reingewinn": format_gw2_money(profit),
            "Empfehlung": rec
        })
        
    st.dataframe(pd.DataFrame(cooldown_results), use_container_width=True, hide_index=True)

# --- TAB 2: FRAKTAL RENDITE ---
with tab2:
    st.header("📉 Fraktal-Verschlüsselungen")
    col1, col2 = st.columns([1, 1])
    with col1:
        enc_amount = st.number_input("Anzahl Verschlüsselungen", value=100, step=10)
        key_source = st.selectbox("Schlüssel-Einkauf", ["Tiefenrabatt (20 Silber)", "Rabattiert (30 Silber)", "Normalpreis (50 Silber)"])
        
    key_cost_unit = {"Tiefenrabatt (20 Silber)": 2000, "Rabattiert (30 Silber)": 3000, "Normalpreis (50 Silber)": 5000}[key_source]
    enc_sell_price = get_price(ENCRYPTION_ID, "sells")
    
    avg_open_value = 4850
    total_key_cost = enc_amount * key_cost_unit
    total_sell_revenue = (enc_amount * enc_sell_price) * fee_multiplier
    total_open_revenue = (enc_amount * avg_open_value) - total_key_cost

    with col2:
        st.metric("Direktverkauf", format_gw2_money(total_sell_revenue))
        st.metric("Wert bei Öffnung", format_gw2_money(total_open_revenue))

    if total_open_revenue > total_sell_revenue and live_data:
        st.success(f"🚀 **Öffnen lohnt sich!** Plus von {format_gw2_money(total_open_revenue - total_sell_revenue)}.")
    elif live_data:
        st.warning(f"⚖️ **Verkaufen!** Öffnen bringt {format_gw2_money(total_sell_revenue - total_open_revenue)} Verlust.")

# --- TAB 3: MYSTIC FORGE ---
with tab3:
    st.header("🔮 Schmiede-Materialaufwertung")
    dust_price = get_price(MF_MATERIAL_PARE["Staub"]["t6"], "buys")
    st.markdown(f"**Kristalliner Staub (Einkauf):** `{format_gw2_money(dust_price)}`")

    mf_results = []
    for mat_key, ids in MF_MATERIAL_PARE.items():
        if mat_key == "Staub": continue
        t5_buy, t6_sell = get_price(ids["t5"], "buys"), get_price(ids["t6"], "sells")
        total_craft_cost = (26 * t5_buy) + (5 * dust_price) # 25 + 1
        gross_revenue = 4.25 * t6_sell * fee_multiplier
        net_profit = gross_revenue - total_craft_cost
        
        mf_results.append({
            "Material": ids["name"],
            "Einkauf T5": format_gw2_money(25 * t5_buy),
            "Ertrag T6 (4.25x)": format_gw2_money(gross_revenue),
            "Reingewinn": net_profit,
            "Gewinn/Verlust": format_gw2_money(net_profit)
        })

    df_mf = pd.DataFrame(mf_results).sort_values(by="Reingewinn", ascending=False)
    st.dataframe(df_mf[["Material", "Einkauf T5", "Ertrag T6 (4.25x)", "Gewinn/Verlust"]], use_container_width=True, hide_index=True)

# --- TAB 4: HISTORISCHE TRENDS ---
with tab4:
    st.header("📊 Historische Diagramme")
    all_history_options = {**COOLDOWN_IDS, **RAW_MAT_IDS}
    selected_trend_name = st.selectbox("Wähle ein Material:", list(all_history_options.keys()))
    
    if selected_trend_name:
        item_history = price_history.get(str(all_history_options[selected_trend_name]), {}).get("data", [])
        if len(item_history) < 2:
            st.info("Sammle noch Daten... Lade die Seite später neu, um Diagramme zu sehen.")
        else:
            df_chart = pd.DataFrame(item_history)
            df_chart["timestamp"] = pd.to_datetime(df_chart["timestamp"])
            df_chart["Verkauf (Silber)"] = df_chart["sell"] / 100
            df_chart["Einkauf (Silber)"] = df_chart["buy"] / 100
            st.line_chart(df_chart.set_index("timestamp")[["Verkauf (Silber)", "Einkauf (Silber)"]])
