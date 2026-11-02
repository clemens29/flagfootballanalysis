# Flag Football GPS-Analyse – Technische Dokumentation

## Übersicht

Die Pipeline besteht aktuell aus zwei Modulen. Die Spielzug-Segmentierung und ein Dashboard sind geplant.

| Modul | Datei | Zweck |
|---|---|---|
| Datenlader | `src/loader.py` | FIT-Datei einlesen, GPS konvertieren, Speed bereinigen und glätten, persönliche v_max bestimmen |
| Sprint-Erkennung | `src/sprint_detector.py` | Geschwindigkeitszonen, Sprint-Detektion, Kennzahlen, Visualisierung, Season-Trend |
| Spielzug-Segmentierung | `src/play_segment.py` | *geplant* |

---

## 1. Datenladen & GPS-Konvertierung (`loader.py`)

### FIT-Format

Garmin-Geräte speichern Trainingsdaten im `.fit`-Binärformat. Die Bibliothek `fitparse` liest die enthaltenen `record`-Messages aus. Bei der Einstellung „Jede Sekunde“ entspricht jeder Record **einer Sekunde** (1 Hz). Bei „Smart Recording“ oder pausierter Uhr sind die Abstände unregelmäßig, deshalb prüft die Pipeline das Abtastintervall (siehe unten) und rechnet überall mit der echten Sample-Dauer.

### GPS: Semicircles → Grad

Garmin speichert Koordinaten nicht als Dezimalgrad, sondern als **Semicircles** (32-Bit-Integer). Umrechnung:

```
lat_deg = position_lat  × (180 / 2³¹)
lon_deg = position_long × (180 / 2³¹)
```

Warum `2³¹`? Ein vollständiger Kreis entspricht `2³²` Semicircles, ein Halbkreis (180°) also `2³¹`.

Punkte, bei denen Breiten- und Längengrad vorhanden sind, erhalten `gps_valid = True`.

### Abtastintervall & Aufzeichnungslücken

Nach dem Sortieren werden doppelte Zeitstempel entfernt und der Abstand zum Vorpunkt bestimmt:

```
Δt[t] = timestamp[t] − timestamp[t−1]
```

Ist `Δt > MAX_GAP_S` (Standard 2 s), gilt das als **Aufzeichnungslücke** (z.B. Uhr pausiert). Jeder lückenlose Abschnitt erhält eine `segment_id`:

```
segment_id[t] = Anzahl der Lücken bis einschließlich t
```

Daraus wird die **effektive Sample-Dauer** `dt_s` berechnet: der echte Abstand Δt, außer am ersten Punkt und direkt nach einer Lücke. Dort wird das typische Intervall (Median von Δt) eingesetzt, damit die Pause nicht als Bewegungszeit zählt.

Bei Lücken oder einem typischen Intervall ≠ 1 s gibt `load_fit()` eine Warnung aus.

**Stand der Daten (14 Sessions):** 13 Sessions sind lückenlos mit exakt 1 Hz aufgezeichnet. `funBowl/26-10-17-1` enthält zwei Pausen (344 s und 47 s).

### Speed-Datenquelle

Garmin liefert zwei Speed-Felder. Die Pipeline priorisiert:

1. `enhanced_speed` (höhere Präzision, m/s)
2. `speed` als Fallback (ältere Geräte)

Fehlende Werte werden mit `0.0` aufgefüllt.

### Ausreißerbereinigung

Physikalisch unrealistische Werte (z.B. durch kurzen GPS-Verlust) werden entfernt:

```
MAX_SPEED_MS = 12.0  m/s  ≈  43 km/h
```

Werte oberhalb dieser Schwelle werden als `NaN` markiert und danach **linear interpoliert**, sodass keine Datenlücken entstehen. Interpoliert wird nur innerhalb eines Abschnitts (`segment_id`), nicht über Aufzeichnungslücken hinweg.

### Trimmen

Über `trim_start_min` und `trim_end_min` lassen sich Aufwärmen bzw. Auslaufen am Anfang und Ende einer Session abschneiden.

### Ausgabe

