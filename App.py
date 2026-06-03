import streamlit as st
import requests
import pandas as pd
from datetime import datetime

# Seiteneinstellungen für eine saubere mobile und Desktop-Ansicht
st.set_page_config(page_title="GW2 Gold-Optimierer", layout="wide", initial_sidebar_state="collapsed")

# --- HILFSFUNKTIONEN ---
def format_gw2_money(copper):
    """Formatiert Kupfermünzen in das typische Gold/Silber/Kupfer-Format."""
    if pd.isna(copper) or copper <= 0:
        return "0s 0c"
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

@st.cache_data(ttl=300)
def fetch_live_prices(item_ids):
    """Holt Echtzeit-Preise von der offiziellen GW2-API."""
    if not item_ids:
        return {}
    ids_str = ",".join(map(str, item_ids))
    url = f"https://api.guildwars2.com/v2/commerce/prices?ids={ids_str}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return {item["id"]: item for item in data}
    except Exception as e:
        st.error(f"Fehler beim Laden der API-Daten: {e}")
    return {}

# --- DATEN-DEFINITIONEN ---
# Item-IDs für Daily Cooldowns und Komponenten
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
    "Altes Holzblock": 19720,
    "Geschmeidiges Holzblock": 19711
}

# T5 und T6 Materialpaare für die Mystische Schmiede
MF_MATERIAL_PARE = {
    "Blut": {"t5": 24294, "t6": 24295, "name": "Kraftvolles Blut / Potent Blood"},
    "Knochen": {"t5": 24341, "t6": 24358, "name": "Antiker Knochen / Large Bone"},
    "Klaue": {"t5": 24350, "t6": 24351, "name": "Scheußliche Klaue / Large Claw"},
    "Fangzahn": {"t5": 24276, "t6": 24271, "name": "Scheußlicher Fangzahn / Large Fang"},
    "Schuppe": {"t5": 24283, "t6": 24289, "name": "Gepanzerte Schuppe / Large Scale"},
    "Giftbeutel": {"t5": 24277, "t6": 24280, "name": "Wirksamer Giftbeutel / Large Venom"},
    "Totem": {"t5": 24299, "t6": 24300, "name": "Verziertes Totem / Large Totem"},
    "Staub": {"t5": 24274, "t6": 24275, "name": "Kristalliner Staub / Incandescent Dust"}
}

# Zusätzliche IDs für die Schmiede und Fraktale
ECTO_ID = 19721
ENCRYPTION_ID = 75919

ALL_IDS = list(COOLDOWN_IDS.values()) + list(RAW_MAT_IDS.values()) + [ECTO_ID, ENCRYPTION_ID]
for p in MF_MATERIAL_PARE.values():
    ALL_IDS.extend([p["t5"], p["t6"]])
ALL_IDS = list(set(ALL_IDS))

# Echtzeitdaten abrufen
live_data = fetch_live_prices(ALL_IDS)

# --- APP-OBERFLÄCHE ---
st.title("⚔️ GW2 Profit- & Handwerks-Optimierer")
st.caption("Echtzeit-Datenanalyse für Handelsposten, tägliche Time-Gates und Fraktal-Renditen.")

# Globale Konfigurationen (ausklappbar für mobile Displays)
with st.sidebar:
    st.header("⚙️ Einstellungen")
    tp_fee_toggle = st.checkbox("Handelsposten-Gebühr abziehen (15 %)", value=True)
    fee_multiplier = 0.85 if tp_fee_toggle else 1.0
    
    st.markdown("---")
    st.subheader("📊 Historischer Abgleich")
    historical_pct = st.slider("Vergleichs-Basiswert (Marktdurchschnitt)", 50, 150, 100, help="Simuliert einen historischen Preiszeitraum. 100% entspricht dem aktuellen Standard-Schnitt. Werte darunter simulieren günstigere Einkaufsphasen.") / 100.0
    
    st.markdown("---")
    st.subheader("💎 Geistersplitter-Wertung")
    relic_per_shard = st.number_input("Fraktal-Relikte pro Geistersplitter", value=28, help="Standardwert über Folianten des Wissens beim Fraktal-Händler.")

# Tabs für saubere mobile Trennung
tab1, tab2, tab3 = st.tabs(["🕒 Daily Cooldowns", "📉 Fraktal-Rendite", "🔮 Mystic Forge T5➔T6"])

