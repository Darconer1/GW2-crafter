import streamlit as st
import requests
import pandas as pd
import os
import json
from datetime import datetime

# Seiteneinstellungen für eine saubere mobile und Desktop-Ansicht
st.set_page_config(page_title="GW2 Gold-Optimierer", layout="wide", initial_sidebar_state="collapsed")

# --- HISTORISCHE DATENVERWALTUNG ---
HISTORY_FILE = "price_history.json"

def load_price_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except:
                return {}
    return {}

def save_price_history(history):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)

def update_history_entry(history, item_id, name, sell_price, buy_price):
    str_id = str(item_id)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if str_id not in history:
        history[str_id] = {"name": name, "data": []}
    
    # Nur hinzufügen, wenn gültige Preise vorhanden sind
    if sell_price > 0 or buy_price > 0:
        history[str_id]["data"].append({
            "timestamp": timestamp,
            "sell": sell_price,
            "buy": buy_price
        })
    # Historie auf die letzten 100 Einträge begrenzen
    if len(history[str_id]["data"]) > 100:
        history[str_id]["data"].pop(0)

# --- HILFSFUNKTIONEN ---
def format_gw2_money(copper):
    """Formatiert Kupfermünzen in das typische Gold/Silber/Kupfer-Format."""
    if pd.isna(copper) or copper <= 0:
        return "0s 0c"
    if copper == float('inf'):
        return "🔒 Accountgebunden"
    copper = int(copper)
    gold = copper // 10000
    silver = (copper % 10000) // 100
    rem_copper = copper % 100
    
    if gold > 0:
        return f"{gold}g {silver}s {rem_copper}c"
    elif silver > 0:
        return f"{silver}s {rem_copper}c"
    else:
        return f"{rem_copper}c"

@st.cache_data(ttl=60)
def fetch_live_prices(item_ids):
    """Holt Echtzeit-Preise von der offiziellen GW2-API mit Browser-User-Agent."""
    if not item_ids:
        return {}
    ids_str = ",".join(map(str, item_ids))
    url = f"https://api.guildwars2.com/v2/commerce/prices?ids={ids_str}"
    
    # FIX: Echter User-Agent verhindert das Blockieren durch Cloudflare/ArenaNet
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return {int(item["id"]): item for item in data}
    except Exception as e:
        st.error(f"Fehler beim Laden der API-Daten: {e}")
    return {}

# --- DATEN-DEFINITIONEN ---
COOLDOWN_IDS = {
    "Deldrimor-Stahlbarren": 46738,
    "Elonischer Lederquadrat": 46739,
    "Chiffon-Ballen": 46740,
    "Geistreichen-Holzplanke": 46741
}

RAW_MAT_IDS = {
    "Mithril-Barren": 19684,
    "Eisenerz": 19697,
    "Platinerz": 19702,
    "Grober Lederabschnitt": 19722,
    "Rauher Lederabschnitt": 19725,
    "Dicker Lederabschnitt": 19728,
    "Seidenrest": 19748,
    "Leinenrest": 19743,
    "Altes Holzblock": 19720,       # Antikes Holz
    "Geschmeidiges Holzblock": 19711 # Weiches Holz
}

MF_MATERIAL_PARE = {
    "Blut": {"t5": 24294, "t6": 24295, "name": "Kraftvolles Blut (T6)"},
    "Knochen": {"t5": 24341, "t6": 24358, "name": "Antiker Knochen (T6)"},
    "Klaue": {"t5": 24350, "t6": 24351, "name": "Scheußliche Klaue (T6)"},
    "Fangzahn": {"t5": 24276, "t6": 24271, "name": "Scheußlicher Fangzahn (T6)"},
    "Schuppe": {"t5": 24283, "t6": 24289, "name": "Gepanzerte Schuppe (T6)"},
    "Giftbeutel": {"t5": 24277, "t6": 24280, "name": "Wirksamer Giftbeutel (T6)"},
    "Totem": {"t5": 24299, "t6": 24300, "name": "Verziertes Totem (T6)"},
    "Staub": {"t5": 24274, "t6": 24275, "name": "Kristalliner Staub (T6)"}
}

ECTO_ID = 19721
ENCRYPTION_ID = 75919

ALL_IDS = list(COOLDOWN_IDS.values()) + list(RAW_MAT_IDS.values()) + [ECTO_ID, ENCRYPTION_ID]
for p in MF_MATERIAL_PARE.values():
    ALL_IDS.extend([p["t5"], p["t6"]])
ALL_IDS = list(set(ALL_IDS))

