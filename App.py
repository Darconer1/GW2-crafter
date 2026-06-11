import streamlit as st
import requests
import pandas as pd
import os
import json
import io
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

def build_flipping_history_dataframe():
    records = []
    for label, item_id in FLIP_IDS.items():
        history = price_history.get(str(item_id), {}).get('data', [])
        if len(history) < 2:
            rows = fetch_db_prices(item_id, days=120)
            history = [{
                'timestamp': r[2],
                'sell': r[0],
                'buy': r[1]
            } for r in rows if r[2]]

        for entry in history:
            try:
                ts = pd.to_datetime(entry['timestamp'])
            except Exception:
                continue
            records.append({
                'timestamp': ts,
                f'{label} Sell': entry.get('sell', 0) or 0,
                f'{label} Buy': entry.get('buy', 0) or 0
            })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    df = df.sort_values('timestamp')
    df = df.groupby('timestamp').max()
    return df


def build_history_movers(history, item_map):
    rows = []
    for label, item_id in item_map.items():
        data = history.get(str(item_id), {}).get('data', [])
        if len(data) < 2:
            continue
        df = pd.DataFrame(data)
        if df.empty or 'sell' not in df.columns:
            continue
        df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
        df = df.dropna(subset=['timestamp']).sort_values('timestamp')
        if len(df) < 2:
            continue

        first_price = int(df.iloc[0]['sell'] or 0)
        last_price = int(df.iloc[-1]['sell'] or 0)
        if first_price <= 0:
            continue

        delta = last_price - first_price
        pct_change = (delta / first_price) * 100 if first_price else 0
        rows.append({
            'Item': label,
            'Erster Preis': format_gw2_money(first_price),
            'Letzter Preis': format_gw2_money(last_price),
            'Δ Preis': format_gw2_money(delta),
            'Δ %': f"{pct_change:+.1f}%",
            'Abs Δ %': abs(pct_change),
            'Datenpunkte': len(df)
        })

    if not rows:
        return pd.DataFrame()

    dfm = pd.DataFrame(rows)
    dfm = dfm.sort_values('Abs Δ %', ascending=False)
    dfm = dfm.drop(columns=['Abs Δ %'])
    return dfm


