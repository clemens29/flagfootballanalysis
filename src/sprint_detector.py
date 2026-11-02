"""
sprint_detector.py
------------------
Wissenschaftlich fundierte Sprint-Detektion aus Consumer-GPS-Daten
für Flag Football im Amateurbereich.

Verwendung:
    from loader import load_fit, load_multiple, personal_max_speed
    from sprint_detector import detect_sprints, sprint_summary, plot_sprints

    df      = load_fit("mein_training.fit")
    df      = detect_sprints(df, v_max=7.0)
    df      = detect_accelerations(df)
    summary = sprint_summary(df)
    antritte = accel_summary(df)

Wissenschaftliche Grundlagen:
    [7]  Science for Sport – GPS Metrics & Application
         https://www.scienceforsport.com/gps-wearables-metrics-and-application/
    [8]  Science for Sport – GPS Validity & Reliability
         https://www.scienceforsport.com/gps-wearables-validity-and-reliability/
    [9]  Kim & Kim (2018) – On-Line Detection and Segmentation of Sports
         Motions Using a Wearable Sensor. Sensors, 18(3), 913.
         https://doi.org/10.3390/s18030913
    [2]  Yurko et al. (2025) – Fractional Tackles: leveraging player
         tracking data for within-play tackling evaluation.
         https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11739690/

Abhängigkeiten für GPS-Karte:
    pip install folium
"""

import os
import warnings
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.ticker as ticker


# ============================================================
# SPRINT-ZONEN DEFINITION
# ============================================================
#
# Wissenschaftliche Grundlage: [7] Science for Sport empfiehlt,
# Geschwindigkeitszonen INDIVIDUELL auf Basis der persönlichen
# Maximalgeschwindigkeit (v_max) zu definieren – nicht mit
# absoluten Werten (z.B. "18 km/h = Sprint"), da diese zwischen
# Sportlern und Sportarten stark variieren.
#
# Im Fußball übliche Zonen liegen bei High Speed ab ~70% und
# Sprint ab ~85% v_max. Für Flag Football (explosive Starts, kurze
# Distanzen, selten volle Maximalgeschwindigkeit) sind die Grenzen
# nach unten verschoben:
#   Walking:     0  – 30% v_max
#   Jogging:    30  – 45% v_max
#   Running:    45  – 60% v_max
#   High Speed: 60  – 80% v_max
#   Sprint:     ≥ 80% v_max
#
# Die Lauf-Detektion (detect_sprints) nutzt GENAU diese Grenzen:
# erkannt wird jede Phase ab High Speed (≥ 60%); erreicht sie die
# Sprint-Zone (≥ 80%), wird sie als "sprint" kategorisiert, sonst
# als "high_speed". Es gibt damit nur noch eine Definition.

ZONE_BOUNDARIES_PCT = {
    "walking":    (0.00, 0.30),
    "jogging":    (0.30, 0.45),
    "running":    (0.45, 0.60),
    "high_speed": (0.60, 0.80),
    "sprint":     (0.80, 1.00),
}

# Abgeleitet aus den Zonen – nicht separat ändern, sondern die Zonen anpassen
SPRINT_ZONE_PCT = ZONE_BOUNDARIES_PCT["high_speed"][0]   # Detektionsschwelle (60%)
SPRINT_PEAK_PCT = ZONE_BOUNDARIES_PCT["sprint"][0]       # Kategorie "sprint" (80%)

MIN_SPRINT_DURATION_S = 2


# ============================================================
# ANTRITT-DEFINITION (BESCHLEUNIGUNG)
# ============================================================
#
# Im Flag Football sind viele Aktionen kurze, explosive Antritte über
# 5–15 m, bei denen die Speed-Schwelle nie erreicht wird. Diese werden
# über die Beschleunigung a = Δv / Δt erkannt.
#
# Übliche Schwellen im Teamsport: > 2 m/s² (moderat), > 3 m/s² (hoch).
# Berechnet wird aus speed_raw (Garmins enhanced_speed ist bereits
# intern gefiltert) – die zusätzliche Glättung würde die Spitzen etwa
# halbieren. Die Werte sind Startwerte und per Video zu validieren.

ACCEL_MODERATE_MS2 = 2.0   # Antritt-Schwelle
ACCEL_HIGH_MS2     = 3.0   # Kategorie "high"


# ============================================================
# HAUPTFUNKTION: SPRINT-DETEKTION
# ============================================================

