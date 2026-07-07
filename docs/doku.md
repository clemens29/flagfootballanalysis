# Flag Football GPS-Analyse – Technische Dokumentation

## Übersicht

Die Pipeline besteht aus drei aufeinander aufbauenden Modulen:

| Modul | Datei | Zweck |
|---|---|---|
| Datenlader | `loader.py` | FIT-Datei einlesen, GPS konvertieren, Speed säubern |
| Sprint-Erkennung | `sprint_detection.py` | Hochgeschwindigkeitsphasen isolieren |

---

## 1. Datenladen & GPS-Konvertierung (`loader.py`)

### FIT-Format

Garmin-Geräte speichern Trainingsdaten im `.fit`-Binärformat. Die Bibliothek `fitparse` liest die enthaltenen `record`-Messages aus – jeder Record entspricht in der Regel **einer Sekunde** (1 Hz Abtastrate).

### GPS: Semicircles → Grad

Garmin speichert Koordinaten nicht als Dezimalgrad, sondern als **Semicircles** (32-Bit-Integer). Umrechnung:

```
lat_deg = position_lat  × (180 / 2³¹)
lon_deg = position_long × (180 / 2³¹)
```

Warum `2³¹`? Ein vollständiger Kreis entspricht `2³²` Semicircles, ein Halbkreis (180°) also `2³¹`.

### Speed-Datenquelle

Garmin liefert zwei Speed-Felder. Die Pipeline priorisiert:

1. `enhanced_speed` (höhere Präzision, m/s)
2. `speed` als Fallback (ältere Geräte)

### Ausreißerbereinigung

Physikalisch unrealistische Sprünge (z.B. durch kurzen GPS-Verlust) werden entfernt:

```
MAX_SPEED_MS = 12.0  m/s  ≈  43 km/h
```

Werte oberhalb dieser Schwelle werden als `NaN` markiert und danach **linear interpoliert**, sodass keine Datenlücken entstehen.

---

## 2. Geschwindigkeitsglättung (Rolling Mean)

### Problem

GPS-Speed-Daten sind verrauscht. Ein einzelner Ausreißer-Sample (z.B. kurzer Satellitenverlust) erzeugt einen unrealistischen Peak, der sonst als Sprint gewertet würde.

### Lösung: Gleitender Mittelwert (Rolling Mean)

```
speed_smooth[t] = (1/w) × Σ speed_raw[t-k]   für k = -(w-1)/2 … +(w-1)/2
```

mit Fenstergröße `w` (Standard: `SMOOTH_WINDOW = 3`, in `sprint_detection.py` auch `window=5` genutzt).

**Zentriert** (`center=True`): Das Fenster liegt symmetrisch um den aktuellen Punkt, d.h. es werden auch zukünftige Werte einbezogen. Das ist für die Offline-Analyse sinnvoll (kein Echtzeitbedarf) und liefert eine gleichmäßigere Kurve ohne zeitlichen Versatz.

Beispiel mit w=3:

| t | speed_raw | speed_smooth |
|---|---|---|
| 0 | 2.1 | – |
| 1 | 2.4 | (2.1+2.4+5.8)/3 = 3.43 |
| 2 | 5.8 | (2.4+5.8+2.0)/3 = 3.40 |
| 3 | 2.0 | – |

Ohne Glättung wäre t=2 ein Ausreißer-Sprint; mit Glättung bleibt der Wert im normalen Bereich.

---

## 3. Sprint-Erkennung (`sprint_detection.py`)

### Schwellwert-Definition

Die Sprint-Schwelle wird **relativ zur maximalen Geschwindigkeit** berechnet, nicht absolut. Damit passt sich das Modell automatisch an unterschiedliche Spieler und Spieltage an:

```
SPRINT_THRESHOLD = max(speed_smooth) * 0.6
```

Ein Sample gilt als Sprint, wenn:

```
speed_smooth[t] > SPRINT_THRESHOLD   →   is_sprint[t] = True
```

### Sprint-Segmentierung

Aufeinanderfolgende `True`-Werte werden zu einem **Sprint-Segment** zusammengefasst:

```python
df["sprint_id"] = (df["is_sprint"] != df["is_sprint"].shift()).cumsum()
```

`shift()` vergleicht jeden Wert mit dem Vorgänger. Jeder Wechsel (`False→True` oder `True→False`) erhöht den Zähler um 1. Damit bekommt jedes zusammenhängende Segment eine eindeutige ID.

---

## 5. GPS-Visualisierung

### Koordinatensystem

Alle Plots nutzen direkt Längen-/Breitengrad als x/y-Achse. Mit `plt.axis("equal")` werden beide Achsen gleich skaliert, sodass Abstände visuell korrekt erscheinen (sonst wirken Nord-Süd-Bewegungen gestaucht).

### Speed-Farbkodierung (Heatmap pro Play)

Für jeden Play wird die Geschwindigkeit **normalisiert** auf [0, 1]:

```
norm_speed[j] = (speed[j] - min(speed)) / (max(speed) - min(speed) + ε)
```

Das `ε = 1e-9` verhindert Division durch Null wenn alle Werte gleich sind. Der Wert wird dann als Farbe im `plasma`-Colormap-Spektrum genutzt (violett = langsam, gelb = schnell).

### Richtungsvektoren (Quiver-Plot)

Die lokale Bewegungsrichtung wird aus aufeinanderfolgenden GPS-Punkten berechnet:

```
dlon[t] = lon[t] - lon[t-1]
dlat[t] = lat[t] - lat[t-1]
```

Diese Differenzen sind proportional zum tatsächlichen Verschiebungsvektor auf der Erdoberfläche (für kleine Abstände, wie auf einem Spielfeld, ist die Verzerrung vernachlässigbar). `plt.quiver()` zeichnet diese Vektoren als Pfeile an jedem GPS-Punkt.

---

## 6. Parametertabelle

| Parameter | Datei | Standardwert | Beschreibung |
|---|---|---|---|
| `SMOOTH_WINDOW` | `loader.py` | 3 | Glättungsfenster in Sekunden |
| `MAX_SPEED_MS` | `loader.py` | 12.0 m/s | Obergrenze für gültige Speed-Werte |
| `SPRINT_THRESHOLD` | `sprint_detection.py` | max * 0.6 | Sprint-Schwelle |
| `trim_start_min` | `loader.py` | 0.0 | Aufwärmzeit vom Anfang abschneiden |
| `trim_end_min` | `loader.py` | 0.0 | Auslaufen am Ende abschneiden |

---

## 7. Typischer Ablauf

```
FIT-Datei
   ↓
load_fit()          → DataFrame mit timestamp, lat, lon, speed_raw, speed_smooth, heart_rate
   ↓
Schwelle berechnen  → mean + k × std
   ↓
Binär-Maske         → is_sprint / active  (True/False pro Sample)
   ↓
Statistiken         → duration, max_speed, mean_speed pro Segment
   ↓
Visualisierung      → GPS-Karte, Speed-Plot, Quiver
```