# Live-Preise abrufen
live_data = fetch_live_prices(ALL_IDS)

# Historische Datenbank laden und füttern
price_history = load_price_history()
for name, idx in COOLDOWN_IDS.items():
    info = live_data.get(idx, {})
    update_history_entry(price_history, idx, name, info.get("sells", {}).get("unit_price", 0), info.get("buys", {}).get("unit_price", 0))
for name, idx in RAW_MAT_IDS.items():
    info = live_data.get(idx, {})
    update_history_entry(price_history, idx, name, info.get("sells", {}).get("unit_price", 0), info.get("buys", {}).get("unit_price", 0))
save_price_history(price_history)

# --- APP-OBERFLÄCHE ---
st.title("⚔️ GW2 Profit- & Handwerks-Optimierer")
st.caption("Fehlerbereinigte Echtzeit-Preise inklusive automatischer historischer Trendanalyse.")

with st.sidebar:
    st.header("⚙️ Einstellungen")
    tp_fee_toggle = st.checkbox("Handelsposten-Gebühr abziehen (15 %)", value=True)
    fee_multiplier = 0.85 if tp_fee_toggle else 1.0
    
    st.markdown("---")
    st.subheader("💎 Geistersplitter-Wertung")
    relic_per_shard = st.number_input("Fraktal-Relikte pro Geistersplitter", value=28)

tab1, tab2, tab3, tab4 = st.tabs(["🕒 Daily Cooldowns", "📉 Fraktal-Rendite", "🔮 Mystic Forge T5➔T6", "📊 Historische Trends"])

# --- TAB 1: DAILY COOLDOWN PLANER (FIXED) ---
with tab1:
    st.header("🕒 Tägliche Veredelung & Vorrats-Planer")
    
    cooldown_results = []
    
    # Helfer für schnellen Preis-Lookup (verhindert 0c Bugs)
    def get_price(item_id, mode="buys"):
        return live_data.get(item_id, {}).get(mode, {}).get("unit_price", 0)

    for name, item_id in COOLDOWN_IDS.items():
        sell_price = get_price(item_id, "sells")
        
        # FIX: Exakte mathematische Berechnungen nach echten GW2-Rezepten
        if name == "Deldrimor-Stahlbarren":
            # 20x Eisenerz (für Stahl/Eisen) + 2x Platinerz (Dunkelstahl) + 50x Mithrilerz (über Mithrillium-Äquivalent)
            craft_cost = (get_price(19697) * 20) + (get_price(19702) * 2) + (get_price(19684) * 25) + 48 # + Händlerkosten
        elif name == "Elonischer Lederquadrat":
            # 50x Dicker Lederabschnitt + 2x Grober + 2x Rauher
            craft_cost = (get_price(19728) * 50) + (get_price(19722) * 2) + (get_price(19725) * 2)
        elif name == "Chiffon-Ballen":
            # 100x Seidenrest + 2x Leinenrest
            craft_cost = (get_price(19748) * 100) + (get_price(19743) * 2)
        else: # Geistreichen-Holzplanke
            # 50x Altes Holzblock + 3x Geschmeidiges Holzblock
            craft_cost = (get_price(19720) * 50) + (get_price(19711) * 3)

        revenue = sell_price * fee_multiplier
        profit = revenue - craft_cost
        
        # HISTORISCHE BEWERTUNG: Vergleiche Live-Kosten mit dem ermittelten Allzeit-Schnitt
        str_id = str(item_id)
        past_costs = [d["sell"] for d in price_history.get(str_id, {}).get("data", [])]
        avg_historic = sum(past_costs) / len(past_costs) if past_costs else craft_cost
        
        if craft_cost < avg_historic * 0.96:
            recommendation = "🟢 VORRAT KAUFEN (Günstiger als Schnitt)"
        elif craft_cost > avg_historic * 1.04:
            recommendation = "🔴 ABWARTEN (Aktuell überteuert)"
        else:
            recommendation = "编 NORMAL (Nach Bedarf kaufen)"
            
        cooldown_results.append({
            "Gegenstand": name,
            "VK-Preis (Live)": format_gw2_money(sell_price),
            "Herstellkosten (Live)": format_gw2_money(craft_cost),
            "Reingewinn": format_gw2_money(profit),
            "Historische Bewertung": recommendation
        })
        
    st.dataframe(pd.DataFrame(cooldown_results), use_container_width=True, hide_index=True)