def detect_sprints(df: pd.DataFrame,
                   v_max: float,
                   sprint_pct: float = SPRINT_ZONE_PCT,
                   min_duration_s: int = MIN_SPRINT_DURATION_S) -> pd.DataFrame:
    """
    Erkennt Sprint-Phasen in einem geladenen Session-DataFrame.

    Methode
    -------
    1. Individueller Schwellwert: threshold = sprint_pct * v_max
       → [7] Science for Sport: individuelle statt absolute Schwellwerte

    2. Binäre Klassifikation pro Datenpunkt (1 Hz)

    3. Mindestdauer-Filter: Bursts < min_duration_s (Summe der echten
       Sample-Dauern dt_s) werden entfernt
       → [8] GPS-Rauschen bei hohen Speeds eliminieren

    4. Sprint-IDs: zusammenhängende Phasen erhalten eine eindeutige ID.
       Ein Sprint endet auch an einer Aufzeichnungslücke (segment_id).
       → Grundlage für spätere Play-Segmentierung

    Parameter
    ----------
    df : pd.DataFrame
        Ausgabe von loader.load_fit()
    v_max : float
        Persönliche Maximalgeschwindigkeit in m/s.
        Berechnen mit loader.personal_max_speed()
    sprint_pct : float
        Anteil von v_max als Detektionsschwelle
        (default: Beginn der Zone high_speed = 0.60)
    min_duration_s : int
        Minimale Sprint-Dauer in Sekunden (default: 2)

    Rückgabe
    --------
    df mit zusätzlichen Spalten:
        speed_zone    – kategorische Geschwindigkeitszone (string)
        is_sprint     – bool: True wenn Sprint-Schwelle überschritten
        sprint_id     – int: eindeutige ID pro Sprint-Phase (0 = kein Sprint)
    """

    df = df.copy()

    # ----------------------------------------------------------
    # 1. Geschwindigkeitszonen
    # ----------------------------------------------------------
    def assign_zone(v):
        if pd.isna(v):
            return "unknown"
        for zone, (lo, hi) in ZONE_BOUNDARIES_PCT.items():
            if lo * v_max <= v < hi * v_max:
                return zone
        if v >= v_max:
            return "sprint"
        return "walking"

    df["speed_zone"] = df["speed_smooth"].apply(assign_zone)

    # ----------------------------------------------------------
    # 2. Sprint-Flag
    # ----------------------------------------------------------
    threshold = sprint_pct * v_max
    df["is_sprint"] = df["speed_smooth"] >= threshold

    # ----------------------------------------------------------
    # 3. Mindestdauer-Filter
    # ----------------------------------------------------------
    def _blocks(d):
        # Neuer Block bei Zustandswechsel ODER Beginn eines neuen
        # Aufzeichnungsabschnitts (Lücke) → kein Sprint über Pausen hinweg
        changed = (d["is_sprint"] != d["is_sprint"].shift()) | \
                  (d["segment_id"] != d["segment_id"].shift())
        return changed.cumsum()

    raw_blocks = _blocks(df)
    sprint_dur = df.loc[df["is_sprint"], "dt_s"].groupby(raw_blocks[df["is_sprint"]]).sum()
    too_short  = sprint_dur[sprint_dur < min_duration_s].index
    df.loc[raw_blocks.isin(too_short) & df["is_sprint"], "is_sprint"] = False

    # ----------------------------------------------------------
    # 4. Sprint-IDs
    # ----------------------------------------------------------
    blocks = _blocks(df)
    df["sprint_id"] = 0
    sprint_blocks = blocks[df["is_sprint"]]
    ids = {b: i + 1 for i, b in enumerate(pd.unique(sprint_blocks))}
    df.loc[df["is_sprint"], "sprint_id"] = sprint_blocks.map(ids).astype(int)

    # ----------------------------------------------------------
    # 5. Logging
    # ----------------------------------------------------------
    n_sprints     = df["sprint_id"].max()
    total_time    = round(df.loc[df["is_sprint"], "dt_s"].sum(), 1)
    pct_time      = round(total_time / df["dt_s"].sum() * 100, 1)
    threshold_kmh = round(threshold * 3.6, 1)

    print(f"  Sprint-Schwelle:    {threshold:.2f} m/s  ({threshold_kmh} km/h)"
          f"  = {int(sprint_pct*100)}% von v_max={v_max:.2f} m/s")
    print(f"  Sprints gefunden:   {n_sprints}")
    print(f"  Sprint-Zeit:        {total_time}s  ({pct_time}% der Session)")

    return df


# ============================================================
# SPRINT-STATISTIKEN
# ============================================================

