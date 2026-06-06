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
def calculate_fractal_loot_value(num_keys=1, fee_multiplier=0.85, relic_per_shard=28):
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

    # Bestimme Relikt-/Shard-Wert (Fallbacks auf buys wenn sells fehlt)
    relic_price = get_price(FRACTAL_RELIC_ID, "sells") or get_price(FRACTAL_RELIC_ID, "buys") or 0
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

# Fraktale-Items
FRACTAL_RELIC_ID = 74166
PRISTINE_ENCRYPTION_ID = 75921
INFUSION_ID = 77508
LEGENDARY_INSIGHT_ID = 77290

ALL_IDS = list(COOLDOWN_IDS.values()) + list(RAW_MAT_IDS.values()) + [ECTO_ID, ENCRYPTION_ID, FRACTAL_RELIC_ID, PRISTINE_ENCRYPTION_ID, INFUSION_ID, LEGENDARY_INSIGHT_ID]
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

# --- TAB 1: DAILY COOLDOWN PLANER (Ascended Material View) ---
with tab1:
    st.header("🕒 Ascended-Materialien & Daily Cooldowns")
    st.markdown("Zeigt für jedes ascended Material die benötigten Komponenten, Kaufempfehlungen und mögliche Weiterverarbeitung.")

    for asc_name, cooldown_name in ASCENDED_TO_COOLDOWN.items():
        st.markdown("---")
        with st.expander(f"{asc_name} → {cooldown_name}", expanded=False):
            st.markdown(f"**Cooldown-Item:** {cooldown_name}")

            # Zutaten für das Herstellen des Cooldowns
            recipe = DAILY_COOLDOWN_RECIPES.get(cooldown_name, [])
            st.markdown("**Benötigte Items für den Cooldown:**")
            ing_rows = []
            for ing in recipe:
                row = ingredient_price_assessment(ing["name"], ing.get("id"), ing["qty"])
                ing_rows.append(row)
            df_ing = pd.DataFrame(ing_rows).rename(columns={
                "Ingredient": "Zutat",
                "Quantity": "Menge",
                "Current": "Aktueller Preis",
                "Average": "Ø Preis",
                "Diff": "Abweichung",
                "Recommendation": "Empfehlung",
                "Reason": "Begründung"
            })
            st.dataframe(df_ing, use_container_width=True, hide_index=True)

            # Herstellkosten und Verkauf des Cooldowns
            cooldown_id = COOLDOWN_IDS.get(cooldown_name)
            sell_price = get_price(cooldown_id, "sells")
            # Berechne Herstellkosten aus recipe (falls vorhanden)
            craft_cost = 0
            for ing in recipe:
                if ing.get("id"):
                    unit = get_price(ing.get("id"), "buys") or get_price(ing.get("id"), "sells") or 0
                    craft_cost += unit * ing.get("qty", 0)
                else:
                    # feste Gebühr
                    craft_cost += ing.get("qty", 0)

            revenue = sell_price * fee_multiplier
            profit = revenue - craft_cost

            st.markdown(f"**Herstellkosten:** {format_gw2_money(int(craft_cost))}  —  **Verkauf (nach Gebühren):** {format_gw2_money(int(revenue))}")
            if profit > 0:
                st.success(f"💰 Direkter Verkauf des Cooldowns lohnt: Reingewinn {format_gw2_money(int(profit))}")
            else:
                st.warning(f"⚠️ Direkter Verkauf des Cooldowns bringt {format_gw2_money(int(profit))} (Verlust).")

            # Begründung / Empfehlung für Vorratskäufe der Zutaten
            st.markdown("**Kaufempfehlungen für Zutaten (Vorrat):**")
            for _, r in df_ing.iterrows():
                st.write(f"- **{r['Zutat']}**: {r['Empfehlung']} — {r['Begründung']}")

            # Welche Items kann man mit dem Cooldown herstellen (höherwertige Rezepte)?
            st.divider()
            st.markdown("**Mögliche Weiterverarbeitung / Rezepte mit diesem Cooldown:**")
            cooldown_recipes = COOLDOWN_RECIPES.get(cooldown_name, [])
            if cooldown_recipes:
                out_rows = []
                for r in cooldown_recipes:
                    profit_data = calculate_recipe_profit(r, fee_multiplier=fee_multiplier)
                    # Kaufempfehlungen für zusätzliche Komponenten (außer dem Cooldown)
                    add_comps = []
                    for ing in r.get("ingredients", []):
                        if ing.get("name") == cooldown_name or ing.get("id") is None:
                            continue
                        comp_assess = ingredient_price_assessment(ing["name"], ing.get("id"), ing.get("qty"))
                        add_comps.append((ing["name"], comp_assess["Recommendation"], comp_assess["Reason"]))

                    out_rows.append({
                        "Rezept": r["name"],
                        "Eingabe-Kosten": format_gw2_money(int(profit_data["input_cost"])),
                        "Ausgabe-Wert": format_gw2_money(int(profit_data["output_value"])),
                        "Reingewinn": format_gw2_money(int(profit_data["profit"])),
                        "ROI": f"{profit_data['roi']:+.1f}%",
                        "Komponenten-Empfehlungen": ", ".join([f"{c[0]}: {c[1]} ({c[2]})" for c in add_comps])
                    })

                st.dataframe(pd.DataFrame(out_rows), use_container_width=True, hide_index=True)
            else:
                st.info("Keine Weiterverarbeitungsrezepte für dieses Item hinterlegt.")