`load_fit()` liefert einen DataFrame mit den Spalten
`timestamp, dt_s, segment_id, lat, lon, gps_valid, speed_raw, speed_smooth, heart_rate, distance, source_file`.

`load_multiple()` lädt alle `.fit`-Dateien eines Ordners (oder eine Liste von Pfaden) und hängt sie zu einem DataFrame zusammen; die Sessions sind über `source_file` unterscheidbar.

---

## 2. Geschwindigkeitsglättung (Rolling Mean)

### Problem

GPS-Speed-Daten sind verrauscht. Ein einzelner Ausreißer-Sample erzeugt einen unrealistischen Peak, der sonst als Sprint gewertet würde.

### Lösung: Zentrierter gleitender Mittelwert

```
speed_smooth[t] = (1/w) × Σ speed_raw[t+k]   für k = -(w-1)/2 … +(w-1)/2
```

mit Fenstergröße `w = SMOOTH_WINDOW = 3` (in `loader.py`).

**Zentriert** (`center=True`): Das Fenster liegt symmetrisch um den aktuellen Punkt, d.h. es wird auch der folgende Wert einbezogen. Für die Offline-Analyse ist das sinnvoll und vermeidet einen zeitlichen Versatz.

**Ränder** (`min_periods=1`): Am Anfang und Ende wird über die vorhandenen Werte gemittelt, es entstehen keine Lücken.

**Je Abschnitt:** Geglättet wird getrennt pro `segment_id`, sodass Werte vor und nach einer Aufzeichnungspause nicht miteinander vermischt werden.

Beispiel mit w = 3:

| t | speed_raw | speed_smooth |
|---|---|---|
| 0 | 2.1 | (2.1+2.4)/2 = 2.25 |
| 1 | 2.4 | (2.1+2.4+5.8)/3 = 3.43 |
| 2 | 5.8 | (2.4+5.8+2.0)/3 = 3.40 |
| 3 | 2.0 | (5.8+2.0)/2 = 3.90 |

Der Ausreißer bei t = 2 (5.8 m/s) wird auf 3.40 m/s gedämpft.

---

## 3. Persönliche Maximalgeschwindigkeit (`personal_max_speed`)

Alle Schwellen werden **relativ zur individuellen Maximalgeschwindigkeit** gesetzt und nicht absolut (z.B. „18 km/h = Sprint“), da diese zwischen Sportlern und Sportarten stark variiert [7].

`v_max` wird über alle geladenen Sessions als **99,99. Perzentil** von `speed_raw` bestimmt:

```
v_max = quantile(speed_raw, 0.9999)
```

Ausgegeben wird zusätzlich das absolute Maximum zum Vergleich.

Beim Aufruf über die Kommandozeile wird `v_max` über alle Dateien des übergebenen Ordners bzw. die übergebene Einzeldatei berechnet.

---

## 4. Sprint-Erkennung (`sprint_detector.py`)

### 4.1 Geschwindigkeitszonen

Jeder Datenpunkt wird anhand von `speed_smooth` einer Zone relativ zu `v_max` zugeordnet (`ZONE_BOUNDARIES_PCT`):

| Zone | Bereich (Anteil von v_max) |
|---|---|
| walking | 0 – 30 % |
| jogging | 30 – 50 % |
| running | 50 – 70 % |
| high_speed | 70 – 85 % |
| sprint | ≥ 85 % |

Werte ≥ v_max werden ebenfalls als `sprint` eingestuft; fehlende Werte als `unknown`.

### 4.2 Sprint-Schwelle

Unabhängig von den Zonen wird eine eigene Sprint-Schwelle für die Detektion verwendet:

```
threshold = SPRINT_ZONE_PCT × v_max      (SPRINT_ZONE_PCT = 0.6)

is_sprint[t] = speed_smooth[t] ≥ threshold
```

Die Schwelle ist für Flag Football bewusst niedriger als im Fußball angesetzt, da die Spielzüge kurz sind und selten die volle Maximalgeschwindigkeit erreicht wird.