def sprint_summary(df: pd.DataFrame) -> pd.DataFrame:
    """
    Berechnet Kennzahlen pro Sprint-Phase.

    Alle zeitabhängigen Größen nutzen die echte Sample-Dauer dt_s,
    funktionieren also auch bei Abtastraten != 1 Hz:
        duration_s = Σ dt_s
        distance_m = Σ v · dt_s
        accel      = Δv / Δt   (m/s²)
    """
    sprints = df[df["is_sprint"] & (df["sprint_id"] > 0)]

    rows = []
    for sid, group in sprints.groupby("sprint_id"):
        group = group.copy()
        # Sprints liegen immer innerhalb eines lückenlosen Abschnitts,
        # daher ist dt_s hier der echte Abstand zum Vorpunkt
        accel = (group["speed_smooth"].diff() / group["dt_s"]).fillna(0)

        rows.append({
            "sprint_id":      sid,
            "duration_s":     round(group["dt_s"].sum(), 1),
            "speed_max_ms":   round(group["speed_smooth"].max(), 2),
            "speed_max_kmh":  round(group["speed_smooth"].max() * 3.6, 1),
            "speed_mean_ms":  round(group["speed_smooth"].mean(), 2),
            "distance_m":     round((group["speed_smooth"] * group["dt_s"]).sum(), 1),
            "accel_peak":     round(accel.max(), 2),
            # Kategorie nach erreichter Zone (siehe ZONE_BOUNDARIES_PCT)
            "category":       "sprint" if (group["speed_zone"] == "sprint").any()
                              else "high_speed",
            "time_start":     group["timestamp"].iloc[0],
            "time_end":       group["timestamp"].iloc[-1],
        })

    return pd.DataFrame(rows)


# ============================================================
# ANTRITT-DETEKTION (BESCHLEUNIGUNG)
# ============================================================

def detect_accelerations(df: pd.DataFrame,
                         accel_threshold: float = ACCEL_MODERATE_MS2,
                         verbose: bool = True) -> pd.DataFrame:
    """
    Erkennt Antritte (explosive Beschleunigungen) unabhängig von der
    erreichten Geschwindigkeit.

    Methode
    -------
    1. a[t] = (v_raw[t] − v_raw[t−1]) / dt_s[t]   (m/s²)
       – aus speed_raw, nicht speed_smooth (Spitzen bleiben erhalten)
       – am Beginn jedes Aufzeichnungsabschnitts a = 0 (keine Δv über Lücken)
    2. is_accel[t] = a[t] ≥ accel_threshold
    3. Zusammenhängende Samples (innerhalb eines Abschnitts) bilden
       einen Antritt mit eindeutiger accel_id (0 = kein Antritt)

    Kein Mindestdauer-Filter: bei 1 Hz dauert ein Antritt oft nur 1–2
    Samples. Ausreißer > MAX_SPEED_MS sind bereits im Loader entfernt.

    Rückgabe
    --------
    df mit zusätzlichen Spalten:
        accel     – Beschleunigung in m/s²
        is_accel  – bool
        accel_id  – int, eindeutige ID pro Antritt (0 = kein Antritt)
    """
    df = df.copy()

    new_segment = df["segment_id"] != df["segment_id"].shift()
    accel = df["speed_raw"].diff() / df["dt_s"]
    accel[new_segment] = 0.0
    df["accel"] = accel.fillna(0.0)

    df["is_accel"] = df["accel"] >= accel_threshold

    changed = (df["is_accel"] != df["is_accel"].shift()) | new_segment
    blocks  = changed.cumsum()
    acc_blocks = blocks[df["is_accel"]]
    ids = {b: i + 1 for i, b in enumerate(pd.unique(acc_blocks))}
    df["accel_id"] = 0
    df.loc[df["is_accel"], "accel_id"] = acc_blocks.map(ids).astype(int)

    if verbose:
        summ = accel_summary(df)
        n_high = int((summ["category"] == "high").sum()) if not summ.empty else 0
        print(f"  Antritt-Schwelle:   {accel_threshold:.1f} m/s²"
              f"  (hoch ≥ {ACCEL_HIGH_MS2:.1f} m/s²)")
        print(f"  Antritte gefunden:  {len(summ)}  (davon hoch: {n_high})")

    return df


