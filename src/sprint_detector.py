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
    summary = sprint_summary(df)

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
# Übliche Zoneneinteilung relativ zu v_max:
#   Walking:     0  – 30% v_max
#   Jogging:    30  – 50% v_max
#   Running:    50  – 70% v_max
#   High Speed: 70  – 85% v_max
#   Sprint:     > 85% v_max
#
# Für Flag Football (explosive Starts, kurze Distanzen) wurde
# die Sprint-Schwelle auf 60% gesetzt – etwas niedriger als im
# Fußball, da die Spielzüge kürzer sind und selten volle
# Maximalgeschwindigkeit erreicht wird. Dieser Wert ist anpassbar.

ZONE_BOUNDARIES_PCT = {
    "walking":    (0.00, 0.30),
    "jogging":    (0.30, 0.50),
    "running":    (0.50, 0.70),
    "high_speed": (0.70, 0.85),
    "sprint":     (0.85, 1.00),
}

SPRINT_ZONE_PCT = 0.6   # Schwelle für Flag Football (anpassbar)

MIN_SPRINT_DURATION_S = 2


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

    3. Mindestdauer-Filter: Bursts < min_duration_s werden entfernt
       → [8] GPS-Rauschen bei hohen Speeds eliminieren

    4. Sprint-IDs: zusammenhängende Phasen erhalten eine eindeutige ID
       → Grundlage für spätere Play-Segmentierung

    Parameter
    ----------
    df : pd.DataFrame
        Ausgabe von loader.load_fit()
    v_max : float
        Persönliche Maximalgeschwindigkeit in m/s.
        Berechnen mit loader.personal_max_speed()
    sprint_pct : float
        Anteil von v_max als Sprint-Schwelle (default: 0.60)
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
    df["_raw_sprint_id"] = (df["is_sprint"] != df["is_sprint"].shift()).cumsum()

    for sid, group in df[df["is_sprint"]].groupby("_raw_sprint_id"):
        if len(group) < min_duration_s:
            df.loc[group.index, "is_sprint"] = False

    df.drop(columns=["_raw_sprint_id"], inplace=True)

    # ----------------------------------------------------------
    # 4. Sprint-IDs
    # ----------------------------------------------------------
    df["sprint_id"] = 0
    state_change = (df["is_sprint"] != df["is_sprint"].shift()).cumsum()
    sprint_counter = 0

    for sid, group in df[df["is_sprint"]].groupby(state_change[df["is_sprint"]]):
        sprint_counter += 1
        df.loc[group.index, "sprint_id"] = sprint_counter

    # ----------------------------------------------------------
    # 5. Logging
    # ----------------------------------------------------------
    n_sprints     = df["sprint_id"].max()
    total_time    = df["is_sprint"].sum()
    pct_time      = round(df["is_sprint"].mean() * 100, 1)
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
    """
    sprints = df[df["is_sprint"] & (df["sprint_id"] > 0)]

    rows = []
    for sid, group in sprints.groupby("sprint_id"):
        group = group.copy()
        accel = group["speed_smooth"].diff().fillna(0)

        rows.append({
            "sprint_id":      sid,
            "duration_s":     len(group),
            "speed_max_ms":   round(group["speed_smooth"].max(), 2),
            "speed_max_kmh":  round(group["speed_smooth"].max() * 3.6, 1),
            "speed_mean_ms":  round(group["speed_smooth"].mean(), 2),
            "distance_m":     round(group["speed_smooth"].sum(), 1),
            "accel_peak":     round(accel.max(), 2),
            "time_start":     group["timestamp"].iloc[0],
            "time_end":       group["timestamp"].iloc[-1],
        })

    return pd.DataFrame(rows)


# ============================================================
# ZONENVERTEILUNG
# ============================================================

def zone_distribution(df: pd.DataFrame) -> pd.DataFrame:
    """
    Gibt die prozentuale Verteilung der Geschwindigkeitszonen zurück.
    """
    zone_order = ["walking", "jogging", "running", "high_speed", "sprint", "unknown"]
    counts = df["speed_zone"].value_counts()

    result = pd.DataFrame({
        "zone":    zone_order,
        "seconds": [counts.get(z, 0) for z in zone_order],
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

    return {
        "source_file":       df["source_file"].iloc[0],
        "date":              df["timestamp"].iloc[0].date(),
        "duration_min":      round((df["timestamp"].max() - df["timestamp"].min()).total_seconds() / 60, 1),
        "n_sprints":         int(len(stats)),
        "sprint_time_s":     int(df["is_sprint"].sum()),
        "speed_max_kmh":     round(stats["speed_max_kmh"].max(), 1) if not stats.empty else 0.0,
        "sprint_distance_m": round(stats["distance_m"].sum(), 1) if not stats.empty else 0.0,
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
        ("n_sprints",         "Anzahl Sprints",          "#FB8C00"),
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
                    label="Sprint", zorder=1)
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

    # OpenStreetMap-Kacheln (max_zoom=25 für sehr hohe Auflösung auf Spielfeld)
    folium.TileLayer(
        tiles="OpenStreetMap",
        max_zoom=25,
        max_native_zoom=19,
        name="OSM"
    ).add_to(m)

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

    trend_rows = []

    for f in files:
        print(f"\n{'='*55}")
        print(f"  Session: {os.path.basename(f)}")
        print(f"{'='*55}")

        df = load_fit(f)
        df = detect_sprints(df, v_max=v_max)
        trend_rows.append(session_trend_row(df))

        print("\n  Zonenverteilung:")
        print(zone_distribution(df).to_string(index=False))

        stats = sprint_summary(df)
        if not stats.empty:
            top_n = min(10, len(stats))
            print(f"\n  Top {top_n} Sprints nach v_max:")
            print(stats.nlargest(top_n, "speed_max_ms")
                  [["sprint_id", "duration_s", "speed_max_kmh",
                    "distance_m", "accel_peak"]]
                  .to_string(index=False))

        session_name = os.path.splitext(os.path.basename(f))[0]
        plot_sprints(df, title=f"Sprint Detection – {os.path.basename(f)}")
        plot_sprint_gps(df,
                        title=f"Sprint-Routen – {os.path.basename(f)}",
                        output_file=f"sprint_map_{session_name}.html")

    # Season-Trend nur sinnvoll bei mehreren Sessions
    if len(files) > 1:
        print(f"\n{'='*55}")
        print("  Season-Trend (alle Sessions)")
        print(f"{'='*55}\n")

        trend = season_trend_summary(trend_rows)
        print(trend.to_string(index=False))

        plot_season_trend(trend, title="Season-Trend – alle Sessions")