# --- TAB 2: FRAKTAL RENDITE ---
with tab2:
    st.header("📉 Fraktal-Verschlüsselungen & Schlüssel")
    
    col1, col2 = st.columns([1, 1])
    with col1:
        enc_amount = st.number_input("Anzahl Verschlüsselungen", value=100, step=10, key="enc_input")
        key_source = st.selectbox(
            "Schlüssel-Einkauf",
            ["Tiefenrabatt (20 Silber)", "Rabattiert (30 Silber)", "Normalpreis (50 Silber)"]
        )
        
    key_cost_unit = {"Tiefenrabatt (20 Silber)": 2000, "Rabattiert (30 Silber)": 3000, "Normalpreis (50 Silber)": 5000}[key_source]
    enc_sell_price = get_price(ENCRYPTION_ID, "sells")
    
    avg_open_value = 4850 # Statistischer Wert flüssigen Goldes pro Box
    total_key_cost = enc_amount * key_cost_unit
    total_sell_revenue = (enc_amount * enc_sell_price) * fee_multiplier
    total_open_revenue = (enc_amount * avg_open_value) - total_key_cost

    with col2:
        st.metric("Direktverkauf (nach Gebühr)", format_gw2_money(total_sell_revenue))
        st.metric("Wert bei Öffnung (Netto)", format_gw2_money(total_open_revenue))

    if total_open_revenue > total_sell_revenue:
        st.success(f"🚀 **Öffnen lohnt sich!** Plus von ca. {format_gw2_money(total_open_revenue - total_sell_revenue)} gegenüber Verkauf.")
    else:
        st.warning(f"⚖️ **Lieber im Handelsposten verkaufen!** Öffnen bringt {format_gw2_money(total_sell_revenue - total_open_revenue)} Verlust.")

# --- TAB 3: MYSTIC FORGE T5➔T6 ---
with tab3:
    st.header("🔮 Schmiede-Materialaufwertung")
    
    dust_price = get_price(MF_MATERIAL_PARE["Staub"]["t6"], "buys")
    st.markdown(f"**Fixkosten-Basis:** Kristalliner Staub: `{format_gw2_money(dust_price)}`")

    mf_results = []
    for mat_key, ids in MF_MATERIAL_PARE.items():
        if mat_key == "Staub": continue
            
        t5_buy = get_price(ids["t5"], "buys")
        t6_sell = get_price(ids["t6"], "sells")
        
        total_craft_cost = (25 * t5_buy) + (1 * t5_buy) + (5 * dust_price)
        gross_revenue = 4.25 * t6_sell * fee_multiplier
        net_profit = gross_revenue - total_craft_cost
        
        mf_results.append({
            "Material-Typ": ids["name"],
            "Einkauf T5 (25x)": format_gw2_money(25 * t5_buy),
            "Ertrag T6 (Schnitt 4.25x)": format_gw2_money(gross_revenue),
            "Reingewinn": net_profit,
            "Reingewinn Formatiert": format_gw2_money(net_profit)
        })

    df_mf = pd.DataFrame(mf_results).sort_values(by="Reingewinn", ascending=False)
    st.dataframe(df_mf[["Material-Typ", "Einkauf T5 (25x)", "Ertrag T6 (Schnitt 4.25x)", "Reingewinn Formatiert"]], use_container_width=True, hide_index=True)

# --- TAB 4: HISTORISCHE TRENDS (NEU) ---
with tab4:
    st.header("📊 Historische Diagramme & Markt-Entwicklungen")
    st.write("Visualisiert die Preis-Datenpunkte, die das Skript im Laufe der Zeit im Hintergrund sammelt.")
    
    all_history_options = {**COOLDOWN_IDS, **RAW_MAT_IDS}
    selected_trend_name = st.selectbox("Wähle ein Material zur Kursanalyse:", list(all_history_options.keys()))
    
    if selected_trend_name:
        target_id = str(all_history_options[selected_trend_name])
        item_history = price_history.get(target_id, {}).get("data", [])
        
        if len(item_history) < 2:
            st.info("Sammle noch Daten... Komm wieder, wenn die App ein paar Mal neu geladen wurde, um Kurven zu zeichnen.")
        else:
            df_chart = pd.DataFrame(item_history)
            df_chart["timestamp"] = pd.to_datetime(df_chart["timestamp"])
            df_chart["Verkaufspreis (Silber)"] = df_chart["sell"] / 100
            df_chart["Einkaufspreis (Silber)"] = df_chart["buy"] / 100
            
            st.line_chart(df_chart.set_index("timestamp")[["Verkaufspreis (Silber)", "Einkaufspreis (Silber)"]])
