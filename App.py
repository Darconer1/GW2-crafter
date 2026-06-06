import streamlit as st
import requests
import pandas as pd
import os
import json
from datetime import datetime
import sqlite3
import statistics
import math
import time
from openai import OpenAI

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


def normalize_api_key(key_value):
    if not key_value:
        return None
    key_value = str(key_value).strip()
    if key_value.startswith("OPENAI_API_KEY") and "=" in key_value:
        key_value = key_value.split("=", 1)[1].strip()
    if (key_value.startswith('"') and key_value.endswith('"')) or (key_value.startswith("'") and key_value.endswith("'")):
        key_value = key_value[1:-1].strip()
    return key_value if key_value else None


AI_CACHE_TTL = 3600
AI_MAX_CALLS_PER_MINUTE = 20
AI_RATE_LIMIT_WINDOW_SECONDS = 60
AI_MAX_TOKENS = 120


def get_openai_api_key():
    key = normalize_api_key(os.getenv("OPENAI_API_KEY"))
    if key:
        return key
    return normalize_api_key(st.secrets.get("OPENAI_API_KEY"))


def is_ai_rate_limited():
    now = time.time()
    timestamps = st.session_state.get("ai_call_timestamps", [])
    timestamps = [ts for ts in timestamps if now - ts < AI_RATE_LIMIT_WINDOW_SECONDS]
    if len(timestamps) >= AI_MAX_CALLS_PER_MINUTE:
        next_available = int(math.ceil(AI_RATE_LIMIT_WINDOW_SECONDS - (now - timestamps[0]))) if timestamps else AI_RATE_LIMIT_WINDOW_SECONDS
        st.session_state["ai_call_timestamps"] = timestamps
        return True, next_available
    timestamps.append(now)
    st.session_state["ai_call_timestamps"] = timestamps
    return False, 0


def heuristic_ai_assessment(current_price, moving_avg):
    if moving_avg and current_price < moving_avg * 0.95:
        return {
            "assessment": "🟢 Kaufempfehlung",
            "reasoning": f"Preis {current_price} unter Durchschnitt ({int(moving_avg)})",
            "confidence": "Mittel (Heuristik)"
        }
    elif moving_avg and current_price > moving_avg * 1.05:
        return {
            "assessment": "🔴 Nicht kaufen",
            "reasoning": f"Preis {current_price} über Durchschnitt ({int(moving_avg)})",
            "confidence": "Mittel (Heuristik)"
        }
    else:
        return {
            "assessment": "🟡 Abwarten",
            "reasoning": "Preis im Normbereich",
            "confidence": "Mittel (Heuristik)"
        }


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
        
        if response.status_code in [200, 206]:  # 206 = Partial Content (Range Request)
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
        if r.status_code in [200, 206]:  # 206 = Partial Content
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

DAILY_COOLDOWN_RECIPES = {
    "Deldrimor Steel Ingot": [
        {"name": "Mithril Ore", "id": 19684, "qty": 50},
        {"name": "Iron Ore", "id": 19697, "qty": 90},
        {"name": "Platinum Ore", "id": 19702, "qty": 40},
        {"name": "Händlergebühr", "id": None, "qty": 1135, "fixed": True}
    ],
    "Elonian Leather Square": [
        {"name": "Thick Leather Section", "id": 19728, "qty": 50},
        {"name": "Thin Leather Section", "id": 19718, "qty": 40},
        {"name": "Rugged Leather Section", "id": 19719, "qty": 20},
        {"name": "Coarse Leather Section", "id": 19725, "qty": 40},
        {"name": "Händlergebühr", "id": None, "qty": 15, "fixed": True}
    ],
    "Bolt of Damask": [
        {"name": "Silk Scrap", "id": 19748, "qty": 100},
        {"name": "Wool Scrap", "id": 19739, "qty": 40},
        {"name": "Cotton Scrap", "id": 19741, "qty": 20},
        {"name": "Linen Scrap", "id": 19743, "qty": 40},
        {"name": "Händlergebühr", "id": None, "qty": 15, "fixed": True}
    ],
    "Spiritwood Plank": [
        {"name": "Ancient Wood Log", "id": 19722, "qty": 50},
        {"name": "Seasoned Wood Log", "id": 19710, "qty": 40},
        {"name": "Aged Wood Log", "id": 19709, "qty": 30},
        {"name": "Hardwood Log", "id": 19713, "qty": 60},
        {"name": "Händlergebühr", "id": None, "qty": 15, "fixed": True}
    ]
}

