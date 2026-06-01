import streamlit as st
import requests
import pandas as pd

API_BASE = "https://api.guildwars2.com/v2"

# --- Konfiguration & Styling ---
st.set_page_config(page_title="GW2 Make-vs-Buy", layout="wide")
st.title("⚔️ GW2 Make-vs-Buy (Bugfix Version)")

# 1. WICHTIG: Ein Button um den Cache zu leeren, falls die API hängt
if st.button("🔄 Cache leeren & Daten neu laden"):
    st.cache_data.clear()

# --- Datenbeschaffung ---
@st.cache_data(ttl=600)
def fetch_gw2_data():
    # 2. WICHTIG: Wir testen jetzt mit 100% garantierten, handelbaren Items (Barren & Buff-Food)
    top_items = [
        19684, # Mithrilbarren
        19685, # Orichalcumbarren
        19712, # Eisenbarren
        19710, # Bronzebarren
        41569, # Schüssel mit süßer und scharfer Butternusskürbissuppe
        12452  # Omnombeerenriegel
    ]
    
    # 3. WICHTIG: Wir geben uns als App aus, damit die API uns nicht blockt
    session = requests.Session()
    session.headers.update({'User-Agent': 'GW2-Streamlit-Handy-App'})
    
    recipes_map = {}
    needed_items = set(top_items)
    
    # Rezeptbäume laden
    queue = list(top_items)
    for depth in range(3):
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

    # Preise und Namen laden
    prices = {}
    item_names = {}
    needed_items_list = list(needed_items)
    
    for i in range(0, len(needed_items_list), 200):
        chunk = needed_items_list[i:i+200]
        ids_str = ",".join(map(str, chunk))
        
        p_res = session.get(f"{API_BASE}/commerce/prices?ids={ids_str}")
        if p_res.status_code == 200:
            for p in p_res.json(): prices[p["id"]] = p
                
        n_res = session.get(f"{API_BASE}/items?ids={ids_str}")
        if n_res.status_code == 200:
            for item in n_res.json(): item_names[item["id"]] = item["name"]
            
    return top_items, recipes_map, prices, item_names

# --- Make-vs-Buy Logik ---
def calc_optimal_unit_cost(item_id, prices, recipes_map):
    buy_unit_price = prices.get(item_id, {}).get("buys", {}).get("unit_price", float('inf'))
    if buy_unit_price == 0: buy_unit_price = float('inf')
    
    recipe = recipes_map.get(item_id)
    if not recipe:
        return buy_unit_price, "Kaufen", {"id": item_id, "count": 1, "action": "Kaufen", "unit_cost": buy_unit_price, "children": []}
        
    craft_unit_cost = 0
    children = []
    for ing in recipe["ingredients"]:
        ing_id = ing["item_id"]
        ing_count = ing["count"]
        
        ing_unit_cost, ing_action, ing_node = calc_optimal_unit_cost(ing_id, prices, recipes_map)
        craft_unit_cost += (ing_unit_cost * ing_count)
        
        ing_node["count"] = ing_count
        children.append(ing_node)
        
    if craft_unit_cost < buy_unit_price:
        return craft_unit_cost, "Herstellen", {"id": item_id, "count": 1, "action": "Herstellen", "unit_cost": craft_unit_cost, "children": children}
    else:
        return buy_unit_price, "Kaufen", {"id": item_id, "count": 1, "action": "Kaufen", "unit_cost": buy_unit_price, "children": []}

def format_money(copper):
    if copper == float('inf'): return "Accountgebunden"
    copper = abs(int(copper))
    g, s, c = copper // 10000, (copper % 10000) // 100, copper % 100
    if g > 0: return f"{g}g {s}s {c}c"
    elif s > 0: return f"{s}s {c}c"
    else: return f"{c}c"

def build_tree_string(node, item_names, count_multiplier, indent=""):
    name = item_names.get(node["id"], f"Item {node['id']}")
    total_count = node["count"] * count_multiplier
    cost_str = format_money(node["unit_cost"] * total_count)
    marker = "🔨 [HERSTELLEN]" if node["action"] == "Herstellen" else "💰 [KAUFEN]"
    
    line = f"{indent}- {total_count}x {name} {marker} -> {cost_str}\n"
    for child in node["children"]:
        line += build_tree_string(child, item_names, total_count, indent + "   ")
    return line

# --- App UI & Ausführung ---
try:
    with st.spinner("Lade Daten und berechne Bäume..."):
        top_items, recipes_map, prices, item_names = fetch_gw2_data()
        
        # --- NEU: DEBUG INFO FÜR DAS HANDY ---
        with st.expander("🐛 API-Status (Hier klicken für Infos)"):
            st.write(f"- Geladene Rezepte: {len(recipes_map)}")
            st.write(f"- Geladene Preise aus dem TP: {len(prices)}")
            st.write(f"- Geladene Item-Namen: {len(item_names)}")
        
        results = []
        full_data_map = {}
        
        for item_id in top_items:
            if item_id not in item_names: continue
            sell_price = prices.get(item_id, {}).get("sells", {}).get("unit_price", 0)
            if sell_price == 0: continue
            
            opt_cost, action, root_node = calc_optimal_unit_cost(item_id, prices, recipes_map)
            profit = (sell_price * 0.85) - opt_cost
            
            results.append({
                "Item": item_names[item_id],
                "Verkauf (TP)": format_money(sell_price),
                "Herstellkosten": format_money(opt_cost),
                "Netto-Profit": round(profit / 100, 2),
                "Profit Ansicht": format_money(profit),
                "Strategie": action
            })
            full_data_map[item_names[item_id]] = root_node

        if len(results) > 0:
            df = pd.DataFrame(results)
            df = df.sort_values(by="Netto-Profit", ascending=False).reset_index(drop=True)
            
            st.dataframe(df[["Item", "Verkauf (TP)", "Herstellkosten", "Profit Ansicht", "Strategie"]], use_container_width=True)

            st.markdown("### 🔍 Deep Dive: Rezept-Analyse")
            selected_item = st.selectbox("Wähle ein Item für den detaillierten Baum:", df["Item"])
            
            if selected_item:
                tree_node = full_data_map[selected_item]
                tree_text = build_tree_string(tree_node, item_names, 1)
                st.code(tree_text, language="markdown")
        else:
            st.warning("Keine Preisdaten gefunden. Bitte klicke oben auf 'Cache leeren'!")
            
except Exception as e:
    st.error(f"Es gab einen Fehler bei der API-Abfrage: {e}")