def accel_summary(df: pd.DataFrame) -> pd.DataFrame:
    """
    Kennzahlen pro Antritt.

    Ein Antritt beginnt am Sample VOR der ersten Überschreitung (dort
    startet die Beschleunigung) und endet am letzten Sample über der
    Schwelle.

        v_start_kmh  – Geschwindigkeit beim Start
        v_end_kmh    – Geschwindigkeit am Ende der Beschleunigung
        delta_v_kmh  – Geschwindigkeitsgewinn
        accel_peak   – maximale Beschleunigung (m/s²)
        accel_mean   – mittlere Beschleunigung (m/s²)
        duration_s   – Dauer der Beschleunigungsphase (Σ dt_s)
        category     – "high" (Peak ≥ ACCEL_HIGH_MS2) oder "moderate"
    """
    cols = ["accel_id", "duration_s", "v_start_kmh", "v_end_kmh",
            "delta_v_kmh", "accel_peak", "accel_mean", "category",
            "time_start", "time_end"]
    if "accel_id" not in df.columns or df["accel_id"].max() == 0:
        return pd.DataFrame(columns=cols)

    rows = []
    for aid, group in df[df["accel_id"] > 0].groupby("accel_id"):
        i_first, i_last = group.index[0], group.index[-1]
        # Startpunkt = Sample davor (innerhalb desselben Abschnitts)
        i_start = i_first - 1 if (i_first - 1 in df.index and
                  df.at[i_first - 1, "segment_id"] == df.at[i_first, "segment_id"]) else i_first
        v_start = df.at[i_start, "speed_raw"]
        v_end   = df.at[i_last, "speed_raw"]
        peak    = group["accel"].max()

        rows.append({
            "accel_id":    aid,
            "duration_s":  round(group["dt_s"].sum(), 1),
            "v_start_kmh": round(v_start * 3.6, 1),
            "v_end_kmh":   round(v_end * 3.6, 1),
            "delta_v_kmh": round((v_end - v_start) * 3.6, 1),
            "accel_peak":  round(peak, 2),
            "accel_mean":  round(group["accel"].mean(), 2),
            "category":    "high" if peak >= ACCEL_HIGH_MS2 else "moderate",
            "time_start":  df.at[i_start, "timestamp"],
            "time_end":    df.at[i_last, "timestamp"],
        })

    return pd.DataFrame(rows, columns=cols)


# ============================================================
# ZONENVERTEILUNG
# ============================================================

def zone_distribution(df: pd.DataFrame) -> pd.DataFrame:
    """
    Gibt die prozentuale Verteilung der Geschwindigkeitszonen zurück.
    """
    zone_order = ["walking", "jogging", "running", "high_speed", "sprint", "unknown"]
    counts = df.groupby("speed_zone")["dt_s"].sum()

    result = pd.DataFrame({
        "zone":    zone_order,
        "seconds": [round(counts.get(z, 0.0), 1) for z in zone_order],
    })
    result["percent"] = (result["seconds"] / result["seconds"].sum() * 100).round(1)
    return result[result["seconds"] > 0].reset_index(drop=True)


# ============================================================
# SEASON-TREND (SESSION-ÜBERGREIFEND)
# ============================================================
#
# Einzelne Sessions sagen wenig über Formentwicklung aus. Erst über
# mehrere Trainingseinheiten hinweg zeigt sich, ob Top-Speed, Sprintzahl
# oder Herzfrequenz-Belastung steigen/sinken – z.B. ein Hinweis auf
# Fitnessfortschritt oder aufkommende Ermüdung über die Saison.

def session_trend_row(df: pd.DataFrame) -> dict:
    """
    Fasst eine einzelne, bereits mit detect_sprints() analysierte Session
    zu einer Zeile für die Season-Trend-Übersicht zusammen.
    """
    stats = sprint_summary(df)
    acc   = accel_summary(df) if "accel_id" in df.columns else pd.DataFrame()

    return {
        "source_file":       df["source_file"].iloc[0],
        "date":              df["timestamp"].iloc[0].date(),
        "duration_min":      round(df["dt_s"].sum() / 60, 1),
        "n_sprints":         int(len(stats)),
        "sprint_time_s":     round(df.loc[df["is_sprint"], "dt_s"].sum(), 1),
        "speed_max_kmh":     round(stats["speed_max_kmh"].max(), 1) if not stats.empty else 0.0,
        "sprint_distance_m": round(stats["distance_m"].sum(), 1) if not stats.empty else 0.0,
        "n_sprints_80":      int((stats["category"] == "sprint").sum()) if not stats.empty else 0,
        "n_accel":           int(len(acc)),
        "n_accel_high":      int((acc["category"] == "high").sum()) if not acc.empty else 0,
        "accel_peak_max":    round(acc["accel_peak"].max(), 2) if not acc.empty else 0.0,
        "hr_mean_bpm":       round(df["heart_rate"].mean(), 1) if df["heart_rate"].notna().any() else None,
        "hr_max_bpm":        int(df["heart_rate"].max()) if df["heart_rate"].notna().any() else None,
    }