DAILY_COOLDOWN_USAGE = {
    "Deldrimor Steel Ingot": [
        "Waffen- und Rüstungscrafting",
        "Legendäre Komponenten",
        "Schmiede- und Rüstungsrezepte"
    ],
    "Elonian Leather Square": [
        "Lederverarbeitungsexotische Rüstung",
        "Legendäre Lederkomponenten",
        "Rüstungsteile für Handschuhe und Stiefel"
    ],
    "Bolt of Damask": [
        "Schneiderei-Exotische Rüstung",
        "Legendäre Stoffkomponenten",
        "Rüstungsteile wie Tunika und Beinschutz"
    ],
    "Spiritwood Plank": [
        "Waffenverarbeitungswaffen",
        "Stäbe und Gewehre",
        "Legendäre Holzkomponenten"
    ]
}


def get_item_average(item_id, days=30):
    if item_id is None:
        return None
    ma, _ = moving_average(item_id, days)
    return ma


def ingredient_price_assessment(name, item_id, qty):
    if item_id is None or name == "Händlergebühr":
        return {
            "Ingredient": name,
            "Quantity": qty,
            "Current": format_gw2_money(qty) if name == "Händlergebühr" else "N/A",
            "Average": "N/A",
            "Diff": "N/A",
            "Recommendation": "Keine Preisbewertung",
            "Reason": "Fixkosten"
        }
    current_unit = get_price(item_id, "sells") or get_price(item_id, "buys")
    avg_unit = get_item_average(item_id, 30)
    total_current = current_unit * qty
    total_avg = int(avg_unit * qty) if avg_unit else None
    diff_pct = None
    if avg_unit and avg_unit > 0:
        diff_pct = ((current_unit - avg_unit) / avg_unit) * 100
    rec = "keine Daten"
    reason = "Keine historische Daten"
    if avg_unit and current_unit > 0:
        if diff_pct <= -10:
            rec = "🟢 Kaufempfehlung"
            reason = f"{abs(diff_pct):.0f}% günstiger als 30d Ø"
        elif diff_pct >= 10:
            rec = "🔴 Nicht kaufen"
            reason = f"{diff_pct:.0f}% teurer als 30d Ø"
        else:
            rec = "🟡 Abwarten"
            reason = f"{diff_pct:+.0f}% gegenüber 30d Ø"
    return {
        "Ingredient": name,
        "Quantity": qty,
        "Current": format_gw2_money(total_current) if total_current else "0c",
        "Average": format_gw2_money(total_avg) if total_avg else "N/A",
        "Diff": f"{diff_pct:+.1f}%" if diff_pct is not None else "N/A",
        "Recommendation": rec,
        "Reason": reason
    }