def summarize_cooldown_profit(fee_multiplier=0.85):
    rows = []
    for cooldown_name, cooldown_id in COOLDOWN_IDS.items():
        recipe = DAILY_COOLDOWN_RECIPES.get(cooldown_name, [])
        craft_cost = 0
        for ing in recipe:
            if ing.get('id'):
                unit = get_price(ing['id'], 'buys') or get_price(ing['id'], 'sells') or 0
                craft_cost += unit * ing['qty']
            else:
                craft_cost += ing.get('qty', 0)
        revenue = get_price(cooldown_id, 'sells') * fee_multiplier
        profit = revenue - craft_cost
        rows.append({
            'Cooldown': cooldown_name,
            'Aktueller Verkauf': format_gw2_money(int(get_price(cooldown_id, 'sells'))),
            'Herstellkosten': format_gw2_money(int(craft_cost)),
            'Reingewinn': format_gw2_money(int(profit)),
            'ROI': f"{((profit / craft_cost) * 100 if craft_cost else 0):+.1f}%"
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values('Reingewinn', ascending=False)

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


@st.cache_data(ttl=28800)
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

# --- REZEPTE FÜR ECTOPLASM REFINEMENTS ---
ECTOPLASM_REFINEMENT_RECIPES = {
    "Lump of Mithrillium": [
        {"name": "Glob of Ectoplasm", "id": 19721, "qty": 1},
        {"name": "Mithril Ingot", "id": 19684, "qty": 1},
        {"name": "Thermocatalytic Reagent", "id": 46747, "qty": 1, "fixed_price": 150}
    ],
    "Glob of Elder Spirit Residue": [
        {"name": "Glob of Ectoplasm", "id": 19721, "qty": 1},
        {"name": "Elder Wood Plank", "id": 19722, "qty": 1},
        {"name": "Thermocatalytic Reagent", "id": 46747, "qty": 1, "fixed_price": 150}
    ],
    "Spool of Thick Elonian Cord": [
        {"name": "Glob of Ectoplasm", "id": 19721, "qty": 1},
        {"name": "Cured Thick Leather Square", "id": 19729, "qty": 1},
        {"name": "Thermocatalytic Reagent", "id": 46747, "qty": 1, "fixed_price": 150}
    ],
    "Spool of Silk Weaving Thread": [
        {"name": "Glob of Ectoplasm", "id": 19721, "qty": 1},
        {"name": "Bolt of Silk", "id": 19748, "qty": 1},
        {"name": "Thermocatalytic Reagent", "id": 46747, "qty": 1, "fixed_price": 150}
    ]
}

# --- REZEPTE FÜR DAILY COOLDOWN MATERIALIEN (Beispiele) ---
# Diese zeigen mögliche Verwendungen und deren Profitabilität

COOLDOWN_RECIPES = {
    "Deldrimor Steel Ingot": [
        {
            "name": "Legende Rüstungkomponente",
            "outputs": [
                {"name": "Legendary Insight", "id": 77290, "qty": 1}
            ],
            "ingredients": [
                {"name": "Deldrimor Steel Ingot", "id": 46738, "qty": 2},
                {"name": "Ecto", "id": 19721, "qty": 10},
                {"name": "Orichalcum Ore", "id": 19704, "qty": 20}
            ]
        },
        {
            "name": "Stahling-Zubehör",
            "outputs": [
                {"name": "Steel Accessory", "id": 75633, "qty": 1}
            ],
            "ingredients": [
                {"name": "Deldrimor Steel Ingot", "id": 46738, "qty": 3},
                {"name": "Silver Ore", "id": 19700, "qty": 15},
                {"name": "Copper Ore", "id": 19699, "qty": 25}
            ]
        }
    ],
    "Elonian Leather Square": [
        {
            "name": "Legendäre Leder-Rüstung",
            "outputs": [
                {"name": "Legendary Insight", "id": 77290, "qty": 1}
            ],
            "ingredients": [
                {"name": "Elonian Leather Square", "id": 46739, "qty": 2},
                {"name": "Ecto", "id": 19721, "qty": 8},
                {"name": "Thin Leather Section", "id": 19718, "qty": 30}
            ]
        },
        {
            "name": "Exotische Leder-Rüstung",
            "outputs": [
                {"name": "Exotic Leather Coat", "id": 75632, "qty": 1}
            ],
            "ingredients": [
                {"name": "Elonian Leather Square", "id": 46739, "qty": 1},
                {"name": "Thick Leather Section", "id": 19728, "qty": 20},
                {"name": "Thin Leather Section", "id": 19718, "qty": 15}
            ]
        }
    ],
    "Bolt of Damask": [
        {
            "name": "Legendäre Stoff-Rüstung",
            "outputs": [
                {"name": "Legendary Insight", "id": 77290, "qty": 1}
            ],
            "ingredients": [
                {"name": "Bolt of Damask", "id": 46741, "qty": 2},
                {"name": "Ecto", "id": 19721, "qty": 8},
                {"name": "Silk Scrap", "id": 19748, "qty": 40}
            ]
        },
        {
            "name": "Exotische Stoff-Rüstung",
            "outputs": [
                {"name": "Exotic Damask Coat", "id": 75631, "qty": 1}
            ],
            "ingredients": [
                {"name": "Bolt of Damask", "id": 46741, "qty": 1},
                {"name": "Wool Scrap", "id": 19739, "qty": 25},
                {"name": "Silk Scrap", "id": 19748, "qty": 30}
            ]
        }
    ],
    "Spiritwood Plank": [
        {
            "name": "Legendäre Waffe",
            "outputs": [
                {"name": "Legendary Insight", "id": 77290, "qty": 1}
            ],
            "ingredients": [
                {"name": "Spiritwood Plank", "id": 46736, "qty": 3},
                {"name": "Ecto", "id": 19721, "qty": 10},
                {"name": "Ancient Wood Log", "id": 19722, "qty": 40}
            ]
        },
        {
            "name": "Exotische Waffe",
            "outputs": [
                {"name": "Exotic Greatsword", "id": 75630, "qty": 1}
            ],
            "ingredients": [
                {"name": "Spiritwood Plank", "id": 46736, "qty": 2},
                {"name": "Seasoned Wood Log", "id": 19710, "qty": 35},
                {"name": "Ancient Wood Log", "id": 19722, "qty": 25}
            ]
        }
    ]
}

# --- FRAKTALE LOOT-TABELLE ---
# Basierend auf GW2 Wiki: Typische Drops pro Fractal Encryption Key

FRACTAL_LOOT_TABLE = {
    "guaranteed": [
        {"name": "Geistersplitter (Shards of Ectorium)", "id": None, "qty_min": 15, "qty_max": 25, "avg_qty": 20, "copper_value": None}
    ],
    "common_drops": [
        {"name": "Fraktal-Relikt", "id": 74166, "drop_rate": 0.35, "avg_qty": 1},
        {"name": "Flax Seed", "id": 8062, "drop_rate": 0.15, "avg_qty": 1},
        {"name": "Pile of Flax Seeds", "id": 19729, "drop_rate": 0.15, "avg_qty": 1},
        {"name": "Geistersplitter extra", "id": None, "drop_rate": 0.25, "avg_qty": 5}
    ],
    "materials": [
        {"name": "Orichalcum Ore", "id": 19704, "drop_rate": 0.08, "avg_qty": 2},
        {"name": "Mithril Ore", "id": 19684, "drop_rate": 0.08, "avg_qty": 2},
        {"name": "Ancient Wood Log", "id": 19722, "drop_rate": 0.08, "avg_qty": 2},
        {"name": "Thick Leather Section", "id": 19728, "drop_rate": 0.08, "avg_qty": 1}
    ],
    "rare": [
        {"name": "Pristine Fractal Encryption", "id": 75921, "drop_rate": 0.02, "avg_qty": 1},
        {"name": "Infusion Slot Unlock", "id": 77508, "drop_rate": 0.005, "avg_qty": 1}
    ]
}


def get_item_average(item_id, days=30):
    if item_id is None:
        return None
    ma, _ = moving_average(item_id, days)
    return ma

def get_item_average(item_id, days=30):
    if item_id is None:
        return None
    ma, _ = moving_average(item_id, days)
    return ma

def calculate_price_change(item_id, days=30):
    """
    Berechnet die Preisveränderung eines Items über X Tage.
    Returns: (first_price, last_price, change_absolute, change_percent)
    """
    try:
        # Versuche, aus der Datenbank zu laden
        rows = fetch_db_prices_simple(item_id, days)
        if len(rows) >= 2:
            first_price = rows[0]
            last_price = rows[-1]
            change_abs = last_price - first_price
            change_pct = (change_abs / first_price * 100) if first_price > 0 else 0
            return first_price, last_price, change_abs, change_pct
    except Exception:
        pass
    
    # Fallback zu lokaler JSON
    try:
        hist = price_history.get(str(item_id), {}).get('data', [])
        if len(hist) >= 2:
            first_price = int(hist[0].get('sell', 0) or 0)
            last_price = int(hist[-1].get('sell', 0) or 0)
            if first_price > 0:
                change_abs = last_price - first_price
                change_pct = (change_abs / first_price * 100)
                return first_price, last_price, change_abs, change_pct
    except Exception:
        pass
    
    return None, None, None, None

# --- PROFIT-ANALYSE FÜR REZEPTE ---
def calculate_recipe_profit(recipe, fee_multiplier=0.85):
    """
    Berechnet den Gewinn für ein bestimmtes Rezept.
    recipe = {"name": ..., "outputs": [...], "ingredients": [...]}
    """
    total_input_cost = 0
    total_output_value = 0
    
    # Kosten der Zutaten
    for ingredient in recipe["ingredients"]:
        item_id = ingredient.get("id", 0)
        price = get_price(item_id, "buys") or 0
        total_input_cost += price * ingredient["qty"]
    
    # Wert der Outputs (Verkauf)
    for output in recipe["outputs"]:
        price = get_price(output.get("id", 0), "sells") or 0
        total_output_value += price * output["qty"] * fee_multiplier
    
    profit = total_output_value - total_input_cost
    return {
        "name": recipe["name"],
        "input_cost": total_input_cost,
        "output_value": total_output_value,
        "profit": profit,
        "roi": ((profit / total_input_cost) * 100) if total_input_cost > 0 else 0
    }

def get_best_recipes(cooldown_material_name, top_n=3):
    """
    Gibt die besten Rezepte für ein Daily Cooldown Material zurück
    """
    recipes = COOLDOWN_RECIPES.get(cooldown_material_name, [])
    if not recipes:
        return []
    
    results = []
    for recipe in recipes:
        profit_data = calculate_recipe_profit(recipe)
        results.append(profit_data)
    
    # Sortiere nach Profit
    return sorted(results, key=lambda x: x["profit"], reverse=True)[:top_n]

# --- FRACTAL LOOT ANALYSE ---
def calculate_fractal_loot_value(num_keys=1, fee_multiplier=0.85, relic_per_shard=28, shard_unit_price=0):
    """
    Berechnet den erwarteten Wert des Loots aus Fractal Encryption Keys
    unter Berücksichtigung aller Drops und Handelsplatzgebühren.
    - Konvertiert Fraktal-Relikte in Geistersplitter-Wert über `relic_per_shard`.
    - Behandelt Items ohne ID (z.B. Shards) separat.
    """
    results = {
        "total_value": 0,
        "items": [],
        "by_category": {}
    }

    # Bestimme Relikt-/Shard-Wert
    relic_price = get_price(FRACTAL_RELIC_ID, "sells") or get_price(FRACTAL_RELIC_ID, "buys") or 0
    if shard_unit_price and shard_unit_price > 0:
        shard_value = shard_unit_price * fee_multiplier
    else:
        shard_value = (relic_price / relic_per_shard * fee_multiplier) if relic_price > 0 else 0

    # Guaranteed drops (Geistersplitter)
    for item in FRACTAL_LOOT_TABLE["guaranteed"]:
        avg_qty = item.get("avg_qty", 0)
        total_shards = avg_qty * num_keys
        total_value = total_shards * shard_value
        results["items"].append({
            "name": item["name"],
            "qty": int(total_shards),
            "unit_value": format_gw2_money(int(shard_value)) if shard_value else "N/A",
            "total_value": total_value,
            "type": "guaranteed"
        })
        results["by_category"]["guaranteed"] = results["by_category"].get("guaranteed", 0) + total_value

    # Common drops
    for item in FRACTAL_LOOT_TABLE.get("common_drops", []):
        if item.get("id"):
            price = get_price(item["id"], "sells") or get_price(item["id"], "buys") or 0
            unit_value = format_gw2_money(int(price))
            expected_qty = item.get("avg_qty", 0) * item.get("drop_rate", 0) * num_keys
            total_value = expected_qty * price * fee_multiplier
        else:
            # Items without ID considered as extra shards
            price = shard_value
            unit_value = format_gw2_money(int(shard_value)) if shard_value else "N/A"
            expected_qty = item.get("avg_qty", 0) * item.get("drop_rate", 0) * num_keys
            total_value = expected_qty * shard_value

        results["items"].append({
            "name": item["name"],
            "drop_rate": f"{item.get('drop_rate',0)*100:.0f}%",
            "expected_qty": round(expected_qty, 2),
            "unit_value": unit_value,
            "total_value": total_value,
            "type": "common"
        })
        results["by_category"]["common"] = results["by_category"].get("common", 0) + total_value

    # Materials
    for item in FRACTAL_LOOT_TABLE.get("materials", []):
        price = get_price(item["id"], "sells") or get_price(item["id"], "buys") or 0
        expected_qty = item.get("avg_qty", 0) * item.get("drop_rate", 0) * num_keys
        total_value = expected_qty * price * fee_multiplier

        results["items"].append({
            "name": item["name"],
            "drop_rate": f"{item.get('drop_rate',0)*100:.0f}%",
            "expected_qty": round(expected_qty, 2),
            "unit_value": format_gw2_money(int(price)),
            "total_value": total_value,
            "type": "material"
        })
        results["by_category"]["materials"] = results["by_category"].get("materials", 0) + total_value

    # Rare drops
    for item in FRACTAL_LOOT_TABLE.get("rare", []):
        price = get_price(item["id"], "sells") or get_price(item["id"], "buys") or 0
        expected_qty = item.get("avg_qty", 0) * item.get("drop_rate", 0) * num_keys
        total_value = expected_qty * price * fee_multiplier

        results["items"].append({
            "name": item["name"],
            "drop_rate": f"{item.get('drop_rate',0)*100:.2f}%",
            "expected_qty": round(expected_qty, 3),
            "unit_value": format_gw2_money(int(price)),
            "total_value": total_value,
            "type": "rare"
        })
        results["by_category"]["rare"] = results["by_category"].get("rare", 0) + total_value

    # Gesamtwert
    results["total_value"] = sum(results["by_category"].values())
    results["shard_value"] = shard_value
    results["relic_price"] = relic_price
    return results


FIXED_FRACTAL_TABLE_CSV = '''Drop Rate,Item Name,API ID,Buy Price
0.2812,"Manuscript of 'Halfway There and...'",Junk,60 Silver 00 Copper
0.2868,"Postulate of Construction",Junk,20 Silver 00 Copper
0.4309,"Proof of Bask's Theorem",Junk,30 Silver 00 Copper
0.2871,"Treatise on Convergence",Junk,25 Silver 00 Copper
0.28271,"Vial of Potent Blood",24294,75 Copper
0.300645,"Large Bone",24341,58 Copper
0.294865,"Large Claw",24350,66 Copper
0.291295,"Large Scale",24288,60 Copper
0.280245,"Large Fang",24356,60 Copper
0.29155,"Intricate Totem",24299,68 Copper
0.29087,"Potent Venom Sac",24282,62 Copper
0.28645,"Pile of Incandescent Dust",24276,1 Silver 83 Copper
0.017255,"Mini Professor Mew",48099,2 Copper
1.95245,"+1 Agony Infusion",49424,12 Copper
'''

def parse_user_table(csv_text):
    """
    Erwartet CSV mit Header: Drop Rate,Item Name,API ID,Buy Price
    Liefert Liste von dicts: {drop_rate, name, id, buy_price_copper}
    """
    rows = []
    try:
        df = pd.read_csv(io.StringIO(csv_text))
    except Exception:
        # try simple split fallback
        lines = [l.strip() for l in csv_text.strip().splitlines() if l.strip()]
        header = []
        for i, line in enumerate(lines):
            if i == 0:
                header = [h.strip() for h in line.split(',')]
                continue
            parts = [p.strip().strip('"') for p in line.split(',')]
            if len(parts) < 4:
                continue
            try:
                dr = float(parts[0])
            except:
                dr = 0
            name = parts[1]
            api_id = parts[2]
            buy = parts[3]
            rows.append({'drop_rate': dr, 'name': name, 'api_id': api_id, 'buy_price': buy})
        return rows

    for _, r in df.iterrows():
        try:
            dr = float(r.iloc[0])
        except Exception:
            dr = 0
        name = r.iloc[1]
        api_id = r.iloc[2]
        buy_raw = r.iloc[3]
        # parse buy price like '60 Silver 00 Copper' or '75 Copper' or '1 Silver 83 Copper' or '2 Copper'
        buy_copper = 0
        try:
            if isinstance(buy_raw, str):
                parts = buy_raw.split()
                # e.g. ['60','Silver','00','Copper'] or ['60','Silver']
                vals = [p for p in parts if p.isdigit()]
                # manual parse for common formats
                if 'Gold' in buy_raw or 'gold' in buy_raw or 'g' in buy_raw:
                    # not expected, skip
                    buy_copper = 0
                else:
                    # Convert Silver/Copper pairs
                    if 'Silver' in buy_raw:
                        # find first number = silver, last number = copper if present
                        nums = [int(x) for x in parts if x.isdigit()]
                        if len(nums) == 1:
                            buy_copper = nums[0] * 100
                        elif len(nums) >= 2:
                            buy_copper = nums[0] * 100 + nums[1]
                    elif 'Copper' in buy_raw or buy_raw.strip().isdigit():
                        nums = [int(x) for x in parts if x.isdigit()]
                        buy_copper = nums[0] if nums else 0
                    else:
                        buy_copper = 0
            else:
                buy_copper = int(buy_raw)
        except Exception:
            buy_copper = 0

        rows.append({'drop_rate': dr, 'name': name, 'api_id': api_id, 'buy_price': buy_copper})
    return rows

FIXED_FRACTAL_DROPS = parse_user_table(FIXED_FRACTAL_TABLE_CSV)


def analyze_user_table(rows, num_keys=1, fee_multiplier=0.85):
    """Berechnet erwarteten Wert pro Key basierend auf gegebenen Rows"""
    # collect numeric ids to fetch
    ids = [int(r['api_id']) for r in rows if str(r['api_id']).isdigit()]
    live_map, dbg = fetch_live_prices(ids) if ids else ({}, [])

    # update history for fetched ids
    for r in rows:
        if str(r['api_id']).isdigit():
            iid = int(r['api_id'])
            info = live_map.get(iid, {})
            update_history_entry(price_history, iid, r['name'], info.get('sells', {}).get('unit_price', 0), info.get('buys', {}).get('unit_price', 0))

    save_price_history(price_history)

    results = []
    total_expected = 0
    for r in rows:
        dr = r['drop_rate']
        name = r['name']
        api_id = r['api_id']
        buy_price = r.get('buy_price', 0)

        if str(api_id).isdigit():
            iid = int(api_id)
            market_sell = live_map.get(iid, {}).get('sells', {}).get('unit_price', 0)
            market_buy = live_map.get(iid, {}).get('buys', {}).get('unit_price', 0)
            unit = market_sell or market_buy or 0
            immediate_vendor = 0
        else:
            # Junk items: use provided buy_price as vendor sale (fixed)
            unit = 0
            immediate_vendor = buy_price

        expected_qty = dr * num_keys
        # value if sold on TP (after fee)
        tp_value = expected_qty * (unit * fee_multiplier)
        vendor_value = expected_qty * immediate_vendor
        # recommendation based on history
        rec = None
        reason = None
        if str(api_id).isdigit():
            ma, vals = moving_average(iid, 30)
            current = unit
            if ma:
                if current < ma * 0.9:
                    rec = 'lagern'
                    reason = f'aktuell {format_gw2_money(current)} < 30d Ø {format_gw2_money(int(ma))}'
                elif current > ma * 1.1:
                    rec = 'sofort verkaufen'
                    reason = f'aktuell {format_gw2_money(current)} > 30d Ø {format_gw2_money(int(ma))}'
                else:
                    rec = 'abwarten'
                    reason = 'Preis im Bereich des 30d Ø'
            else:
                rec = 'keine Daten'
                reason = 'keine Historie'

        item_total = tp_value + vendor_value
        total_expected += item_total

        results.append({
            'name': name,
            'api_id': api_id,
            'drop_rate': dr,
            'expected_qty': expected_qty,
            'unit_market': unit,
            'tp_value': tp_value,
            'vendor_value': vendor_value,
            'recommendation': rec,
            'reason': reason
        })

    return {'items': results, 'total_value': total_expected, 'debug': dbg}


def calculate_key_cost(num_keys):
    tier1 = min(num_keys, 30)
    tier2 = min(max(num_keys - 30, 0), 30)
    tier3 = max(num_keys - 60, 0)

    cost_20s = tier1 * 2000
    cost_25s4c = tier2 * 2504
    cost_30s = tier3 * 3000

    return cost_20s + cost_25s4c + cost_30s, {
        '20s (1-30)': cost_20s,
        '25s 4c (31-60)': cost_25s4c,
        '30s (61+)': cost_30s
    }


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

# --- ECTOPLASM REFINEMENTS (Neue tägliche Cooldowns) ---
ECTOPLASM_REFINEMENTS = {
    "Lump of Mithrillium": 46753,
    "Glob of Elder Spirit Residue": 46754,
    "Spool of Thick Elonian Cord": 46755,
    "Spool of Silk Weaving Thread": 46756
}

# Handelbares Basismaterial für Ectoplasm Refinements
ECTOPLASM_BASE_MATERIALS = {
    "Glob of Ectoplasm": 19721,
    "Mithril Ingot": 19684,
    "Elder Wood Plank": 19722,
    "Cured Thick Leather Square": 19729,
    "Bolt of Silk": 19748
}

# NPC-Item mit festem Preis (nicht handelbar)
THERMOCATALYTIC_REAGENT_ID = 46747
THERMOCATALYTIC_REAGENT_PRICE = 149  # 1,49 Silber = 149 Kupfer

# Mapping: Ascended Material -> Daily Cooldown Item
ASCENDED_TO_COOLDOWN = {
    "Lump of Mithrillium": "Deldrimor Steel Ingot",
    "Glob of Elder Spirit Residue": "Spiritwood Plank",
    "Spool of Thick Elonian Cord": "Elonian Leather Square",
    "Spool of Silk Weaving Thread": "Bolt of Damask"
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

FLIP_IDS = {
    "Evergreen Sliver": 68952,
    "Evergreen Lodestone": 68942
}

# Fraktale-Items
FRACTAL_RELIC_ID = 74166
PRISTINE_ENCRYPTION_ID = 75921
INFUSION_ID = 77508
LEGENDARY_INSIGHT_ID = 77290

ALL_IDS = list(COOLDOWN_IDS.values()) + list(ECTOPLASM_REFINEMENTS.values()) + list(ECTOPLASM_BASE_MATERIALS.values()) + list(RAW_MAT_IDS.values()) + list(FLIP_IDS.values()) + [ECTO_ID, ENCRYPTION_ID, FRACTAL_RELIC_ID, PRISTINE_ENCRYPTION_ID, INFUSION_ID, LEGENDARY_INSIGHT_ID, THERMOCATALYTIC_REAGENT_ID]
for p in MF_MATERIAL_PARE.values():
    ALL_IDS.extend([p["t5"], p["t6"]])
# Ergänze alle IDs aus der Fractal Loot Tabelle (falls vorhanden), damit wir Preise für diese Items laden
for cat in FRACTAL_LOOT_TABLE.values():
    for entry in cat:
        if entry.get("id"):
            ALL_IDS.append(entry["id"])
# Ergänze IDs aus der fixen Fraktal-Drop-Liste
ALL_IDS.extend([int(r['api_id']) for r in FIXED_FRACTAL_DROPS if str(r['api_id']).isdigit()])

ALL_IDS = list(set(ALL_IDS))

# Ergänze IDs aus COOLDOWN_RECIPES (Outputs und Zutaten)
for recipes in COOLDOWN_RECIPES.values():
    for r in recipes:
        for ing in r.get("ingredients", []):
            if ing.get("id"):
                ALL_IDS.append(ing.get("id"))
        for out in r.get("outputs", []):
            if out.get("id"):
                ALL_IDS.append(out.get("id"))

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
    for name, idx in {**COOLDOWN_IDS, **ECTOPLASM_REFINEMENTS, **ECTOPLASM_BASE_MATERIALS, **RAW_MAT_IDS, **FLIP_IDS}.items():
        info = live_data.get(idx, {})
        update_history_entry(price_history, idx, name, info.get("sells", {}).get("unit_price", 0), info.get("buys", {}).get("unit_price", 0))
    for row in FIXED_FRACTAL_DROPS:
        if str(row['api_id']).isdigit():
            iid = int(row['api_id'])
            info = live_data.get(iid, {})
            update_history_entry(price_history, iid, row['name'], info.get("sells", {}).get("unit_price", 0), info.get("buys", {}).get("unit_price", 0))
    save_price_history(price_history)

with st.sidebar:
    st.header("⚙️ Einstellungen")
    tp_fee_toggle = st.checkbox("Handelsposten-Gebühr abziehen (15 %)", value=True)
    fee_multiplier = 0.85 if tp_fee_toggle else 1.0
    st.subheader("💎 Geistersplitter-Wertung")
    relic_per_shard = st.number_input("Fraktal-Relikte pro Geistersplitter", value=28)
    shard_unit_price = st.number_input("Geistersplitter-Wert pro Stück (Kupfer, 0 = auto)", value=0)
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

    all_history_options = {**COOLDOWN_IDS, **ECTOPLASM_REFINEMENTS, **ECTOPLASM_BASE_MATERIALS, **RAW_MAT_IDS}

def get_price(item_id, mode="buys"):
    return live_data.get(item_id, {}).get(mode, {}).get("unit_price", 0)

tab0, tab1, tab2, tab3, tab4, tab5 = st.tabs(["🏠 Übersicht", "🕒 Daily Cooldowns", "📉 Fraktale", "🔮 Mystic Forge", "📊 Historie", "🔁 Flipping"])

with tab0:
    st.header("🏠 Startseite / Übersicht")
    st.markdown("Schneller Überblick über die wichtigsten Markttrends, historische Preisbewegungen und Handelschancen.")

    history_movers_df = build_history_movers(price_history, all_history_options)
    best_cooldowns = summarize_cooldown_profit(fee_multiplier)

    sliver_id = FLIP_IDS.get("Evergreen Sliver")
    lodestone_id = FLIP_IDS.get("Evergreen Lodestone")
    sliver_buy = get_price(sliver_id, "buys")
    lodestone_sell = get_price(lodestone_id, "sells")
    sliver_cost = sliver_buy * 16
    current_revenue = int(lodestone_sell * 0.85)
    flip_profit = current_revenue - sliver_cost
    flip_status = "Rentabel" if flip_profit > 0 else "Nicht rentabel" if flip_profit < 0 else "Break-even"

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("Getrackte Materialien", len(all_history_options))
        st.metric("Live Items aus API", len(live_data))
        st.metric("Historische Preis-Tracker", len(price_history))
    with col2:
        if not history_movers_df.empty:
            top_mover = history_movers_df.iloc[0]
            st.metric("Stärkster historischer Mover", top_mover['Item'], top_mover['Δ %'])
        else:
            st.metric("Stärkster historischer Mover", "Keine Daten", "-")
        if not best_cooldowns.empty:
            best = best_cooldowns.iloc[0]
            st.metric("Bestes Cooldown-Gewinnpotenzial", best['Cooldown'], best['Reingewinn'])
        else:
            st.metric("Bestes Cooldown-Gewinnpotenzial", "Keine Daten", "-")
        st.metric("Flipping-Status", flip_status, format_gw2_money(int(flip_profit)))
    with col3:
        st.metric("Aktuelle API-Last", f"{len(live_data)}/{len(ALL_IDS)} Items", "Live-Daten")
        st.metric("Top 5 Historische Mover", "Zeigt nachfolgend", "Sortiert nach Δ %")
        st.metric("KI aktiviert", "In Sidebar steuerbar", "Deaktiviert standardmäßig")

    st.subheader("Top 5 Historische Bewegungen")
    if history_movers_df.empty:
        st.info("Noch nicht genügend historische Preisdaten vorhanden.")
    else:
        st.dataframe(history_movers_df.head(5), use_container_width=True, hide_index=True)

    st.subheader("Beste Daily Cooldown Chancen")
    if best_cooldowns.empty:
        st.info("Keine Cooldown-Daten verfügbar.")
    else:
        st.dataframe(best_cooldowns.head(5)[['Cooldown', 'Aktueller Verkauf', 'Herstellkosten', 'Reingewinn', 'ROI']], use_container_width=True, hide_index=True)

    st.subheader("Flipping Kurzcheck")
    st.write(f"- Evergreen Sliver Buy: {format_gw2_money(sliver_buy)}")
    st.write(f"- Evergreen Lodestone Sell: {format_gw2_money(lodestone_sell)}")
    st.write(f"- Netto-Gewinn bei aktuell 16 Slivers → 1 Lodestone: {format_gw2_money(int(flip_profit))}")


def get_price(item_id, mode="buys"):
    return live_data.get(item_id, {}).get(mode, {}).get("unit_price", 0)

# --- TAB 1: DAILY COOLDOWN PLANER (Ectoplasm Refinements) ---
with tab1:
    st.header("⏳ Ectoplasm Refinements & Daily Cooldowns")
    st.markdown("Zeigt die profitabelsten Ectoplasm Refinements und Kaufempfehlungen für Basis-Materialien auf Basis des 30-Tage-Trends.")

    # --- 1. NEU: Preistabelle Basis-Materialien (Sortiert nach Preisverfall) ---
    st.subheader("📊 Handelbare Basis-Materialien (Kauf-Radar)")
    base_mat_rows = []
    
    for name, item_id in ECTOPLASM_BASE_MATERIALS.items():
        current_price = get_price(item_id, "sells") or get_price(item_id, "buys")
        avg_price = get_item_average(item_id, 30)
        
        diff_pct = 0
        if avg_price and avg_price > 0 and current_price:
            diff_pct = ((current_price - avg_price) / avg_price) * 100
            
        rec = ingredient_price_assessment(name, item_id, 1)
        
        base_mat_rows.append({
            "Material": name,
            "Aktueller Preis": format_gw2_money(current_price) if current_price else "0c",
            "30d Ø Preis": format_gw2_money(int(avg_price)) if avg_price else "N/A",
            "Preisverfall (%)": diff_pct, # Versteckte Spalte für die Sortierung
            "Abweichung": f"{diff_pct:+.1f}%" if avg_price else "N/A",
            "Empfehlung": rec["Recommendation"]
        })
        
    if base_mat_rows:
        df_base = pd.DataFrame(base_mat_rows)
        # Sortieren nach Preisverfall (stärkster Drop / negativster Wert ganz oben)
        df_base = df_base.sort_values(by="Preisverfall (%)", ascending=True)
        # Hilfsspalte für das UI entfernen
        display_df = df_base.drop(columns=["Preisverfall (%)"])
        st.dataframe(display_df, use_container_width=True, hide_index=True)
    else:
        st.info("Lade Daten für Basis-Materialien...")

    st.divider()

    # --- 2. NEU: Die 4 Ectoplasm Refinements ---
    st.subheader("🛠️ Refinement Cooldowns")
    for ref_name, ref_id in ECTOPLASM_REFINEMENTS.items():
        with st.expander(f"{ref_name}", expanded=False):
            recipe = ECTOPLASM_REFINEMENT_RECIPES.get(ref_name, [])
            st.markdown("**Benötigte Materialien:**")
            
            ing_rows = []
            craft_cost = 0
            
            for ing in recipe:
                # Prüfen, ob es sich um ein NPC-Item mit festem Preis handelt (Thermocatalytic Reagent)
                if ing.get("fixed_price"):
                    unit_price = ing["fixed_price"]
                    cost = unit_price * ing["qty"]
                    craft_cost += cost
                    ing_rows.append({
                        "Zutat": ing["name"],
                        "Menge": ing["qty"],
                        "Kosten/Aktuell": format_gw2_money(cost),
                        "Empfehlung": "Fixkosten",
                        "Begründung": "NPC-Item"
                    })
                else:
                    unit_price = get_price(ing.get("id"), "buys") or get_price(ing.get("id"), "sells") or 0
                    craft_cost += unit_price * ing["qty"]
                    row = ingredient_price_assessment(ing["name"], ing.get("id"), ing["qty"])
                    ing_rows.append({
                        "Zutat": ing["name"],
                        "Menge": ing["qty"],
                        "Kosten/Aktuell": row["Current"],
                        "Empfehlung": row["Recommendation"],
                        "Begründung": row["Reason"]
                    })
                    
            st.dataframe(pd.DataFrame(ing_rows), use_container_width=True, hide_index=True)
            
            # Profit-Berechnung für das Refinement
            sell_price = get_price(ref_id, "sells")
            revenue = sell_price * fee_multiplier
            profit = revenue - craft_cost
            
            st.markdown(f"**Herstellkosten:** {format_gw2_money(int(craft_cost))}  —  **Verkauf (nach Gebühren):** {format_gw2_money(int(revenue))}")
            
            if profit > 0:
                st.success(f"💰 Direkter Verkauf lohnt: Reingewinn {format_gw2_money(int(profit))}")
            else:
                st.warning(f"⚠️ Direkter Verkauf bringt {format_gw2_money(int(profit))} (Verlust).")

# --- TAB 2: FRAKTAL RENDITE ---
with tab2:
    st.header("📉 Fraktal-Boxen & Loot-Analyse")
    
    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        enc_amount = st.number_input("Anzahl Boxen (zu öffnen)", value=100, step=10, min_value=1)
        free_keys = st.number_input("Kostenlose Fractal Encryption Keys (Eigenbestand)", value=0, min_value=0, step=1)

    total_key_cost, key_cost_breakdown = calculate_key_cost(enc_amount)
    enc_sell_price = get_price(ENCRYPTION_ID, "sells")
    enc_buy_price = get_price(ENCRYPTION_ID, "buys") or enc_sell_price
    user_analysis = analyze_user_table(FIXED_FRACTAL_DROPS, num_keys=enc_amount, fee_multiplier=fee_multiplier)
    expected_loot_value = user_analysis['total_value']
    expected_loot_per_key = expected_loot_value / enc_amount if enc_amount > 0 else 0
    direct_sell_value = (enc_amount * enc_sell_price) * fee_multiplier
    open_profit = expected_loot_value - total_key_cost
    direct_profit = direct_sell_value - total_key_cost

    with col2:
        st.subheader("Schlüssel-Kosten & Verkauf")
        st.metric("Gesamtkosten für Keys", format_gw2_money(int(total_key_cost)))
        
        # Helfer-Funktion für die farbige Anzeige des Gewinns pro Schlüssel
        def get_key_profit_str(loot_val, key_cost):
            profit = loot_val - key_cost
            color = "green" if profit >= 0 else "red"
            sign = "+" if profit >= 0 else "-"
            # abs() nutzen, da format_gw2_money sonst bei negativen Werten "0s 0c" ausgibt
            formatted_money = format_gw2_money(int(abs(profit)))
            label = "Gewinn/Key" if profit >= 0 else "Verlust/Key"
            return f":{color}[**{sign}{formatted_money}** {label}]"

        # Anzeige der 3 Preis-Stufen inklusive farbigem Profit
        st.write(f"- 20s (1-30 Keys): {format_gw2_money(int(key_cost_breakdown['20s (1-30)']))} | {get_key_profit_str(expected_loot_per_key, 2000)}")
        st.write(f"- 25s 4c (31-60 Keys): {format_gw2_money(int(key_cost_breakdown['25s 4c (31-60)']))} | {get_key_profit_str(expected_loot_per_key, 2504)}")
        st.write(f"- 30s (61+ Keys): {format_gw2_money(int(key_cost_breakdown['30s (61+)']))} | {get_key_profit_str(expected_loot_per_key, 3000)}")
        
        st.metric("Aktuelle TP-Verkaufsrate für Boxen", format_gw2_money(int(enc_sell_price)))
        st.metric("Erlös bei Direktverkauf", format_gw2_money(int(direct_sell_value)))
        st.metric("Netto-Gewinn beim Verkauf", format_gw2_money(int(direct_profit)))

    with col3:
        st.subheader("Öffnen mit fixer Drop-Tabelle")
        st.metric("Erwarteter Loot-Wert gesamt", format_gw2_money(int(expected_loot_value)))
        st.metric("Erwarteter Wert pro Öffnung", format_gw2_money(int(expected_loot_per_key)))
        st.metric("Gewinn beim Öffnen", format_gw2_money(int(open_profit)))
        if expected_loot_per_key == 0:
            st.warning("Die Analyse konnte keinen erwarteten Loot-Wert pro Öffnung bestimmen. Bitte prüfe die Tabelle oder API-Daten.")

    with st.expander("📥 Fixe Drop-Tabelle (CSV, unveränderlich)", expanded=True):
        st.text_area("Fixe Drop-Tabelle", value=FIXED_FRACTAL_TABLE_CSV, height=260, disabled=True)

    st.subheader("🔎 Analyse der fixen Drop-Tabelle")
    df_rows = []
    for it in user_analysis['items']:
        df_rows.append({
            'Item': it['name'],
            'API ID': it['api_id'],
            'Drop-Rate': it['drop_rate'],
            'Erw. Menge (gesamt)': round(it['expected_qty'], 3),
            'TP-Wert (nach Geb.)': format_gw2_money(int(it['tp_value'])),
            'Sofort-Verkauf (Vendor)': format_gw2_money(int(it['vendor_value'])) if it['vendor_value'] > 0 else '-',
            'Empfehlung': it['recommendation'] or '-',
            'Begründung': it['reason'] or '-'
        })

    st.dataframe(pd.DataFrame(df_rows), use_container_width=True, hide_index=True)
    st.markdown(f"**Erwarteter Gesamtwert für {enc_amount} geöffnete Boxen:** {format_gw2_money(int(expected_loot_value))}")
    st.markdown(f"**Durchschnittlicher erwarteter Wert pro Öffnung:** {format_gw2_money(int(expected_loot_per_key))}")

    st.markdown("**Vergleich mit aktuellem Box-Verkauf**")
    st.write(f"- Erwarteter Loot-Wert (laut fixer Tabelle): {format_gw2_money(int(expected_loot_value))}")
    st.write(f"- Direktverkauf aller Boxen im TP: {format_gw2_money(int(direct_sell_value))}")
    if expected_loot_value > direct_sell_value:
        st.success(f"Öffnen könnte besser sein (+{format_gw2_money(int(expected_loot_value - direct_sell_value))})")
    else:
        st.warning(f"Direktverkauf besser (+{format_gw2_money(int(direct_sell_value - expected_loot_value))})")

    if free_keys > 0:
        st.divider()
        st.subheader("🆓 Szenario: Kostenlose Fractal Encryption Keys")
        buy_price = enc_buy_price
        st.write(f"Aktueller TP-Kaufpreis für Boxen: {format_gw2_money(int(buy_price))}")
        st.write(f"Maximal rentabler Einkaufspreis pro Verschlüsselung: {format_gw2_money(int(expected_loot_per_key))}")
        free_profit = (expected_loot_per_key - buy_price) * free_keys
        if buy_price and free_profit > 0:
            st.success(f"Empfehlung: Kaufen und öffnen, wenn der TP-Kaufpreis unter dem erwarteten Loot-Wert liegt.\nPotentieller Gewinn mit {free_keys} kostenlosen Keys: {format_gw2_money(int(free_profit))}.")
        elif buy_price and free_profit <= 0:
            st.warning(f"Empfehlung: Nicht kaufen. Der aktuelle TP-Kaufpreis ist höher als der erwartete Loot-Wert.\nWarte auf günstigere Encryption-Preise oder verkaufe die Keys direkt in späteren Situationen.")
        else:
            st.info("Aktueller TP-Kaufpreis für Boxen ist nicht verfügbar. Bitte prüfen Sie die API oder schließen Sie den Streamlit-Neustart nicht aus.")

    st.divider()
    st.subheader("💡 Rentabilitätsanalyse")
    col_a, col_b = st.columns([1, 1])

    with col_a:
        if open_profit > direct_profit:
            difference = open_profit - direct_profit
            st.success(f"🚀 **ÖFFNEN LOHNT SICH!**\n\nDurch Öffnen und Verkauf des Loots verdient ihr **{format_gw2_money(int(difference))}** mehr als durch Direktverkauf.\n\n**ROI beim Öffnen:** {((open_profit / total_key_cost) * 100):.1f}%\n**ROI beim Verkauf:** {((direct_profit / total_key_cost) * 100):.1f}%")
        elif direct_profit > open_profit:
            difference = direct_profit - open_profit
            st.warning(f"⚖️ **DIREKT VERKAUFEN BESSER**\n\nDirektverkauf bringt **{format_gw2_money(int(difference))}** mehr Gewinn.\n\n**ROI beim Verkauf:** {((direct_profit / total_key_cost) * 100):.1f}%\n**ROI beim Öffnen:** {((open_profit / total_key_cost) * 100):.1f}%")
        else:
            st.info("💭 **GLEICHWERTIG**\n\nBeide Optionen liefern ähnliche Renditen.")

    with col_b:
        if open_profit > direct_profit and direct_profit != 0:
            savings_pct = ((open_profit - direct_profit) / abs(direct_profit) * 100)
            st.metric("Zusatz-Gewinn durch Öffnen", f"+{savings_pct:.1f}%")
        elif direct_profit > open_profit and open_profit != 0:
            loss_pct = ((direct_profit - open_profit) / abs(open_profit) * 100)
            st.metric("Potentieller Verlust durch Öffnen", f"-{loss_pct:.1f}%")
        else:
            st.metric("Vergleich", "Keine eindeutige Differenz")

    st.divider()
    st.markdown("**ℹ️ Hinweise zur Analyse:**")
    st.markdown("""
    - **Drop-Raten** basieren auf GW2-Community-Daten und können variieren
    - **Preise** stammen aus Live-API-Daten (alle 60 Sekunden aktualisiert)
    - **Gebühren** beinhalten die **15%** Handelsposten-Verkaufsgebühr
    - **Geistersplitter** werden über Fraktal-Relikte berechnet (1 Relikt ≈ 28 Shards)
    - Die Analyse geht von optimalem Loot-Verkauf aus (alle Items auf TP)
    """)


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
    st.markdown("Erste Übersicht: Alle trackbaren Materialien nach der größten historischen Preisänderung.")
    history_movers_df = build_history_movers(price_history, all_history_options)

    if history_movers_df.empty:
        st.info("Noch nicht genügend historische Preisdaten gesammelt. Bitte warte auf den nächsten Preisabgleich.")
    else:
        st.dataframe(history_movers_df, use_container_width=True, hide_index=True)

    st.markdown("---")
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

# --- TAB 5: FLIPPING ANALYSE ---
with tab5:
    st.header("🔁 Flipping: Evergreen Sliver ↔ Evergreen Lodestone")
    st.markdown(
        "Vergleicht historische Marktpreise für Evergreen Sliver und Evergreen Lodestone und prüft, ob der Flipping-Trade aktuell rentabel ist."
    )

    sliver_id = FLIP_IDS["Evergreen Sliver"]
    lodestone_id = FLIP_IDS["Evergreen Lodestone"]
    sliver_buy = get_price(sliver_id, "buys")
    lodestone_sell = get_price(lodestone_id, "sells")

    sliver_cost = sliver_buy * 16
    net_sell_multiplier = 0.85  # nur Verkauf unterliegt der 15%-Handelsposten-Gebühr
    min_sell_price = math.ceil(sliver_cost / net_sell_multiplier) if sliver_cost > 0 else 0
    current_revenue = int(lodestone_sell * net_sell_multiplier)
    net_profit = current_revenue - sliver_cost
    profit_ok = net_profit > 0
    profit_label = "Rentabel" if profit_ok else "Nicht rentabel" if net_profit < 0 else "Break-even"

    col1, col2 = st.columns(2)
    with col1:
        st.metric("Sliver Buy-Order (1)", format_gw2_money(sliver_buy))
        st.metric("Kosten für 16 Slivers", format_gw2_money(sliver_cost))
        st.metric("Mindestverkaufspreis (vor 15 % Gebühr)", format_gw2_money(min_sell_price))
    with col2:
        st.metric("Lodestone Sell-Order (1)", format_gw2_money(lodestone_sell))
        st.metric("Erlös nach 15 % Gebühren", format_gw2_money(current_revenue))
        st.metric("Aktueller Netto-Profit", format_gw2_money(net_profit))

    if profit_ok:
        st.success(f"🚀 Aktuell rentabel: {format_gw2_money(net_profit)} Profit")
    elif net_profit < 0:
        st.warning(f"⚠️ Aktuell nicht rentabel: {format_gw2_money(net_profit)} Verlust")
    else:
        st.info("⚖️ Break-even: Aktuell keine Gewinnspanne.")

    if min_sell_price > 0:
        diff = lodestone_sell - min_sell_price
        if diff >= 0:
            st.write(f"Der aktuelle Sell-Order liegt **{format_gw2_money(diff)} über** dem Break-Even-Preis.")
        else:
            st.write(f"Der aktuelle Sell-Order liegt **{format_gw2_money(abs(diff))} unter** dem Break-Even-Preis.")

    st.divider()
    st.subheader("📈 Historische Preisentwicklung")
    df_flip_history = build_flipping_history_dataframe()
    if df_flip_history.empty:
        st.info("Es sind noch keine historischen Daten für diesen Flip verfügbar. Bitte warte auf den nächsten Preisabgleich.")
    else:
        st.line_chart(df_flip_history)
        if "Evergreen Sliver Buy" in df_flip_history.columns and "Evergreen Lodestone Sell" in df_flip_history.columns:
            df_flip_history = df_flip_history.copy()
            df_flip_history["Estimated Net Profit"] = (df_flip_history["Evergreen Lodestone Sell"] * 0.85) - (df_flip_history["Evergreen Sliver Buy"] * 16)
            st.line_chart(df_flip_history[["Estimated Net Profit"]])