def season_trend_summary(rows: list) -> pd.DataFrame:
    """
    Baut aus einer Liste von session_trend_row()-Dicts eine nach Datum
    sortierte Übersichtstabelle über mehrere Trainingseinheiten.
    """
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def plot_season_trend(trend: pd.DataFrame, title: str = "Season-Trend") -> None:
    """
    Visualisiert die Entwicklung zentraler Kennzahlen über mehrere
    Trainingseinheiten hinweg (z.B. Formkurve über die Saison).

    Vier Panels, je eine Kennzahl in eigenem Achsensystem (kein
    Dual-Axis-Mixing unterschiedlicher Einheiten):
        - Top-Speed (km/h)
        - Anzahl Sprints
        - Sprint-Distanz gesamt (m)
        - Herzfrequenz (bpm) – Ø und Max, gleiche Einheit
    """
    if len(trend) < 2:
        print("  Zu wenige Sessions für einen Trend (mind. 2 nötig).")
        return

    # Numerische x-Positionen statt String-Kategorien: robuster für
    # Achsen-Ticks und Punkt-Annotationen als matplotlibs Kategorie-Achse.
    xi = range(len(trend))
    xlabels = trend["date"].astype(str)

    panels = [
        ("speed_max_kmh",     "Top-Speed (km/h)",       "#2196F3"),
        ("n_sprints",         "Anzahl Läufe ≥ 60% / Antritte", "#FB8C00"),
        ("sprint_distance_m", "Sprint-Distanz ges. (m)", "#43A047"),
    ]

    fig, axes = plt.subplots(2, 2, figsize=(13, 8))
    fig.suptitle(title, fontsize=13, fontweight="bold")
    flat_axes = list(axes.flat)

    for ax, (col, label, color) in zip(flat_axes, panels):
        ax.plot(xi, trend[col], color=color, linewidth=2, marker="o", markersize=6)
        ax.set_title(label, fontsize=10)
        ax.set_xticks(list(xi))
        ax.set_xticklabels(xlabels, rotation=45, ha="right")
        ax.grid(True, alpha=0.25)

        # Nur den Höchstwert direkt beschriften, nicht jeden Punkt
        i_max = trend[col].idxmax()
        ax.annotate(f"{trend[col].iloc[i_max]:g}",
                    (i_max, trend[col].iloc[i_max]),
                    textcoords="offset points", xytext=(0, 8),
                    fontsize=8, ha="center", color=color, fontweight="bold")

    # Zweites Panel: Antritte zusätzlich zu den Läufen (gleiche Einheit: Anzahl)
    if "n_accel" in trend.columns:
        ax = flat_axes[1]
        ax.plot(xi, trend["n_accel"], color="#E53935", linewidth=2,
                marker="s", markersize=5, label="Antritte ≥ 2 m/s²")
        ax.plot(xi, trend["n_sprints"], color="#FB8C00", linewidth=0,
                label="Läufe ≥ 60% v_max")
        ax.legend(fontsize=8, loc="upper left")

    # Viertes Panel: Herzfrequenz Ø + Max (gleiche Einheit -> ein Achsensystem)
    ax = flat_axes[3]
    if trend["hr_mean_bpm"].notna().any():
        ax.plot(xi, trend["hr_mean_bpm"], color="#8E24AA", linewidth=2,
                marker="o", markersize=6, label="Ø Herzfrequenz")
        ax.plot(xi, trend["hr_max_bpm"], color="#8E24AA", linewidth=1.2,
                marker="o", markersize=4, alpha=0.5, linestyle="--", label="Max Herzfrequenz")
        ax.legend(fontsize=8, loc="upper left")
    ax.set_title("Herzfrequenz (bpm)", fontsize=10)
    ax.set_xticks(list(xi))
    ax.set_xticklabels(xlabels, rotation=45, ha="right")
    ax.grid(True, alpha=0.25)

    plt.tight_layout()
    plt.show()


# ============================================================
# VISUALISIERUNGEN
# ============================================================