# --- TAB 2: FRAKTAL RENDITE ---
with tab2:
    st.header("📉 Fraktal-Verschlüsselungen & Loot-Analyse")
    
    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        enc_amount = st.number_input("Anzahl Verschlüsselungen", value=100, step=10)
        key_source = st.selectbox("Schlüssel-Einkauf", ["Tiefenrabatt (20 Silber)", "Rabattiert (30 Silber)", "Normalpreis (50 Silber)"])
    
    key_cost_unit = {"Tiefenrabatt (20 Silber)": 2000, "Rabattiert (30 Silber)": 3000, "Normalpreis (50 Silber)": 5000}[key_source]
    enc_sell_price = get_price(ENCRYPTION_ID, "sells")
    
    with col2:
        st.subheader("Direktverkauf")
        total_key_cost = enc_amount * key_cost_unit
        total_sell_revenue = (enc_amount * enc_sell_price) * fee_multiplier
        direct_profit = total_sell_revenue - total_key_cost
        
        st.metric("Eingabe", format_gw2_money(total_key_cost))
        st.metric("Verkaufserlös", format_gw2_money(total_sell_revenue))
        st.metric("Netto-Gewinn", format_gw2_money(int(direct_profit)))
    
    # --- DETAILLIERTE LOOT-ANALYSE ---
    with col3:
        st.subheader("Öffnen & Verkaufen")
        loot_analysis = calculate_fractal_loot_value(enc_amount, fee_multiplier, relic_per_shard)
        loot_value = loot_analysis["total_value"]
        loot_profit = loot_value - total_key_cost
        
        st.metric("Loot-Wert", format_gw2_money(int(loot_value)))
        st.metric("Netto-Gewinn", format_gw2_money(int(loot_profit)))
    
    st.divider()
    
    # --- EMPFEHLUNG ---
    st.subheader("💡 Rentabilitätsanalyse")
    col_a, col_b = st.columns([1, 1])
    
    with col_a:
        if loot_profit > direct_profit:
            difference = loot_profit - direct_profit
            st.success(f"🚀 **ÖFFNEN LOHNT SICH!**\n\nDurch Öffnen der Keys verdient ihr **{format_gw2_money(int(difference))}** mehr als durch Direktverkauf.\n\n**ROI beim Öffnen:** {((loot_profit/total_key_cost)*100):.1f}%\n**ROI beim Verkauf:** {((direct_profit/total_key_cost)*100):.1f}%")
        elif direct_profit > loot_profit:
            difference = direct_profit - loot_profit
            st.warning(f"⚖️ **DIREKT VERKAUFEN BESSER**\n\nDirektverkauf bringt **{format_gw2_money(int(difference))}** mehr Gewinn.\n\n**ROI beim Verkauf:** {((direct_profit/total_key_cost)*100):.1f}%\n**ROI beim Öffnen:** {((loot_profit/total_key_cost)*100):.1f}%")
        else:
            st.info("💭 **GLEICHWERTIG**\n\nBoth options yield similar profit margins.")
    
    with col_b:
        if loot_profit > direct_profit:
            savings_pct = ((loot_profit - direct_profit) / direct_profit * 100)
            st.metric("Zusatz-Gewinn durch Öffnen", f"+{savings_pct:.1f}%")
        else:
            loss_pct = ((direct_profit - loot_profit) / loot_profit * 100)
            st.metric("Potentieller Verlust durch Öffnen", f"-{loss_pct:.1f}%")
    
    st.divider()
    
    # --- DETAILLIERTE LOOT-TABELLE ---
    st.subheader("📋 Detaillierte Loot-Berechnung")
    
    # Kategorisierte Anzeige
    tab_loot_common, tab_loot_mat, tab_loot_rare, tab_loot_summary = st.tabs(
        ["🎁 Häufige Drops", "⛏️ Materialien", "💎 Seltene Items", "📊 Zusammenfassung"]
    )
    
    with tab_loot_common:
        st.markdown("**Häufig vorkommende Loot-Items**")
        common_items = [item for item in loot_analysis["items"] if item["type"] == "common"]
        if common_items:
            common_df = pd.DataFrame(common_items)[["name", "drop_rate", "expected_qty", "unit_value", "total_value"]]
            common_df.columns = ["Item", "Drop-Rate", "Erwartete Menge", "Einheit", "Wert TP"]
            st.dataframe(common_df, use_container_width=True, hide_index=True)
            total_common = sum(item["total_value"] for item in common_items)
            st.metric("Kategoriegewinn", format_gw2_money(int(total_common)))
    
    with tab_loot_mat:
        st.markdown("**Handwerksmaterialien**")
        mat_items = [item for item in loot_analysis["items"] if item["type"] == "material"]
        if mat_items:
            mat_df = pd.DataFrame(mat_items)[["name", "drop_rate", "expected_qty", "unit_value", "total_value"]]
            mat_df.columns = ["Material", "Drop-Rate", "Erwartete Menge", "Einheit", "Wert TP"]
            st.dataframe(mat_df, use_container_width=True, hide_index=True)
            total_mat = sum(item["total_value"] for item in mat_items)
            st.metric("Kategoriegewinn", format_gw2_money(int(total_mat)))
    
    with tab_loot_rare:
        st.markdown("**Seltene & Wertvolle Items**")
        rare_items = [item for item in loot_analysis["items"] if item["type"] == "rare"]
        if rare_items:
            rare_df = pd.DataFrame(rare_items)[["name", "drop_rate", "expected_qty", "unit_value", "total_value"]]
            rare_df.columns = ["Item", "Drop-Rate", "Erwartete Menge", "Einheit", "Wert TP"]
            st.dataframe(rare_df, use_container_width=True, hide_index=True)
            total_rare = sum(item["total_value"] for item in rare_items)
            st.metric("Kategoriegewinn", format_gw2_money(int(total_rare)))
        else:
            st.info("Keine seltenen Items in dieser Analyse.")
    
    with tab_loot_summary:
        st.markdown("**Gewinn-Übersicht**")
        summary_data = []
        
        for category, value in loot_analysis["by_category"].items():
            if value > 0:
                summary_data.append({
                    "Kategorie": category.replace("_", " ").title(),
                    "Wert": format_gw2_money(int(value)),
                    "Anteil": f"{(value/loot_value*100):.1f}%" if loot_value > 0 else "0%"
                })
        
        if summary_data:
            st.dataframe(pd.DataFrame(summary_data), use_container_width=True, hide_index=True)
        
        st.divider()
        col_s1, col_s2, col_s3, col_s4 = st.columns(4)
        with col_s1:
            st.metric("Schlüssel-Kosten", format_gw2_money(int(total_key_cost)))
        with col_s2:
            st.metric("Loot-Wert (brutto)", format_gw2_money(int(loot_analysis["total_value"])))
        with col_s3:
            st.metric("Nach TP-Gebühren", format_gw2_money(int(loot_value)))
        with col_s4:
            st.metric("Reingewinn", format_gw2_money(int(loot_profit)))
    
    st.divider()
    st.markdown("**ℹ️ Hinweise zur Analyse:**")
    st.markdown("""
    - **Drop-Raten** basieren auf GW2-Community-Daten und können variieren
    - **Preise** stammen aus Live-API-Daten (alle 60 Sekunden aktualisiert)
    - **Gebühren** beinhalten die 15% Handelsposten-Verkaufsgebühr
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