> **Hinweis:** Die Sprint-Detektion (≥ 60 %) und die Zone `sprint` (≥ 85 %) sind zwei verschiedene Definitionen. Detektierte Sprints können daher auch in den Zonen `running` und `high_speed` liegen.

### 4.3 Mindestdauer-Filter

Zusammenhängende Sprint-Phasen werden über einen Zustandswechsel-Zähler gefunden:

```python
changed  = (df["is_sprint"]  != df["is_sprint"].shift()) | \
           (df["segment_id"] != df["segment_id"].shift())
block_id = changed.cumsum()
```

`shift()` vergleicht jeden Wert mit dem Vorgänger. Jeder Wechsel (`False→True` oder `True→False`) **und jeder Beginn eines neuen Aufzeichnungsabschnitts** erhöht den Zähler um 1. Ein Sprint kann also nie über eine Pause hinweg laufen.

Sprint-Blöcke, deren Dauer `Σ dt_s` kürzer als `MIN_SPRINT_DURATION_S = 2` s ist, werden verworfen. Das entfernt kurze Peaks durch GPS-Rauschen [8].

### 4.4 Sprint-IDs

Nach dem Filter werden die verbleibenden Sprint-Blöcke fortlaufend nummeriert:

- `sprint_id = 1, 2, 3, …` für Sprint-Phasen
- `sprint_id = 0` für alle Nicht-Sprint-Punkte

Diese IDs sind die Grundlage für die geplante Spielzug-Segmentierung.

---

## 5. Kennzahlen

### 5.1 Pro Sprint (`sprint_summary`)

| Kennzahl | Berechnung |
|---|---|
| `duration_s` | Σ `dt_s` |
| `speed_max_ms` / `speed_max_kmh` | Maximum von `speed_smooth` |
| `speed_mean_ms` | Mittelwert von `speed_smooth` |
| `distance_m` | Σ `speed_smooth` × `dt_s` |
| `accel_peak` | Maximum von Δ`speed_smooth` / `dt_s` (m/s²) |
| `time_start` / `time_end` | erster bzw. letzter Zeitstempel |

### 5.2 Zonenverteilung (`zone_distribution`)

Zeit (Σ `dt_s`) und prozentualer Zeitanteil je Geschwindigkeitszone innerhalb einer Session.

### 5.3 Season-Trend (`session_trend_row`, `season_trend_summary`)

Jede Session wird zu einer Zeile zusammengefasst: Datum, Dauer (aufgezeichnete Zeit Σ `dt_s`, ohne Pausen), Anzahl Sprints, Sprint-Zeit, Top-Speed, Sprint-Distanz gesamt, Ø und max. Herzfrequenz. `season_trend_summary` sortiert diese Zeilen nach Datum. Ein Trend wird erst ab zwei Sessions berechnet.

---

## 6. Visualisierung

### 6.1 Speed-Verlauf (`plot_sprints`)

Zwei Panels mit gemeinsamer Zeitachse:

- **oben:** `speed_raw` (hell) und `speed_smooth` (dunkel), Sprint-Phasen rot hinterlegt
- **unten:** Geschwindigkeitszonen als farbiger Hintergrund, Sprint-Phasen markiert, echte Herzfrequenz in bpm auf zweiter Y-Achse

### 6.2 Interaktive Sprint-Karte (`plot_sprint_gps`)

Erzeugt mit `folium` eine HTML-Karte:

- gesamte Route als dünne graue Linie
- die **Top-N Sprints** (Standard 10, nach `speed_max_ms`) segmentweise farbcodiert
- grüner Marker am Sprint-Start (Popup mit v_max, Dauer, Distanz), roter Marker am Ende

**Speed-Farbkodierung:** Innerhalb jedes Sprints wird die Geschwindigkeit auf [0, 1] normiert:

```
norm_speed[j] = (speed[j] - min(speed)) / (max(speed) - min(speed) + ε)
```

`ε = 1e-9` verhindert eine Division durch Null bei konstanter Geschwindigkeit. Der Wert wird auf die `plasma`-Colormap abgebildet (violett = langsam, gelb = schnell). Die Farben sind damit **relativ innerhalb eines Sprints** und nicht zwischen Sprints vergleichbar.