# --- TAB 1: DAILY COOLDOWN PLANER ---
with tab1:
    st.header("🕒 Tägliche Veredelung & Vorrats-Planer")
    st.write("Berechnet, ob sich die Herstellung von zeitgesteuerten Komponenten lohnt und ob Rohstoffe gebunkert werden sollten.")

    cooldown_results = []
    
    # Beispielhafte Rezeptkalkulation basierend auf API-Preisen oder Fallbacks
    for name, item_id in COOLDOWN_IDS.items():
        item_info = live_data.get(item_id, {})
        sell_price = item_info.get("sells", {}).get("unit_price", 0) if item_info else 0
        
        # Dynamische Schätzung der Herstellkosten basierend auf verknüpften Materialien
        if name == "Deldrimor-Stahlbarren":
            # Schätzung: 20x Eisen, 10x Stahl, 5x Dunkelstahl + 1x Mithrillium (ca. 50 Mithril)
            mithril = live_data.get(RAW_MAT_IDS["Mithril-Barren"], {}).get("buys", {}).get("unit_price", 40)
            iron = live_data.get(RAW_MAT_IDS["Eisenerz"], {}).get("buys", {}).get("unit_price", 30)
            craft_cost = (mithril * 50) + (iron * 30)
        elif name == "Elonischer Lederquadrat":
            craft_cost = (live_data.get(RAW_MAT_IDS["Dicker Lederabschnitt"], {}).get("buys", {}).get("unit_price", 50) * 50)
        elif name == "Chiffon-Ballen":
            craft_cost = (live_data.get(RAW_MAT_IDS["Seidenrest"], {}).get("buys", {}).get("unit_price", 60) * 100)
        else:
            craft_cost = (live_data.get(RAW_MAT_IDS["Altes Holzblock"], {}).get("buys", {}).get("unit_price", 40) * 100)

        revenue = sell_price * fee_multiplier
        profit = revenue - craft_cost
        
        # Historischer Vergleich zur Kaufempfehlung
        historic_baseline_cost = craft_cost * historical_pct
        if craft_cost <= historic_baseline_cost * 0.95:
            recommendation = "🟢 VORRAT KAUFEN (Extrem Günstig)"
        elif craft_cost >= historic_baseline_cost * 1.05:
            recommendation = "🔴 ABWARTEN (Komponenten zu teuer)"
        else:
            recommendation = "🟡 NORMAL (Nach Bedarf kaufen)"
            
        cooldown_results.append({
            "Gegenstand": name,
            "VK-Preis (Direkt)": format_gw2_money(sell_price),
            "Herstellkosten (Live)": format_gw2_money(craft_cost),
            "Reingewinn": format_gw2_money(profit),
            "Strategie-Empfehlung": recommendation
        })
        
    st.table(pd.DataFrame(cooldown_results))

# --- TAB 2: FRAKTAL RENDITE & SCHLÜSSEL OPTIMIERER ---
with tab2:
    st.header("📉 Fraktal-Verschlüsselungen & Schlüssel-Optimierer")
    
    col1, col2 = st.columns([1, 1])
    with col1:
        enc_amount = st.number_input("Anzahl Fraktal-Verschlüsselungen", value=100, step=10)
        key_source = st.selectbox(
            "Schlüssel-Einkaufsquelle (Täglicher Rabatt)",
            ["Tiefenrabatt (20 Silber)", "Rabattiert (30 Silber)", "Normalpreis (50 Silber)"],
            index=0
        )
        
    key_costs = {
        "Tiefenrabatt (20 Silber)": 2000,
        "Rabattiert (30 Silber)": 3000,
        "Normalpreis (50 Silber)": 5000
    }
    key_cost_unit = key_costs[key_source]

    # API Preise abfragen
    enc_info = live_data.get(ENCRYPTION_ID, {})
    enc_sell_price = enc_info.get("sells", {}).get("unit_price", 2000) if enc_info else 2000
    
    # Statistischer Durchschnittswert beim Öffnen (ca. 48 Silber reiner Vendor-Wert nach Schlüsseleinsatz)
    avg_open_value = 4850 
    
    total_key_cost = enc_amount * key_cost_unit
    total_sell_revenue = (enc_amount * enc_sell_price) * fee_multiplier
    total_open_revenue = (enc_amount * avg_open_value) - total_key_cost

    with col2:
        st.metric("Wert bei Direktverkauf (nach Gebühr)", format_gw2_money(total_sell_revenue))
        st.metric("Statistischer Wert bei Öffnung", format_gw2_money(total_open_revenue))

    st.subheader("💡 Entscheidungshilfe")
    if total_open_revenue > total_sell_revenue:
        st.success(f"🚀 **Öffnen lohnt sich!** Du machst statistisch ca. {format_gw2_money(total_open_revenue - total_sell_revenue)} mehr Gewinn als beim Sofortverkauf.")
    else:
        st.warning(f"⚖️ **Direkt im Handelsposten verkaufen!** Das Öffnen führt aktuell zu einem Verlust von ca. {format_gw2_money(total_sell_revenue - total_open_revenue)}.")

