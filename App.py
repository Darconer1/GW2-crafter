import streamlit as st
import requests
import pandas as pd
import json

API_BASE = "https://api.guildwars2.com/v2"

st.set_page_config(page_title="GW2 Make-vs-Buy", layout="wide")
st.title("⚔️ GW2 Make-vs-Buy (JSON + Spidy Live)")

if st.button("🔄 Cache leeren & Daten neu laden"):
    st.cache_data.clear()

# --- Hilfsfunktionen für das Laden ---

def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

@st.cache_data(ttl=3600)  # Spidy-Daten 1 Stunde cachen
def fetch_spidy_top_ids(discipline_id, top_n=15):
    try:
        url = f"https://www.gw2spidy.com/api/v0.9/json/recipes-for-discipline/{discipline_id}"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            results = res.json().get("results", [])
            
            # Grobe Vor-Sortierung anhand der Spidy-Daten
            for r in results:
                spidy_sell = r.get("result_item_min_sale_unit_price", 0)
                spidy_cost = r.get("crafting_cost", float('inf'))
                
                if spidy_sell > 0 and spidy_cost > 0:
                    r["profit_margin"] = (spidy_sell * 0.85) - spidy_cost
                else:
                    r["profit_margin"] = -999999
                    
            results.sort(key=lambda x: x.get("profit_margin", -999999), reverse=True)
            
            top_ids = []
            for r in results:
                item_id = r.get("result_item_data_id")
                if item_id and item_id not in top_ids:
                    top_ids.append(item_id)
                if len(top_ids) >= top_n:
                    break
            return top_ids
    except Exception as e:
        st.warning(f"Konnte GW2Spidy nicht erreichen: {e}")
        return []
    return []

@st.cache_data(ttl=600) # GW2 Live-Daten 10 Minuten cachen
def fetch_gw2_data(target_ids):
    debug_log = []
    session = requests.Session()
    session.headers.update({'User-Agent': 'GW2-Streamlit-Handy-App'})
    
    recipes_map = {}
    needed_items = set(target_ids)
    
    # Rezeptbäume laden (On-Demand)
    queue = list(target_ids)
    for depth in range(4):
        next_queue = set()
        for q_id in queue:
            if q_id in recipes_map: continue
            res = session.get(f"{API_BASE}/recipes/search?output={q_id}")
            if res.status_code == 200 and res.json():
                r_id = res.json()[0]
                rec_res = session.get(f"{API_BASE}/recipes/{r_id}")
                if rec_res.status_code == 200:
                    recipe = rec_res.json()
                    recipes_map[q_id] = recipe
                    for ing in recipe['ingredients']:
                        next_queue.add(ing['item_id'])
                        needed_items.add(ing['item_id'])
        queue = list(next_queue)
    debug_log.append(f"ℹ️ {len(recipes_map)} Rezepte für die Auswahl geladen.")

    valid_tp_ids = set()
    tp_res = session.get(f"{API_BASE}/commerce/prices")
    if tp_res.status_code == 200:
        valid_tp_ids = set(tp_res.json())

    needed_items_list = [i for i in needed_items if i in valid_tp_ids]
    
    prices = {}
    for i in range(0, len(needed_items_list), 200):
        chunk = needed_items_list[i:i+200]
        ids_str = ",".join(map(str, chunk))
        p_res = session.get(f"{API_BASE}/commerce/prices?ids={ids_str}")
        if p_res.status_code == 200:
            for p in p_res.json(): prices[p["id"]] = p
                
    item_names = {}
    all_needed_items = list(needed_items)
    for i in range(0, len(all_needed_items), 200):
        chunk = all_needed_items[i:i+200]
        ids_str = ",".join(map(str, chunk))
        n_res = session.get(f"{API_BASE}/items?ids={ids_str}&lang=de")
        
        if n_res.status_code in [200, 206]:
            try:
                for item in n_res.json(): 
                    item_names[item["id"]] = item["name"]
            except Exception: pass
            
    return recipes_map, prices, item_names, debug_log

# --- Rechen-Logik ---
def calc_optimal_unit_cost(item_id, prices, recipes_map):
    buy_unit_price = prices.get(item_id, {}).get("buys", {}).get("unit_price", 0)
    is_account_bound = buy_unit_price == 0
    if is_account_bound: buy_unit_price = float('inf')
    
    recipe = recipes_map.get(item_id)
    if not recipe:
        return buy_unit_price, "Kaufen/Farmen", {"id": item_id, "count": 1, "action": "Kaufen/Farmen", "unit_cost": buy_unit_price, "children": []}
        
    craft_unit_cost = 0
    children = []
    for ing in recipe["ingredients"]:
        ing_id = ing["item_id"]
        ing_count = ing["count"]
        
        ing_unit_cost, ing_action, ing_node = calc_optimal_unit_cost(ing_id, prices, recipes_map)
        if ing_unit_cost != float('inf'):
            craft_unit_cost += (ing_unit_cost * ing_count)
        
        ing_node["count"] = ing_count
        children.append(ing_node)
        
    if craft_unit_cost < buy_unit_price or is_account_bound:
        return craft_unit_cost, "Herstellen", {"id": item_id, "count": 1, "action": "Herstellen", "unit_cost": craft_unit_cost, "children": children}
    else:
        return buy_unit_price, "Kaufen", {"id": item_id, "count": 1, "action": "Kaufen/Farmen", "unit_cost": buy_unit_price, "children": []}