# --- KI-BEWERTUNG FÜR KAUFENTSCHEIDUNGEN ---
@st.cache_data(ttl=AI_CACHE_TTL)
def get_ai_assessment(item_name, price_history_data, current_price, moving_avg, use_ai=False):
    """
    Bewertet mit KI, ob es sinnvoll ist, ein Material auf Vorrat zu kaufen
    basierend auf historischen Daten und Trends
    """
    if not use_ai:
        return heuristic_ai_assessment(current_price, moving_avg)

    api_key = get_openai_api_key()
    if not api_key:
        return heuristic_ai_assessment(current_price, moving_avg)

    rate_limited, wait = is_ai_rate_limited()
    if rate_limited:
        st.warning(f"KI-Anfragen sind auf {AI_MAX_CALLS_PER_MINUTE} pro {AI_RATE_LIMIT_WINDOW_SECONDS} Sekunden begrenzt. Bitte {wait}s warten.")
        return heuristic_ai_assessment(current_price, moving_avg)

    try:
        client = OpenAI(api_key=api_key)
        
        # Zusammenfassung der Preis-Daten
        if isinstance(price_history_data, list) and len(price_history_data) > 0:
            recent_prices = price_history_data[-10:] if len(price_history_data) >= 10 else price_history_data
            price_trend = "stabil" if len(set(recent_prices)) <= 3 else ("steigend" if recent_prices[-1] > recent_prices[0] else "fallend")
            min_price = min(recent_prices)
            max_price = max(recent_prices)
            price_range = max_price - min_price
        else:
            price_trend = "unbekannt"
            min_price = current_price
            max_price = current_price
            price_range = 0
        
        # KI-Anfrage
        prompt = f"""
Du bist ein Experte für GW2 (Guild Wars 2) Wirtschaft und Rohstoffpreise.
Analysiere die folgenden Daten für das Material '{item_name}' und gib eine Kaufempfehlung:

- Aktueller Preis: {current_price} Kupfer ({current_price/100:.0f} Silber)
- 30-Tage Durchschnitt: {moving_avg} Kupfer ({moving_avg/100:.0f} Silber)
- Preis-Trend (letzte 10 Einträge): {price_trend}
- Min-Preis: {min_price}, Max-Preis: {max_price}, Range: {price_range}

Gib deine Bewertung in folgendem Format:
1. EMPFEHLUNG: (Kaufen / Nicht kaufen / Abwarten)
2. BEGRÜNDUNG: (kurz, max 2 Sätze)
3. VERTRAUEN: (Hoch / Mittel / Niedrig)
"""
        
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=AI_MAX_TOKENS
        )
        
        ai_response = response.choices[0].message.content
        
        # Parse response
        lines = ai_response.split('\n')
        assessment = "🟡 Abwarten"
        reasoning = ""
        confidence = "Mittel"
        
        for line in lines:
            if "EMPFEHLUNG:" in line:
                if "Kaufen" in line:
                    assessment = "🟢 Kaufempfehlung"
                elif "Nicht kaufen" in line:
                    assessment = "🔴 Nicht kaufen"
            elif "BEGRÜNDUNG:" in line:
                reasoning = line.split("BEGRÜNDUNG:")[-1].strip()
            elif "VERTRAUEN:" in line:
                confidence = line.split("VERTRAUEN:")[-1].strip()
        
        return {
            "assessment": assessment,
            "reasoning": reasoning or "KI-Analyse durchgeführt",
            "confidence": f"Hoch (KI: GPT-3.5)"
        }
    
    except Exception as e:
        # Bei Fehler fallback auf lokale Historie
        st.warning(f"KI-Analyse nicht verfügbar: {str(e)}. Fallback auf lokale Historie.")
        return heuristic_ai_assessment(current_price, moving_avg)

# --- DATEN-DEFINITIONEN (Korrigierte IDs) ---
COOLDOWN_IDS = {
    "Deldrimor Steel Ingot": 46738,
    "Elonian Leather Square": 46739,
    "Bolt of Damask": 46741,
    "Spiritwood Plank": 46736
}

RAW_MAT_IDS = {
    "Mithril Ore": 19684,
    "Iron Ore": 19697,
    "Platinum Ore": 19702,
    "Thick Leather Section": 19728,
    "Thin Leather Section": 19718,
    "Rugged Leather Section": 19719,
    "Coarse Leather Section": 19725,
    "Silk Scrap": 19748,
    "Wool Scrap": 19739,
    "Cotton Scrap": 19741,
    "Linen Scrap": 19743,
    "Ancient Wood Log": 19722,
    "Seasoned Wood Log": 19710,
    "Aged Wood Log": 19709,
    "Hardwood Log": 19713
}