def plot_sprints(df: pd.DataFrame, title: str = "Sprint Detection") -> None:
    """
    Visualisierung 1: Speed-Verlauf + Geschwindigkeitszonen + echte Herzfrequenz.

    Änderungen gegenüber Original:
    - HR nicht mehr normiert (0–1), sondern echte bpm auf zweiter Y-Achse (twinx)
    - Sprint-Phasen auch im unteren Panel hervorgehoben
    """
    zone_colors = {
        "walking":    "#E3F2FD",
        "jogging":    "#A5D6A7",
        "running":    "#FFF176",
        "high_speed": "#FFB74D",
        "sprint":     "#FF5722",
        "unknown":    "#EEEEEE",
    }

    fig, axes = plt.subplots(2, 1, figsize=(14, 7), sharex=True)
    fig.suptitle(title, fontsize=13, fontweight="bold")

    # --- Oben: Speed-Verlauf ---
    ax = axes[0]
    ax.plot(df["timestamp"], df["speed_smooth"],
            color="#2196F3", linewidth=1.2, label="Speed (geglättet)", zorder=3)
    ax.plot(df["timestamp"], df["speed_raw"],
            color="#90CAF9", linewidth=0.5, alpha=0.5, label="Speed (roh)", zorder=2)
    ax.fill_between(df["timestamp"], 0, df["speed_smooth"],
                    where=df["is_sprint"], color="#FF5722", alpha=0.4,
                    label="Lauf ≥ 60% v_max", zorder=1)

    # Antritte als Dreiecke am Ende der Beschleunigungsphase
    if "accel_id" in df.columns and df["accel_id"].max() > 0:
        last = df[df["accel_id"] > 0].groupby("accel_id").tail(1)
        hi   = last["accel"] >= ACCEL_HIGH_MS2
        ax.scatter(last.loc[~hi, "timestamp"], last.loc[~hi, "speed_raw"],
                   marker="^", s=25, color="#E53935", alpha=0.6,
                   label=f"Antritt ≥ {ACCEL_MODERATE_MS2:g} m/s²", zorder=4)
        ax.scatter(last.loc[hi, "timestamp"], last.loc[hi, "speed_raw"],
                   marker="^", s=55, color="#B71C1C",
                   label=f"Antritt ≥ {ACCEL_HIGH_MS2:g} m/s²", zorder=5)
    ax.set_ylabel("Geschwindigkeit (m/s)")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)

    # --- Unten: Zonen als Hintergrundfarbe ---
    ax2 = axes[1]
    for zone, color in zone_colors.items():
        mask = df["speed_zone"] == zone
        ax2.fill_between(df["timestamp"], 0, 1,
                         where=mask, color=color, alpha=0.7,
                         transform=ax2.get_xaxis_transform())

    # Sprint-Phasen auch hier markieren
    ax2.fill_between(df["timestamp"], 0, 1,
                     where=df["is_sprint"], color="#FF5722", alpha=0.2,
                     transform=ax2.get_xaxis_transform())

    patches = [mpatches.Patch(color=c, label=z)
               for z, c in zone_colors.items() if z != "unknown"]
    ax2.legend(handles=patches, loc="upper left", fontsize=7, ncol=3)
    ax2.set_ylabel("Geschwindigkeitszone")
    ax2.set_xlabel("Zeit")
    ax2.set_ylim(0, 1)
    ax2.set_yticks([])
    ax2.grid(False)

    # Echte HR auf zweiter Y-Achse (twinx)
    if df["heart_rate"].notna().any():
        ax2b = ax2.twinx()
        hr = df["heart_rate"].interpolate()
        ax2b.plot(df["timestamp"], hr,
                  color="#9C27B0", linewidth=1.2, alpha=0.9, label="Herzfrequenz")
        ax2b.set_ylabel("Herzfrequenz (bpm)", color="#9C27B0")
        ax2b.tick_params(axis="y", labelcolor="#9C27B0")
        hr_min = int(hr.min()) - 10
        hr_max = int(hr.max()) + 10
        ax2b.set_ylim(hr_min, hr_max)
        ax2b.yaxis.set_major_locator(ticker.MultipleLocator(20))
        ax2b.legend(loc="upper right", fontsize=8)

    plt.tight_layout()
    plt.show()


