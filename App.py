import streamlit as st
import requests
import pandas as pd
import json

API_BASE = "https://api.guildwars2.com/v2"

st.set_page_config(page_title="GW2 Make-vs-Buy", layout="wide")
st.title("⚔️ GW2 Make-vs-Buy (Namens-Fix)")

if st.button("🔄 Cache leeren & Daten neu laden"):
    st.cache_data.clear()

@st.cache_data(ttl=600)
def fetch_gw2_data():
    debug_log = []
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            config_data = json.load(f)
            debug_log.append("✅ config.json erfolgreich geladen")
    except Exception as e:
        debug_log.append(f"❌ Fehler config.json: {e}")
        return [], {}, {}, {}, debug_log

    top_items = []
    for category, items in config_data.items():
        if isinstance(items, dict):
            top_items.extend(items.values())
    debug_log.append(f"ℹ️ {len(top_items)} Start-Items aus JSON gelesen")

    session = requests.Session()
    session.headers.update({'User-Agent': 'GW2-Streamlit-Handy-App'})
    
    recipes_map = {}
    needed_items = set(top_items)
    
    # Rezeptbäume laden
    queue = list(top_items)
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
    debug_log.append(f"ℹ️ {len(recipes_map)} Rezepte gefunden, {len(needed_items)} Einzel-Items benötigt")

    valid_tp_ids = set()
    tp_res = session.get(f"{API_BASE}/commerce/prices")
    if tp_res.status_code == 200:
        valid_tp_ids = set(tp_res.json())
        debug_log.append(f"✅ {len(valid_tp_ids)} TP-IDs geladen")

    needed_items_list = [i for i in needed_items if i in valid_tp_ids]
    
    prices = {}
    for i in range(0, len(needed_items_list), 200):
        chunk = needed_items_list[i:i+200]
        ids_str = ",".join(map(str, chunk))
        p_res = session.get(f"{API_BASE}/commerce/prices?ids={ids_str}")
        if p_res.status_code == 200:
            for p in p_res.json(): prices[p["id"]] = p
    debug_log.append(f"✅ {len(prices)} Preise geladen")
                
    item_names = {}
    all_needed_items = list(needed_items)
    for i in range(0, len(all_needed_items), 200):
        chunk = all_needed_items[i:i+200]
        ids_str = ",".join(map(str, chunk))
        
        # FIX: &lang=de für deutsche Namen und Status 206 (Partial Content) akzeptieren
        n_res = session.get(f"{API_BASE}/items?ids={ids_str}&lang=de")
        
        if n_res.status_code in [200, 206]:
            try:
                for item in n_res.json(): 
                    item_names[item["id"]] = item["name"]
            except Exception as e:
                debug_log.append(f"⚠️ JSON-Fehler bei Namen: {e}")
        else:
            debug_log.append(f"⚠️ Warnung: Namens-Abfrage fehlgeschlagen (Status {n_res.status_code}).")
    
    debug_log.append(f"✅ {len(item_names)} Namen erfolgreich geladen")
            
    return top_items, recipes_map, prices, item_names, debug_log

# --- Logik ---
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
        cost_str = "🔒 Accountgebunden (Farmen/Craften)"
    else:
        cost_str = format_money(node["unit_cost"] * total_count)
        
    marker = "🔨" if node["action"] == "Herstellen" else "💰/🎒"
    
    line = f"{indent}- {total_count}x {name} {marker} -> {cost_str}\n"
    for child in node["children"]:
        line += build_tree_string(child, item_names, total_count, indent + "   ")
    return line

# --- App UI ---
try:
    with st.spinner("Lade Daten und analysiere..."):
        top_items, recipes_map, prices, item_names, debug_log = fetch_gw2_data()
        
        with st.expander("🛠️ Debug-Log (Bei Fehlern aufklappen)", expanded=False):
            for log_entry in debug_log:
                st.write(log_entry)
        
        results = []
        full_data_map = {}
        
        for item_id in top_items:
            display_name = item_names.get(item_id, f"Unbekanntes Item ({item_id})")
            
            sell_price = prices.get(item_id, {}).get("sells", {}).get("unit_price", 0)
            opt_cost, action, root_node = calc_optimal_unit_cost(item_id, prices, recipes_map)
            
            profit = (sell_price * 0.85) - opt_cost if sell_price > 0 else 0
            
            results.append({
                "Item": display_name,
                "Verkauf (TP)": format_money(sell_price) if sell_price > 0 else "🔒 Accountgebunden",
                "Herstellkosten": format_money(opt_cost),
                "Netto-Profit": round(profit / 100, 2) if sell_price > 0 else 0,
                "Profit Ansicht": format_money(profit) if sell_price > 0 else "-",
                "Strategie": action
            })
            full_data_map[display_name] = root_node

        if len(results) > 0:
            df = pd.DataFrame(results)
            df = df.sort_values(by="Netto-Profit", ascending=False).reset_index(drop=True)
            
            st.dataframe(df[["Item", "Verkauf (TP)", "Herstellkosten", "Profit Ansicht", "Strategie"]], use_container_width=True)

            st.markdown("### 🔍 Deep Dive: Rezept-Baum")
            st.info("💡 🔨 = Herstellen | 💰/🎒 = Kaufen oder Farmen")
            
            selected_item = st.selectbox("Wähle ein Item für den detaillierten Baum:", df["Item"])
            
            if selected_item:
                tree_node = full_data_map[selected_item]
                tree_text = build_tree_string(tree_node, item_names, 1)
                st.code(tree_text, language="markdown")
        else:
            st.error("Es wurden Items verarbeitet, aber die Liste ist leer. Bitte schau ins Debug-Log!")
            
except Exception as e:
    st.error(f"Es gab einen kritischen Fehler: {e}")