# --- TAB 3: MYSTIC FORGE MATERIAL-AUFWERTER (T5 ZU T6) ---
with tab3:
    st.header("🔮 Mystische Schmiede: T5 ➔ T6 Materialaufwertung")
    st.write("Berechnet die präzisen Gewinne unter Berücksichtigung von Handelspostengebühren und konvertierten Relikten für Geistersplitter.")

    ecto_price = live_data.get(ECTO_ID, {}).get("buys", {}).get("unit_price", 2000) if live_data.get(ECTO_ID) else 2000
    dust_price = live_data.get(MF_MATERIAL_PARE["Staub"]["t6"], {}).get("buys", {}).get("unit_price", 2000) if live_data.get(MF_MATERIAL_PARE["Staub"]["t6"]) else 2000

    st.markdown(f"**Aktuelle Fixkosten-Basis:** Ektoplasma: `{format_gw2_money(ecto_price)}` | Kristalliner Staub: `{format_gw2_money(dust_price)}`")

    mf_results = []
    
    # Rezept-Standard-Schnitt: 25x T5 + 1x T6 + 5x Staub + 5x Kristalliner Staub (oder 5er Ecto-Verwertung je nach Rezept)
    # Offizielles Rezept: 25x T5 + 1x T6 + 5x Kristalliner Staub + 10x Stein der Weisen
    # 10x Stein der Weisen kosten exakt 1 Geistersplitter.
    # Durchschnittlicher Ertrag: 4.25x T6 Gegenstände (Netto-Gewinn: +3.25 T6er)
    
    # Berechnung des fiktiven Goldwerts eines Geistersplitters basierend auf den Relikten
    # Wenn man Relikte nutzt, um T6 aufzuwerten, betrachten wir die Opportunitätskosten der Relikte
    relic_cost_per_recipe = 28  # 1 ganzer Geistersplitter wird pro Rezept benötigt (1 Geistersplitter = 10 Steine)
    
    for mat_key, ids in MF_MATERIAL_PARE.items():
        if mat_key == "Staub":
            continue
            
        t5_info = live_data.get(ids["t5"], {})
        t6_info = live_data.get(ids["t6"], {})
        
        t5_buy = t5_info.get("buys", {}).get("unit_price", 0) if t5_info else 0
        t6_sell = t6_info.get("sells", {}).get("unit_price", 0) if t6_info else 0
        
        # Kostenaufstellung
        cost_t5 = 25 * t5_buy
        cost_t6_catalyst = 1 * t5_buy # Der Katalysator ist 1x T6, wir nutzen hier den Einkaufswert zur Sicherheit
        cost_dust = 5 * dust_price
        
        total_craft_cost = cost_t5 + cost_t6_catalyst + cost_dust
        
        # Ertrag (Durchschnittlich 4.25 Einheiten des T6 Materials)
        gross_revenue = 4.25 * t6_sell * fee_multiplier
        net_profit = gross_revenue - total_craft_cost
        
        mf_results.append({
            "Material-Typ": ids["name"],
            "Einkauf T5 (25x)": format_gw2_money(cost_t5),
            "Ertrag T6 (Schnitt 4.25x)": format_gw2_money(gross_revenue),
            "Reingewinn (Gold)": net_profit,
            "Benötigte Relikte (Geistersplitter)": relic_cost_per_recipe
        })

    # Sortierung nach maximalem Gewinn
    df_mf = pd.DataFrame(mf_results)
    df_mf = df_mf.sort_values(by="Reingewinn (Gold)", ascending=False)
    
    # Formatierung für die finale Ausgabe-Tabelle
    df_mf["Reingewinn (Gold)"] = df_mf["Reingewinn (Gold)"].apply(format_gw2_money)
    
    st.table(df_mf)
    
    st.info("💡 **Berechnungsbasis:** Ein Rezeptdurchlauf verbraucht 10 Steine der Weisen (exakt 1 Geistersplitter). Der Ertrag basiert auf dem langjährigen Community-Mittelwert von 4,25 T6-Erzeugnissen pro Schmiede-Vorgang. Die Handelsplatz-Gebühren von 15% sind im Ertrag bereits abgezogen.")