def plot_sprint_gps(df: pd.DataFrame,
                    top_n: int = 10,
                    title: str = "Sprint-Routen",
                    output_file: str = "sprint_map.html") -> None:
    """
    Visualisierung 2: Interaktive Folium-Karte mit OpenStreetMap und
    farbig markierten Sprint-Routen.

    Benötigt: pip install folium

    Gibt eine HTML-Datei aus, die sich im Browser öffnen lässt.
    Zoom, Pan und Klick auf Marker funktionieren interaktiv.

    Farbe der Sprint-Routen:
        Gesamte Route → grau (dünn)
        Top-N Sprints → plasma-Heatmap (blau/lila = langsam, gelb = schnell)
        Startpunkt    → grüner Kreis mit Popup (Sprint-Nr., v_max)
        Endpunkt      → roter Kreis

    Parameter
    ----------
    df          : DataFrame aus detect_sprints()
    top_n       : Wie viele Top-Sprints nach v_max hervorheben
    title       : Kartentitel (erscheint als HTML-Titel)
    output_file : Pfad zur HTML-Ausgabedatei
    """
    try:
        import folium
    except ImportError:
        warnings.warn(
            "folium nicht installiert – Karte nicht verfügbar.\n"
            "  pip install folium\n"
            "Fallback: einfacher Lat/Lon-Plot."
        )
        _plot_sprint_gps_fallback(df, top_n, title)
        return

    import matplotlib.colors as mcolors

    df_gps  = df.dropna(subset=["lat", "lon"]).copy()
    summary = sprint_summary(df)

    if summary.empty:
        print("Keine Sprints gefunden.")
        return

    # Kartenzentrum
    center_lat = df_gps["lat"].mean()
    center_lon = df_gps["lon"].mean()

    m = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=19,
        tiles=None
    )

    # ----------------------------------------------------------
    # Hintergrundkarten
    # ----------------------------------------------------------
    # OSM-Kacheln werden bei lokal geöffneten HTML-Dateien (file://)
    # blockiert (kein Referer). Daher: Esri-Satellitenbild über einen
    # ArcGIS-API-Key aus der Umgebungsvariable ARCGIS_API_KEY.
    api_key = os.environ.get("ARCGIS_API_KEY")

    if api_key:
        folium.TileLayer(
            tiles=("https://ibasemaps-api.arcgis.com/arcgis/rest/services/"
                   "World_Imagery/MapServer/tile/{z}/{y}/{x}?token=" + api_key),
            attr="Tiles © Esri — Source: Esri, Maxar, Earthstar Geographics",
            name="Satellit",
            max_zoom=25,
            max_native_zoom=19,
        ).add_to(m)
    else:
        print("  Hinweis: ARCGIS_API_KEY nicht gesetzt – Karte ohne Hintergrundbild.")

    # ----------------------------------------------------------
    # Gesamte Route grau im Hintergrund
    # ----------------------------------------------------------
    route_coords = list(zip(df_gps["lat"], df_gps["lon"]))
    folium.PolyLine(
        route_coords,
        color="gray",
        weight=1.5,
        opacity=0.4,
        tooltip="Gesamte Route"
    ).add_to(m)

    # ----------------------------------------------------------
    # Top-N Sprint-Routen mit Speed-Heatmap
    # ----------------------------------------------------------
    top_ids = summary.nlargest(top_n, "speed_max_ms")["sprint_id"].tolist()
    cmap    = plt.cm.plasma

    for i, sid in enumerate(top_ids):
        sg = df_gps[df_gps["sprint_id"] == sid].copy()
        if len(sg) < 2:
            continue

        sprint_info = summary[summary["sprint_id"] == sid].iloc[0]
        speed       = sg["speed_smooth"]
        norm        = (speed - speed.min()) / (speed.max() - speed.min() + 1e-9)

        # Segmentweise gefärbte Linie (Heatmap)
        for j in range(1, len(sg)):
            rgba  = cmap(norm.iloc[j])
            color = mcolors.to_hex(rgba)
            folium.PolyLine(
                [
                    [sg["lat"].iloc[j-1], sg["lon"].iloc[j-1]],
                    [sg["lat"].iloc[j],   sg["lon"].iloc[j]]
                ],
                color=color,
                weight=5,
                opacity=0.9
            ).add_to(m)

        # Startpunkt: grüner Marker mit Popup
        folium.CircleMarker(
            location=[sg["lat"].iloc[0], sg["lon"].iloc[0]],
            radius=7,
            color="green",
            fill=True,
            fill_color="green",
            fill_opacity=0.9,
            tooltip=f"Sprint {i+1} Start",
            popup=folium.Popup(
                f"<b>Sprint {i+1}</b><br>"
                f"v_max: {sprint_info['speed_max_kmh']:.1f} km/h<br>"
                f"Dauer: {sprint_info['duration_s']}s<br>"
                f"Distanz: {sprint_info['distance_m']:.0f}m",
                max_width=200
            )
        ).add_to(m)

        # Endpunkt: roter Marker
        folium.CircleMarker(
            location=[sg["lat"].iloc[-1], sg["lon"].iloc[-1]],
            radius=5,
            color="red",
            fill=True,
            fill_color="red",
            fill_opacity=0.9,
            tooltip=f"Sprint {i+1} Ende"
        ).add_to(m)

    # Bounds auf sichtbaren Bereich setzen
    m.fit_bounds([
        [df_gps["lat"].min(), df_gps["lon"].min()],
        [df_gps["lat"].max(), df_gps["lon"].max()]
    ])

    # HTML speichern und öffnen
    m.save(output_file)
    print(f"  Karte gespeichert: {output_file}")

    # Automatisch im Browser öffnen
    import webbrowser, pathlib
    webbrowser.open(pathlib.Path(output_file).resolve().as_uri())