def format_money(copper):
    if copper == float('inf'): return "🔒 Accountgebunden"
    if copper == 0: return "0c"
    copper = abs(int(copper))
    g, s, c = copper // 10000, (copper % 10000) // 100, copper % 100
    if g > 0: return f"{g}g {s}s {c}c"
    elif s > 0: return f"{s}s {c}c"
    else: return f"{c}c"

def build_tree_string(node, item_names, count_multiplier, indent=""):
    name = item_names.get(node["id"], f"Unbekanntes Item ({node['id']})")
    total_count = node["count"] * count_multiplier
    
    if node["unit_cost"] == float('inf'):
        cost_str = "🔒 Accountgebunden"
    else:
        cost_str = format_money(node["unit_cost"] * total_count)
        
    marker = "🔨" if node["action"] == "Herstellen" else "💰/🎒"
    
    line = f"{indent}- {total_count}x {name} {marker} -> {cost_str}\n"
    for child in node["children"]:
        line += build_tree_string(child, item_names, total_count, indent + "   ")
    return line

# --- App UI & Steuerung ---
try:
    config_data = load_config()
    
    st.markdown("### ⚙️ Steuerung")
    hide_account_bound = st.checkbox("🚫 Accountgebundene Items ausblenden (Zeige nur profitable)", value=True)
    
    # Optionen für das Dropdown bauen
    json_categories = list(config_data.keys())
    spidy_categories = ["🔥 Live: Waffenschmied Top 15 (Spidy)", "🔥 Live: Lederer Top 15 (Spidy)"]
    all_options = ["⭐ Alle Favoriten (JSON)"] + json_categories + ["---"] + spidy_categories
    
    selected_option = st.selectbox("📂 Wähle, was analysiert werden soll:", all_options)
    
    items_to_process = []
    
    # Entscheiden, welche IDs geladen werden sollen
    with st.spinner("Frage IDs ab..."):
        if selected_option == "⭐ Alle Favoriten (JSON)":
            for cat, items in config_data.items():
                items_to_process.extend(items.values() if isinstance(items, dict) else items)
        elif selected_option == "🔥 Live: Waffenschmied Top 15 (Spidy)":
            items_to_process = fetch_spidy_top_ids(2, 15)  # 2 = Waffenschmied
        elif selected_option == "🔥 Live: Lederer Top 15 (Spidy)":
            items_to_process = fetch_spidy_top_ids(5, 15)  # 5 = Lederer
        elif selected_option != "---":
            # Eine spezifische JSON Kategorie wurde gewählt
            cat_data = config_data.get(selected_option, {})
            items_to_process = list(cat_data.values()) if isinstance(cat_data, dict) else cat_data
            
    if not items_to_process and selected_option != "---":
        st.warning("Keine Items gefunden oder API nicht erreichbar.")
    elif selected_option != "---":
        with st.spinner("Berechne reale TP-Preise & Rezeptbäume..."):
            recipes_map, prices, item_names, debug_log = fetch_gw2_data(items_to_process)
            
            with st.expander("🛠️ Debug-Log", expanded=False):
                for log_entry in debug_log: st.write(log_entry)
            
            results = []
            full_data_map = {}
            
            for item_id in items_to_process:
                sell_price = prices.get(item_id, {}).get("sells", {}).get("unit_price", 0)
                
                if hide_account_bound and sell_price == 0: continue
                    
                display_name = item_names.get(item_id, f"Item ({item_id})")
                opt_cost, action, root_node = calc_optimal_unit_cost(item_id, prices, recipes_map)
                
                profit = (sell_price * 0.85) - opt_cost if sell_price > 0 else 0
                
                results.append({
                    "Item": display_name,
                    "Verkauf (TP)": format_money(sell_price) if sell_price > 0 else "🔒",
                    "Herstellkosten": format_money(opt_cost),
                    "Netto-Profit": round(profit / 100, 2) if sell_price > 0 else -9999,
                    "Profit Ansicht": format_money(profit) if sell_price > 0 else "-",
                    "Strategie": action
                })
                full_data_map[display_name] = root_node

            if len(results) > 0:
                df = pd.DataFrame(results)
                # Entferne Items mit negativem/unbekanntem Profit aus der reinen Sortierung
                df = df.sort_values(by="Netto-Profit", ascending=False).reset_index(drop=True)
                
                st.dataframe(df[["Item", "Verkauf (TP)", "Herstellkosten", "Profit Ansicht", "Strategie"]], use_container_width=True)

                st.markdown("### 🔍 Deep Dive: Rezept-Baum")
                selected_item = st.selectbox("Wähle ein Item für den detaillierten Baum:", df["Item"])
                
                if selected_item:
                    tree_node = full_data_map[selected_item]
                    tree_text = build_tree_string(tree_node, item_names, 1)
                    st.code(tree_text, language="markdown")
            else:
                st.warning("Nach dem Filtern sind keine Items mehr übrig.")
                
except Exception as e:
    st.error(f"Kritischer Fehler: {e}")
