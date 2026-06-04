# 🚀 Setup-Anleitung für GW2 Crafter mit GitHub Actions & KI

Diese Anleitung erklärt, wie Sie die neuen Funktionen einrichten:

## 1️⃣ **GitHub Actions - Automatische Preis-Updates alle 8 Stunden**

Die GitHub Actions Workflow ist bereits konfiguriert und läuft automatisch:
- **Datei:** `.github/workflows/update_prices.yml`
- **Zeitplan:** Alle 8 Stunden (0:00, 8:00, 16:00 UTC)
- **Was passiert:**
  - Preise von der GW2 API abrufen
  - In die Datenbank (`price_history.db`) speichern
  - JSON-Datei aktualisieren (`price_history.json`)
  - Automatisch ins Repository commiten

**Aktivierung:**
Der Workflow läuft automatisch nach dem Push. Sie können ihn auch manuell starten:
- Gehen Sie zu **Actions** → **Update GW2 Prices Every 8 Hours** → **Run Workflow**

---

## 2️⃣ **KI-Integration für Kaufentscheidungen**

Die App nutzt **OpenAI GPT-3.5-Turbo** zur intelligenten Analyse von Preishistorien.

### Voraussetzungen:
- OpenAI-Account (https://platform.openai.com)
- API-Key erstellt

### Setup:

#### **Option A: Lokal (für Tests)**
```bash
export OPENAI_API_KEY="sk-...dein-api-key..."
streamlit run App.py
```

#### **Option B: GitHub Secrets (für Production)**
1. Gehen Sie zu: **Settings** → **Secrets and variables** → **Actions**
2. Klicken Sie auf **New repository secret**
3. Name: `OPENAI_API_KEY`
4. Value: Ihr OpenAI API-Key (z.B. `sk-...`)
5. Speichern

> ⚠️ **Wichtig:** Der API-Key wird NICHT in der Workflow-Datei angezeigt!

#### **Option C: Streamlit Secrets (für Streamlit Cloud)**
1. Klicken Sie auf **⋯** in der Streamlit App
2. **Settings** → **Secrets**
3. Fügen Sie ein:
```
OPENAI_API_KEY = "sk-...dein-api-key..."
```

---

## 3️⃣ **KI-Features in der App**

Nach dem Setup sehen Sie im Tab **📊 Historie**:

### Neue Features:
- 📊 **Erweiterte Statistiken** (Min, Max, Durchschnitt)
- 🤖 **KI-Kaufentscheidung**: Die KI analysiert:
  - Aktuelle Preise
  - 30-Tage-Durchschnitt
  - Preis-Trends
  - Volatilität
- 💡 **Begründung**: Erklärung der Entscheidung
- 🎯 **Vertrauen-Level**: Wie sicher die KI ist

### Beispiel-Ausgabe:
```
🟢 Kaufempfehlung
Begründung: Preis 39 Silber unter Durchschnitt (50 Silber). Guter Kaufzeitpunkt.
Vertrauen: Hoch (KI: GPT-3.5)
```

---

## 4️⃣ **Fallback ohne API-Key**

Falls kein OpenAI-API-Key vorhanden:
- Die App funktioniert trotzdem! ✅
- Fallback auf einfache Heuristiken
- Kaufentscheidungen basierend auf Durchschnitten

---

## 5️⃣ **Datenbank-Verwaltung**

### Datenbank-Struktur:
```sql
CREATE TABLE prices (
    item_id INTEGER,
    timestamp TEXT,
    sell INTEGER,
    buy INTEGER
)
```

### Größe:
- Alte Einträge werden automatisch gelöscht
- Maximales Alter: **120 Tage**
- Pro Item: **max. 100 Einträge** in der JSON-Datei

### Manuell auslösen:
```bash
python update_prices.py
```

---

## 6️⃣ **Monitoring & Debugging**

### Logs checken:
1. Gehen Sie zu **Actions** im Repository
2. Klicken Sie auf die letzte Workflow-Ausführung
3. Sehen Sie:
   - ✅ Status
   - 📊 Anzahl geladeener Items
   - 📝 Fehler/Warnungen

### Fehlerbehebung:

| Problem | Lösung |
|---------|--------|
| **"API Fehler: Status 206"** | ✅ Behoben! Der Code akzeptiert jetzt 200 & 206. |
| **"Keine Daten in Datenbank"** | Warten Sie auf die nächste Workflow-Ausführung oder führen Sie `update_prices.py` manuell aus. |
| **"KI gibt Fehler zurück"** | Prüfen Sie, ob `OPENAI_API_KEY` gesetzt ist. |
| **"Action timeout"** | Erhöhen Sie das Timeout in `update_prices.yml` |

---

## 7️⃣ **Kosten**

### OpenAI API:
- **Kostenlos bis $5** im Trial-Modus
- Nach dem Trial: ~$0.001 pro Anfrage (GPT-3.5)
- ~120 Anfragen pro Stunde (für alle Materialien)
- **Monatlich ca. $5-15** bei regelmäßiger Nutzung

### GitHub Actions:
- **Kostenlos** für öffentliche Repos
- **500 Minuten/Monat** für private Repos (free tier)

---

## 8️⃣ **Beispiel-Workflow**

```
⏰ 08:00 UTC
  ↓
📡 GitHub Action startet
  ↓
🔄 update_prices.py wird ausgeführt
  ↓
💾 Preise in DB gespeichert
  ↓
📊 JSON aktualisiert
  ↓
✅ Auto-Commit ins Repo
  ↓
👁️ Sie öffnen die Streamlit App
  ↓
🤖 KI analysiert Preishistorien
  ↓
💡 Kaufempfehlungen angezeigt
```

---

## ❓ FAQ

**F: Kann ich den Zeitplan ändern?**
A: Ja, bearbeiten Sie `.github/workflows/update_prices.yml` Zeile mit `cron:`

**F: Wie oft kann ich die KI befragen?**
A: Streamlit cached die Ergebnisse 10 Minuten lang - keine zusätzlichen Kosten bei Refresh!

**F: Funktioniert es ohne OpenAI?**
A: Ja! Der Fallback nutzt einfache Heuristiken. KI ist optional.

**F: Kann ich andere KI-Modelle nutzen?**
A: Ja! Modifizieren Sie `get_ai_assessment()` um z.B. Claude/Gemini zu nutzen.

---

**✨ Viel Erfolg beim Optimieren Ihrer GW2-Wirtschaft!** 🚀