MF_MATERIAL_PARE = {
    "Blood": {"t5": 24294, "t6": 24295, "name": "Powerful Blood"},
    "Bone": {"t5": 24341, "t6": 24358, "name": "Ancient Bone"},
    "Claw": {"t5": 24350, "t6": 24351, "name": "Horrible Claw"},
    "Fangtooth": {"t5": 24276, "t6": 24271, "name": "Horrible Fangtooth"},
    "Scale": {"t5": 24283, "t6": 24289, "name": "Armored Scale"},
    "Venom Sac": {"t5": 24277, "t6": 24280, "name": "Potent Venom Sac"},
    "Totem": {"t5": 24299, "t6": 24300, "name": "Ornate Totem"},
    "Dust": {"t5": 24274, "t6": 24275, "name": "Crystal Dust"}
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
    st.divider()
    use_ai_daily = st.checkbox(
        "KI für Daily Cooldown-Bewertung nutzen",
        value=False,
        help="Nur bei Bedarf aktivieren. Standardmäßig werden historisch basierte Kaufempfehlungen verwendet."
    )
    use_ai_history = st.checkbox(
        "KI in Historischer Trendanalyse aktivieren",
        value=False,
        help="Nur bei Bedarf einschalten."
    )
    use_ai_fractal = st.checkbox(
        "KI für Fraktal-Analyse aktivieren",
        value=False,
        help="Nur bei Bedarf einschalten."
    )
    use_ai_mystic_forge = st.checkbox(
        "KI für Mystic Forge Analyse aktivieren",
        value=False,
        help="Nur bei Bedarf einschalten."
    )
    if use_ai_daily:
        st.info("Die KI wird derzeit nur für die Daily Cooldown-Bewertung verwendet.")
    else:
        st.info("Die Daily Cooldown-Bewertung nutzt die lokale Heuristik.")
    if use_ai_history or use_ai_fractal or use_ai_mystic_forge:
        st.warning("KI außerhalb der Daily Cooldowns wird nur bei expliziter Aktivierung verwendet.")

tab1, tab2, tab3, tab4 = st.tabs(["🕒 Daily Cooldowns", "📉 Fraktale", "🔮 Mystic Forge", "📊 Historie"])

def get_price(item_id, mode="buys"):
    return live_data.get(item_id, {}).get(mode, {}).get("unit_price", 0)

# --- TAB 1: DAILY COOLDOWN PLANER ---
with tab1:
    st.header("🕒 Tägliche Veredelung")
    cooldown_results = []
    detailed_results = []

    for name, item_id in COOLDOWN_IDS.items():
        sell_price = get_price(item_id, "sells")
        recipe = DAILY_COOLDOWN_RECIPES.get(name, [])
        ingredient_rows = [ingredient_price_assessment(i["name"], i.get("id"), i["qty"]) for i in recipe]

        if name == "Deldrimor Steel Ingot":
            craft_cost = (get_price(19684) * 50) + (get_price(19697) * 90) + (get_price(19702) * 40) + 1135
        elif name == "Elonian Leather Square":
            craft_cost = (get_price(19728) * 50) + (get_price(19718) * 40) + (get_price(19719) * 20) + (get_price(19725) * 40) + 15
        elif name == "Bolt of Damask":
            craft_cost = (get_price(19748) * 100) + (get_price(19739) * 40) + (get_price(19741) * 20) + (get_price(19743) * 40) + 15
        else:  # Spiritwood Plank
            craft_cost = (get_price(19722) * 50) + (get_price(19710) * 40) + (get_price(19709) * 30) + (get_price(19713) * 60) + 15

        history_values = [d["sell"] for d in price_history.get(str(item_id), {}).get("data", [])]
        moving_avg, price_vals = moving_average(item_id, 30)
        historical_values = price_vals if price_vals else history_values

        # Fallback: wenn aktueller Live-Preis fehlt, verwende den letzten historischen Verkaufspreis
        if sell_price <= 0 and history_values:
            sell_price = history_values[-1]

        revenue = sell_price * fee_multiplier
        profit = revenue - craft_cost if (live_data or history_values) else 0

        avg_historic = sum(history_values) / len(history_values) if history_values else craft_cost
        price_diff = ((sell_price - avg_historic) / avg_historic * 100) if avg_historic else 0

        if not (live_data or history_values):
            rec = "⚠️ API Fehler"
        elif use_ai_daily:
            assessment = get_ai_assessment(
                name,
                historical_values,
                sell_price,
                moving_avg,
                use_ai=True
            )
            rec = assessment["assessment"]
        elif price_diff <= -10:
            rec = "🟢 Kaufempfehlung"
        elif price_diff >= 10:
            rec = "🔴 Nicht kaufen"
        else:
            rec = "🟡 Abwarten"

        cooldown_results.append({
            "Gegenstand": name,
            "VK-Preis": format_gw2_money(sell_price),
            "Herstellkosten": format_gw2_money(craft_cost),
            "Reingewinn": format_gw2_money(profit),
            "Empfehlung": rec,
            "Preis vs Ø": f"{price_diff:+.1f}%"
        })

        detailed_results.append({
            "name": name,
            "item_id": item_id,
            "sell_price": sell_price,
            "craft_cost": craft_cost,
            "profit": profit,
            "recommendation": rec,
            "price_diff": price_diff,
            "ingredient_rows": ingredient_rows,
            "use_cases": DAILY_COOLDOWN_USAGE.get(name, [])
        })

    df_cooldowns = pd.DataFrame(cooldown_results).sort_values(by="Reingewinn", ascending=False)
    st.markdown("### Übersicht: Daily Cooldowns nach aktuellem Gewinn")
    st.dataframe(df_cooldowns["Gegenstand VK-Preis Herstellkosten Reingewinn Empfehlung Preis vs Ø".split()], use_container_width=True, hide_index=True)

    st.markdown("---")
    st.markdown("### Detaillierte Zutatenanalyse und Verwendung")
    for entry in detailed_results:
        with st.expander(f"{entry['name']} — Reingewinn {format_gw2_money(entry['profit'])} | Empfehlung: {entry['recommendation']}"):
            st.markdown(f"**Aktueller Verkaufspreis:** {format_gw2_money(entry['sell_price'])} — **Herstellkosten:** {format_gw2_money(entry['craft_cost'])} — **Preis vs. 30d Ø:** {entry['price_diff']:+.1f}%")
            st.markdown("**Zutaten & Kaufempfehlungen:**")
            st.dataframe(pd.DataFrame(entry["ingredient_rows"]).rename(columns={
                "Ingredient": "Zutat",
                "Quantity": "Menge",
                "Current": "Aktueller Preis",
                "Average": "Ø Preis",
                "Diff": "Abweichung",
                "Recommendation": "Empfehlung",
                "Reason": "Begründung"
            }), use_container_width=True, hide_index=True)
            st.markdown("**Mögliche Verwendung:** " + ", ".join(entry["use_cases"]))
            st.markdown("**Warum diese Einschätzung?**")
            if entry["recommendation"] == "🟢 Kaufempfehlung":
                st.success(f"Aktueller Verkaufspreis liegt {abs(entry['price_diff']):.1f}% unter dem 30-Tage-Durchschnitt.")
            elif entry["recommendation"] == "🔴 Nicht kaufen":
                st.error(f"Aktueller Verkaufspreis liegt {entry['price_diff']:.1f}% über dem 30-Tage-Durchschnitt.")
            else:
                st.info(f"Preis liegt im Normbereich ({entry['price_diff']:+.1f}% gegenüber 30-Tage-Durchschnitt).")

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
    dust_price = get_price(MF_MATERIAL_PARE["Dust"]["t6"], "buys")
    st.markdown(f"**Crystal Dust (Einkauf):** `{format_gw2_money(dust_price)}`")

    mf_results = []
    for mat_key, ids in MF_MATERIAL_PARE.items():
        if mat_key == "Dust": continue
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
        item_id = all_history_options[selected_trend_name]
        item_history = price_history.get(str(item_id), {}).get("data", [])
        
        if len(item_history) < 2:
            st.info("Sammle noch Daten... Lade die Seite später neu, um Diagramme zu sehen.")
        else:
            df_chart = pd.DataFrame(item_history)
            df_chart["timestamp"] = pd.to_datetime(df_chart["timestamp"])
            df_chart["Verkauf (Silber)"] = df_chart["sell"] / 100
            df_chart["Einkauf (Silber)"] = df_chart["buy"] / 100
            
            # Chart anzeigen
            st.line_chart(df_chart.set_index("timestamp")[["Verkauf (Silber)", "Einkauf (Silber)"]])
            
            # Statistiken
            col1, col2, col3, col4 = st.columns(4)
            sell_prices = df_chart["sell"].tolist()
            
            with col1:
                st.metric("Aktuell", format_gw2_money(sell_prices[-1]) if sell_prices else "N/A")
            with col2:
                avg_price = sum(sell_prices) / len(sell_prices) if sell_prices else 0
                st.metric("Ø 30-Tage", format_gw2_money(int(avg_price)))
            with col3:
                st.metric("Minimum", format_gw2_money(min(sell_prices)) if sell_prices else "N/A")
            with col4:
                st.metric("Maximum", format_gw2_money(max(sell_prices)) if sell_prices else "N/A")
            
            # KI-Bewertung
            st.divider()
            st.subheader("🤖 KI-Kaufentscheidung")

            current_price = get_price(item_id, "sells")
            moving_avg, price_vals = moving_average(item_id, 30)

            if use_ai_history:
                if not get_openai_api_key():
                    st.warning("Streamlit-Secret `OPENAI_API_KEY` nicht gefunden. KI-Analyse verwendet stattdessen die lokale Heuristik.")
                    ai_active = False
                else:
                    rate_limited, wait = is_ai_rate_limited()
                    if rate_limited:
                        st.warning(f"KI-Anfragen sind auf {AI_MAX_CALLS_PER_MINUTE} pro {AI_RATE_LIMIT_WINDOW_SECONDS} Sekunden beschränkt. Bitte {wait}s warten.")
                        ai_active = False
                    else:
                        ai_active = True
            else:
                ai_active = False

            assessment = get_ai_assessment(
                selected_trend_name,
                price_vals if price_vals else sell_prices,
                current_price,
                moving_avg,
                use_ai=ai_active
            )
            
            col1, col2, col3 = st.columns([2, 3, 1])
            with col1:
                st.markdown(f"### {assessment['assessment']}")
            with col2:
                st.write(f"**Begründung:** {assessment['reasoning']}")
            with col3:
                st.write(f"**Vertrauen:** {assessment['confidence']}")