**Kartenhintergrund:** OpenStreetMap-Kacheln werden bei lokal geöffneten HTML-Dateien (`file://`) blockiert, da kein Referer mitgeschickt wird. Als Hintergrund dienen daher **Esri-Satellitenbilder**, für die ein **ArcGIS-API-Key** nötig ist. Dieser wird aus der Umgebungsvariable `ARCGIS_API_KEY` gelesen. Ohne Key wird die Karte ohne Hintergrundbild erzeugt.

Die Karten werden unter `output/maps/sprint_map_<session>.html` gespeichert und automatisch im Browser geöffnet.

**Fallback:** Ist `folium` nicht installiert, wird ein einfacher matplotlib-Plot (Längengrad/Breitengrad mit `axis("equal")`) mit denselben farbcodierten Sprint-Routen erzeugt.

### 6.3 Season-Trend (`plot_season_trend`)

Vier Panels mit jeweils eigener Achse: Top-Speed (km/h), Anzahl Sprints, Sprint-Distanz gesamt (m) sowie Ø und max. Herzfrequenz (bpm). Der jeweils höchste Wert wird beschriftet.

---

## 7. Parametertabelle

| Parameter | Datei | Standardwert | Beschreibung |
|---|---|---|---|
| `SMOOTH_WINDOW` | `loader.py` | 3 | Glättungsfenster in Samples (≙ s bei 1 Hz) |
| `MAX_SPEED_MS` | `loader.py` | 12.0 m/s | Obergrenze für gültige Speed-Werte |
| `MAX_GAP_S` | `loader.py` | 2.0 s | Abstand, ab dem eine Aufzeichnungslücke vorliegt |
| `trim_start_min` | `loader.py` | 0.0 | Minuten am Anfang abschneiden |
| `trim_end_min` | `loader.py` | 0.0 | Minuten am Ende abschneiden |
| `ZONE_BOUNDARIES_PCT` | `sprint_detector.py` | 30/50/70/85 % | Zonengrenzen relativ zu v_max |
| `SPRINT_ZONE_PCT` | `sprint_detector.py` | 0.6 | Sprint-Schwelle relativ zu v_max |
| `MIN_SPRINT_DURATION_S` | `sprint_detector.py` | 2 | Mindestdauer eines Sprints in Sekunden |
| `top_n` | `plot_sprint_gps` | 10 | Anzahl hervorgehobener Sprints auf der Karte |
| `ARCGIS_API_KEY` | Umgebungsvariable | – | API-Key für den Kartenhintergrund |

---

## 8. Typischer Ablauf

```
FIT-Datei(en)
   ↓
load_multiple()        → alle Sessions laden
   ↓
personal_max_speed()   → v_max = 99,99. Perzentil von speed_raw
   ↓
load_fit()             → DataFrame je Session (timestamp, dt_s, segment_id, lat, lon, speed_raw, speed_smooth, heart_rate, …)
   ↓
detect_sprints()       → speed_zone, is_sprint (≥ 60 % v_max, ≥ 2 s), sprint_id
   ↓
sprint_summary()       → Kennzahlen pro Sprint
zone_distribution()    → Zeitanteile je Zone
   ↓
plot_sprints()         → Speed-Verlauf + Zonen + Herzfrequenz
plot_sprint_gps()      → output/maps/sprint_map_<session>.html
   ↓
season_trend_summary() → Trend über mehrere Sessions
plot_season_trend()
```

---

## Quellen

- [2] Yurko et al. (2025) – Fractional Tackles: leveraging player tracking data for within-play tackling evaluation. https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11739690/
- [7] Science for Sport – GPS Metrics & Application. https://www.scienceforsport.com/gps-wearables-metrics-and-application/
- [8] Science for Sport – GPS Validity & Reliability. https://www.scienceforsport.com/gps-wearables-validity-and-reliability/
- [9] Kim & Kim (2018) – On-Line Detection and Segmentation of Sports Motions Using a Wearable Sensor. Sensors, 18(3), 913. https://doi.org/10.3390/s18030913