def _plot_sprint_gps_fallback(df: pd.DataFrame,
                               top_n: int = 10,
                               title: str = "Sprint-Routen (GPS)") -> None:
    """Einfacher Lat/Lon-Plot als Fallback wenn folium fehlt."""
    df_gps  = df.dropna(subset=["lat", "lon"])
    summary = sprint_summary(df)

    if summary.empty:
        print("Keine Sprints gefunden.")
        return

    top_ids = summary.nlargest(top_n, "speed_max_ms")["sprint_id"].tolist()
    colors  = plt.cm.tab10.colors

    fig, ax = plt.subplots(figsize=(10, 8))
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.plot(df_gps["lon"], df_gps["lat"],
            color="lightgray", linewidth=0.8, alpha=0.5, zorder=1)

    for i, sid in enumerate(top_ids):
        sg    = df_gps[df_gps["sprint_id"] == sid]
        if len(sg) < 2:
            continue
        speed = sg["speed_smooth"]
        norm  = (speed - speed.min()) / (speed.max() - speed.min() + 1e-9)
        for j in range(1, len(sg)):
            ax.plot(sg["lon"].iloc[j-1:j+1], sg["lat"].iloc[j-1:j+1],
                    color=plt.cm.plasma(norm.iloc[j]), linewidth=3, zorder=3)
        ax.scatter(sg["lon"].iloc[0], sg["lat"].iloc[0],
                   color=colors[i % 10], s=60, marker="o", zorder=4,
                   label=f"Sprint {i+1} ({speed.max()*3.6:.1f} km/h)")

    ax.legend(fontsize=8)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.axis("equal")
    plt.tight_layout()
    plt.show()


# ============================================================
# QUICK-START WENN DIREKT AUSGEFÜHRT
# ============================================================

if __name__ == "__main__":
    import sys
    import glob, os
    from loader import load_fit, load_multiple, personal_max_speed

    target = sys.argv[1] if len(sys.argv) > 1 else "."

    # Einzelfile (z.B. "..\Aufzeichnungen\26-06-30.fit") oder Ordner erkennen
    if os.path.isfile(target):
        files = [target]
    elif os.path.isdir(target):
        files = sorted(glob.glob(os.path.join(target, "*.fit")))
    else:
        print(f"Pfad nicht gefunden: {target}")
        sys.exit(1)

    if not files:
        print(f"Keine .fit Dateien gefunden in: {target}")
        sys.exit(1)

    print("Berechne persönliche v_max über alle Sessions...")
    df_all = load_multiple(files, verbose=False)
    v_max  = personal_max_speed(df_all)

    # Karten landen gesammelt in <projekt>/output/maps/ statt im Arbeitsverzeichnis
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    map_dir      = os.path.join(project_root, "output", "maps")
    os.makedirs(map_dir, exist_ok=True)

    trend_rows = []

    for f in files:
        print(f"\n{'='*55}")
        print(f"  Session: {os.path.basename(f)}")
        print(f"{'='*55}")

        df = load_fit(f)
        df = detect_sprints(df, v_max=v_max)
        df = detect_accelerations(df)
        trend_rows.append(session_trend_row(df))

        print("\n  Zonenverteilung:")
        print(zone_distribution(df).to_string(index=False))

        stats = sprint_summary(df)
        if not stats.empty:
            top_n = min(10, len(stats))
            print(f"\n  Top {top_n} Sprints nach v_max:")
            print(stats.nlargest(top_n, "speed_max_ms")
                  [["sprint_id", "duration_s", "speed_max_kmh",
                    "distance_m", "category"]]
                  .to_string(index=False))

        acc = accel_summary(df)
        if not acc.empty:
            top_n = min(10, len(acc))
            print(f"\n  Top {top_n} Antritte nach Peak-Beschleunigung:")
            print(acc.nlargest(top_n, "accel_peak")
                  [["accel_id", "time_start", "v_start_kmh", "v_end_kmh",
                    "delta_v_kmh", "accel_peak", "category"]]
                  .to_string(index=False))

        session_name = os.path.splitext(os.path.basename(f))[0]
        plot_sprints(df, title=f"Sprint Detection – {os.path.basename(f)}")
        plot_sprint_gps(df,
                        title=f"Sprint-Routen – {os.path.basename(f)}",
                        output_file=os.path.join(map_dir, f"sprint_map_{session_name}.html"))

    # Season-Trend nur sinnvoll bei mehreren Sessions
    if len(files) > 1:
        print(f"\n{'='*55}")
        print("  Season-Trend (alle Sessions)")
        print(f"{'='*55}\n")

        trend = season_trend_summary(trend_rows)
        print(trend.to_string(index=False))

        plot_season_trend(trend, title="Season-Trend – alle Sessions")