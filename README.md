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

## Dateien

| Datei | Beschreibung |
|---|---|
| `loader.py` | Lädt einzelne `.fit`-Dateien oder ganze Ordner, wandelt GPS-Semicircles in Grad um, bereinigt unrealistische Geschwindigkeitswerte, glättet den Speed-Verlauf (Rolling Mean) |
| `sprint_detector.py` | Erkennt Sprint-Phasen anhand eines individuellen Schwellwerts (Anteil der persönlichen Maximalgeschwindigkeit), segmentiert zusammenhängende Sprints, liefert Statistiken, Plots, eine interaktive GPS-Karte und einen Session-übergreifenden Trend |
| `doku.md` | Ausführliche technische Dokumentation der Methodik (Formeln, Parameter, Beispiele) |
| `doku.pdf` | Mathematische Herleitungen als LaTeX-Dokument |

## Funktionen im Detail (`sprint_detector.py`)

- **`detect_sprints`** – klassifiziert jeden GPS-Punkt in eine Geschwindigkeitszone (Walking … Sprint) und markiert Sprint-Phasen relativ zu `v_max` (Standard: 60 %), inkl. Mindestdauer-Filter gegen GPS-Rauschen
- **`sprint_summary`** – Kennzahlen pro Sprint (Dauer, v_max, Distanz, Peak-Beschleunigung)
- **`zone_distribution`** – prozentuale Zeitverteilung über die Geschwindigkeitszonen
- **`plot_sprints`** – Speed-Verlauf mit Sprint-Phasen und echter Herzfrequenz (zweite Y-Achse)
- **`plot_sprint_gps`** – interaktive Karte (Folium/OpenStreetMap) mit farbcodierten Sprint-Routen; speichert eine `.html`-Datei und öffnet sie automatisch im Browser. Ist `folium` nicht installiert, greift automatisch ein einfacher matplotlib-Fallback-Plot
- **`session_trend_row` / `season_trend_summary` / `plot_season_trend`** – fasst mehrere Sessions zu einer Trend-Übersicht zusammen (Top-Speed, Sprintanzahl, Sprint-Distanz, Herzfrequenz über die Zeit)

## Installation

```bash
git clone <repo-url>
cd <repo-name>
pip install -r requirements.txt
```

Benötigt Python ≥ 3.9.

## Verwendung

```python
from loader import load_multiple, load_fit, personal_max_speed
from sprint_detector import detect_sprints, sprint_summary, zone_distribution, plot_sprints

# Alle Sessions laden, um die persönliche Maximalgeschwindigkeit zu bestimmen
df_all = load_multiple("data/")
v_max = personal_max_speed(df_all)

# Einzelne Session analysieren
df = load_fit("data/260512.fit")
df = detect_sprints(df, v_max=v_max)

print(zone_distribution(df))
print(sprint_summary(df))

plot_sprints(df)
```

Direkt über die Kommandozeile (Einzelfile oder ganzer Ordner):

```bash
python sprint_detector.py data/260512.fit
python sprint_detector.py data/
```

Bei einem Ordner mit mehreren Sessions wird zusätzlich automatisch ein Season-Trend über alle Trainings berechnet und geplottet (Top-Speed, Sprintanzahl, Sprint-Distanz, Herzfrequenz je Session). Für jede Session wird außerdem eine interaktive Karte als `sprint_map_<sessionname>.html` gespeichert und im Browser geöffnet.

## Wissenschaftliche Grundlagen

Die Schwellwertlogik orientiert sich an etablierten GPS-Metriken aus Profisport und Fußball (individuelle statt absolute Geschwindigkeitszonen, Mindestdauer-Filter gegen GPS-Rauschen). Details und Quellenangaben siehe `doku.md` und `doku.pdf`.

## Status / Limitationen

- Aktuelle Sprint-Schwelle (60 % von v_max, min. 2 s Dauer)
- 1-Hz-Abtastrate ist für sehr kurze Spielzüge (3–5 s) grenzwertig
- Interaktive Karte benötigt eine Internetverbindung für die OpenStreetMap-Kacheln
- Play-Segmentierung und Routen-Klassifikation sind für das nächste Semester geplant