# ⚔️ GW2 Crafter - Profit- & Handwerks-Optimierer

Ein intelligentes Werkzeug zur Maximierung Ihrer Gewinne in **Guild Wars 2** durch Marktanalyse und KI-gestützte Kaufempfehlungen.

## ✨ Features

### 📊 Live-Marktdaten
- Echtzeit-Preise von der offiziellen GW2 API
- Unterstützung für 37+ Materialien
- Automatische Fehlerbehandlung (inkl. HTTP 206 Support)

### 🕒 Tägliche Veredelung
- Gewinnberechnung für Cooldown-Rezepte
- Automatische Kostenkalkulation
- Handelsposten-Gebühren berücksichtigen

### 📉 Fraktal-Analysen
- Verschlüsselungs-Optimierung
- Öffnungs-Rentabilität vs. Direktverkauf
- Flexible Schlüssel-Kostenmodelle

### 🔮 Mystic Forge Kalkulatoren
- T5→T6 Upgrades mit Gewinnberechnung
- 8 Materialtypen (Blut, Knochen, Klaue, etc.)
- Kristalliner-Staub-Kosten-Integration

### 📊 Historische Datenanalyse
- 30-Tage-Durchschnittspreise
- Min/Max-Tracking
- Trends und Volatilität
- **🤖 KI-gestützte Kaufempfehlungen** (GPT-3.5)

### 🔄 Automatische Preis-Updates (GitHub Actions)
- Alle **8 Stunden** automatische Updates
- Datenbank-Persistierung
- Auto-Commits ins Repository
- Keine externe Abhängigkeiten

---

## 🚀 Quick Start

### Installation

```bash
# Repository klonen
git clone https://github.com/Darconer1/GW2-crafter.git
cd GW2-crafter

# Dependencies installieren
pip install -r requirements.txt

# Streamlit App starten
streamlit run App.py
```

### Erste Nutzung

1. Öffnen Sie die App (http://localhost:8501)
2. Warten Sie auf das erste API-Update (oder triggern Sie `python update_prices.py`)
3. Nutzen Sie die Tabs zum Optimieren:
   - **🕒 Daily Cooldowns** - Tägliche Handwerk-Pläne
   - **📉 Fraktale** - Verschlüsselungs-Rentabilität
   - **🔮 Mystic Forge** - Material-Upgrades
   - **📊 Historie** - Historische Trends + 🤖 **KI-Empfehlungen**

---

## 🤖 KI-Kaufempfehlungen aktivieren

Die App nutzt **OpenAI GPT-3.5** für intelligente Preis-Analysen:

### Setup:

1. **OpenAI API-Key holen** (https://platform.openai.com/api-keys)

2. **Lokal nutzen:**
   ```bash
   export OPENAI_API_KEY="sk-..."
   streamlit run App.py
   ```

3. **Oder GitHub Secrets setzen:**
   - Gehen Sie zu **Settings** → **Secrets and variables** → **Actions**
   - Neues Secret: `OPENAI_API_KEY = "sk-..."`
   - Die KI analysiert automatisch alle 8 Stunden!

### Die KI bewertet:
- ✅ Aktueller Preis vs. Durchschnitt
- ✅ Preis-Trends der letzten 30 Tage
- ✅ Volatilität und Risiko
- ✅ Kaufzeitpunkt-Optimierung

**💡 Fallback ohne API-Key:** App funktioniert mit einfachen Heuristiken!

---

## 📡 GitHub Actions - Automatische Updates

Die Preis-Updates laufen automatisch:

- **Zeitplan:** Täglich um 0:00, 8:00, 16:00 UTC
- **Was passiert:**
  1. GW2 API abrufen
  2. Preise in Datenbank speichern
  3. JSON-Historie aktualisieren
  4. Automatisch ins Repo committen
- **Manuelle Trigger:** Actions → "Run Workflow"

Siehe [SETUP_GUIDE.md](SETUP_GUIDE.md) für detaillierte Instruktionen.

---

## 📋 Daten-Übersicht

### Unterstützte Materialien

#### Tägliche Cooldowns (4)
- Deldrimor-Stahlbarren
- Elonischer Lederquadrat
- Chiffon-Ballen
- Geistreichen-Holzplanke

#### Rohstoffe (17)
- Erze (Mithril, Eisen, Platin)
- Leder (Dick, Dünn, Grob, Rauh)
- Stoffe (Seide, Wolle, Baumwolle, Leinen)
- Holz (Alt, Geschmeidig, Abgelagert, Hart)

#### Mystic Forge Materialien (8 Paare)
- Blut (T5/T6)
- Knochen (T5/T6)
- Klaue (T5/T6)
- Fangzahn (T5/T6)
- Schuppe (T5/T6)
- Giftbeutel (T5/T6)
- Totem (T5/T6)
- Staub (T5/T6)

#### Spezial
- Ectos (19721)
- Fraktal-Verschlüsselungen (75919)

---

## 📊 Datenbank-Details

- **Typ:** SQLite (`price_history.db`)
- **Aufbewahrung:** Letzte 120 Tage
- **Struktur:** item_id, timestamp, sell, buy
- **JSON-Backup:** `price_history.json` (100 Einträge pro Item)

---

## ⚙️ Einstellungen

In der **Sidebar** können Sie anpassen:
- ✓ Handelsposten-Gebühren (15%) ein/aus
- ✓ Fraktal-Relikte pro Geistersplitter (Default: 28)

---

## 🐛 Bugfixes in dieser Version

- ✅ **HTTP 206 Support:** API gibt nun 206 (Partial Content) - wird korrekt verarbeitet
- ✅ **JSON-Korruption-Handling:** Beschädigte Dateien werden automatisch repariert
- ✅ **Timeout-Optimierung:** Erhöhte Timeouts für zuverlässigere API-Calls

---

## 📚 Weitere Ressourcen

- [SETUP_GUIDE.md](SETUP_GUIDE.md) - Detaillierte Einrichtungsanleitung
- [.env.example](.env.example) - Umgebungsvariablen-Template
- GW2 Wiki: https://wiki.guildwars2.com/
- OpenAI Docs: https://platform.openai.com/docs/

---

## 📝 Lizenz

Dieses Projekt nutzt die öffentliche GW2 API (https://wiki.guildwars2.com/wiki/API:Main)

---

**🚀 Happy Crafting! Maximieren Sie Ihre GW2-Gewinne!** 
