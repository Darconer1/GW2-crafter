import streamlit as st

# Das sorgt dafür, dass die App auf dem Handy im Quer- und Hochformat gut aussieht
st.set_page_config(page_title="GW2 Handy Crafter", layout="wide")

st.title("⚔️ GW2 Handy Crafter")
st.write("Willkommen in deinem Trading-Tool! Die Verbindung steht.")

# Ein kleiner Test-Schieberegler
gold_ziel = st.slider("Wie viel Gold möchtest du heute machen?", 10, 500, 100)
st.write(f"Dein Ziel für heute: {gold_ziel} Gold. Packen wir es an!")
