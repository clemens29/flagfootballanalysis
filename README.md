# Flag Football GPS-Analyse

GPS-basierte Spielzug- und Sprintanalyse im Amateur-Flag-Football auf Basis von Consumer-GPS-Daten (Garmin `.fit`-Dateien).

Forschungsprojekt I · HTW Dresden · Trainingswissenschaft & Sportinformatik · Sommersemester 2026

## Motivation

Für Flag Football existiert bislang keine GPS-Tracking-Studie und kein bezahlbares Analyse-Tool für Amateur-Vereine. Diese Pipeline prüft, ob sich aus 1-Hz-Consumer-GPS-Daten (z.B. einer Garmin-Uhr) automatisch Sprints und Spielzüge detektieren lassen, die für Coaching-Feedback nutzbar sind.

## Pipeline

```
Garmin FIT-File (1 Hz GPS)
        │
        ▼
   loader.py            Einlesen, GPS-Konvertierung, Ausreißerbereinigung, Glättung
        │
        ▼
sprint_detector.py       Sprint-Detektion relativ zur individuellen Maximalgeschwindigkeit
        │
        ▼
  (play_segment.py)      State-Change-Segmentierung von Spielzügen  [geplant]
        │
        ▼
     Dashboard           Coaching-Feedback  [geplant]
```

## Projektstruktur

```
data/
  unikurs/            Uni-Kurs-Sessions (26-04-21 … 26-06-30)
  trainingMonarchs/   Vereinstraining (26-10-15, 26-10-22, 26-10-29)
  funBowl/            Turnier-Spiele (26-10-17-1 … -3)
src/
  loader.py
  sprint_detector.py
docs/
  doku.md, doku.tex, doku.pdf
output/
  maps/               generierte Sprint-Karten (sprint_map_<session>.html)
```

`data/` und `output/` sind per `.gitignore` von der Versionskontrolle ausgeschlossen.

## Dateien

| Datei | Beschreibung |
|---|---|
| `src/loader.py` | Lädt einzelne `.fit`-Dateien oder ganze Ordner, wandelt GPS-Semicircles in Grad um, bereinigt unrealistische Geschwindigkeitswerte, glättet den Speed-Verlauf (Rolling Mean) |
| `src/sprint_detector.py` | Erkennt Sprint-Phasen anhand eines individuellen Schwellwerts (Anteil der persönlichen Maximalgeschwindigkeit), segmentiert zusammenhängende Sprints, liefert Statistiken, Plots, eine interaktive GPS-Karte und einen Session-übergreifenden Trend |
| `docs/doku.md` | Ausführliche technische Dokumentation der Methodik (Formeln, Parameter, Beispiele) |
| `docs/doku.pdf` | Mathematische Herleitungen als LaTeX-Dokument |

## Funktionen im Detail (`sprint_detector.py`)

- **`detect_sprints`** – klassifiziert jeden GPS-Punkt in eine Geschwindigkeitszone (Walking … Sprint) und markiert Sprint-Phasen relativ zu `v_max` (Standard: 60 %), inkl. Mindestdauer-Filter gegen GPS-Rauschen
- **`sprint_summary`** – Kennzahlen pro Sprint (Dauer, v_max, Distanz, Peak-Beschleunigung)
- **`zone_distribution`** – prozentuale Zeitverteilung über die Geschwindigkeitszonen
- **`plot_sprints`** – Speed-Verlauf mit Sprint-Phasen und echter Herzfrequenz (zweite Y-Achse)
- **`plot_sprint_gps`** – interaktive Karte (Folium, Esri-Satellitenbild) mit farbcodierten Sprint-Routen; speichert eine `.html`-Datei und öffnet sie automatisch im Browser. Für das Hintergrundbild wird ein ArcGIS-API-Key benötigt (siehe unten). Ist `folium` nicht installiert, greift automatisch ein einfacher matplotlib-Fallback-Plot
- **`session_trend_row` / `season_trend_summary` / `plot_season_trend`** – fasst mehrere Sessions zu einer Trend-Übersicht zusammen (Top-Speed, Sprintanzahl, Sprint-Distanz, Herzfrequenz über die Zeit)

## Installation

```bash
git clone <repo-url>
cd <repo-name>
pip install -r requirements.txt
```

Benötigt Python ≥ 3.9.

## Verwendung

Aus dem Ordner `src/` heraus (bzw. mit `src/` im Python-Pfad):

```python
from loader import load_multiple, load_fit, personal_max_speed
from sprint_detector import detect_sprints, sprint_summary, zone_distribution, plot_sprints

# Alle Sessions laden, um die persönliche Maximalgeschwindigkeit zu bestimmen
df_all = load_multiple("../data/unikurs/")
v_max = personal_max_speed(df_all)

# Einzelne Session analysieren
df = load_fit("../data/unikurs/26-05-12.fit")
df = detect_sprints(df, v_max=v_max)

print(zone_distribution(df))
print(sprint_summary(df))

plot_sprints(df)
```

Direkt über die Kommandozeile (Einzelfile oder ganzer Ordner):

```bash
python src/sprint_detector.py data/unikurs/26-05-12.fit
python src/sprint_detector.py data/unikurs/
python src/sprint_detector.py data/funBowl/
```

Bei einem Ordner mit mehreren Sessions wird zusätzlich automatisch ein Season-Trend über alle Trainings berechnet und geplottet (Top-Speed, Sprintanzahl, Sprint-Distanz, Herzfrequenz je Session). Für jede Session wird außerdem eine interaktive Karte als `output/maps/sprint_map_<sessionname>.html` gespeichert und im Browser geöffnet.

> Hinweis: `v_max` wird über alle Dateien des übergebenen Ordners bestimmt. Wer Kontexte (Uni-Kurs, Training, Turnier) mit derselben Referenz vergleichen will, sollte `v_max` einmal über alle Ordner berechnen und explizit übergeben.

### Kartenhintergrund (ArcGIS-API-Key)

OpenStreetMap-Kacheln werden bei lokal geöffneten HTML-Dateien (`file://`) blockiert. Die Karte nutzt deshalb Esri-Satellitenbilder, die einen ArcGIS-API-Key benötigen. Der Key wird über die Umgebungsvariable `ARCGIS_API_KEY` gelesen:

```bash
# Windows (PowerShell)
$env:ARCGIS_API_KEY = "dein-key"
# Linux / macOS
export ARCGIS_API_KEY="dein-key"
```

Ohne Key wird die Karte trotzdem erzeugt, aber ohne Hintergrundbild (nur Routen und Marker).

## Wissenschaftliche Grundlagen

Die Schwellwertlogik orientiert sich an etablierten GPS-Metriken aus Profisport und Fußball (individuelle statt absolute Geschwindigkeitszonen, Mindestdauer-Filter gegen GPS-Rauschen). Details und Quellenangaben siehe `doku.md` und `doku.pdf`.

## Status / Limitationen

- Aktuelle Sprint-Schwelle: 60 % von v_max, min. 2 s Dauer
- 1-Hz-Abtastrate ist für sehr kurze Spielzüge (3–5 s) grenzwertig
- Kartenhintergrund benötigt einen ArcGIS-API-Key und eine Internetverbindung
- Play-Segmentierung und Routen-Klassifikation sind für das nächste Semester geplant