"""
loader.py
---------
Saubere Datenpipeline für Flag Football GPS-Analysen.

Verwendung (Einzelfile):
    from loader import load_fit
    df = load_fit("mein_training.fit")

Verwendung (mehrere Files):
    from loader import load_multiple
    df_all = load_multiple("data/")   # lädt alle .fit Files im Ordner

Rückgabe immer: pandas DataFrame mit einheitlichen Spaltennamen,
                bereinigten GPS-Koordinaten und geglättetem Speed.
"""

import os
import glob
import warnings

import numpy as np
import pandas as pd
from fitparse import FitFile


# ============================================================
# KONSTANTEN (hier anpassen, nicht tief im Code suchen)
# ============================================================

# GPS-Konvertierung: Garmin speichert in Semicircles
SEMICIRCLE_TO_DEG = 180.0 / (2 ** 31)

# Glättungsfenster für Speed (Sekunden, da 1 Hz Abtastrate)
# 5 oder 3 Sekunden = guter Kompromiss zwischen Reaktivität und Rauschen
SMOOTH_WINDOW = 3

# Minimale GPS-Genauigkeit: Punkte mit mehr als X Metern Abweichung
# zum Vorpunkt werden als Ausreißer markiert (ca. 50 km/h Sprung = Fehler)
MAX_SPEED_MS = 12.0   # m/s ≈ 43 km/h – realistisches Max für Amateure

# Abtastintervall: Garmin zeichnet bei "Jede Sekunde" mit 1 Hz auf, bei
# "Smart Recording" oder pausierter Uhr entstehen aber größere Abstände.
# Abstände > MAX_GAP_S gelten als Aufzeichnungslücke: dort wird nicht
# geglättet/interpoliert und kein Sprint darf über die Lücke hinweg laufen.
MAX_GAP_S = 2.0


# ============================================================
# HAUPT-LADEFUNKTION
# ============================================================

def load_fit(filepath: str,
             trim_start_min: float = 0.0,
             trim_end_min: float = 0.0) -> pd.DataFrame:
    """
    Lädt eine einzelne .fit Datei und gibt einen sauberen DataFrame zurück.

    Parameter
    ----------
    filepath : str
        Pfad zur .fit Datei.
    trim_start_min : float
        Minuten vom Anfang abschneiden (z.B. Aufwärmen).
    trim_end_min : float
        Minuten vom Ende abschneiden (z.B. Abkühlen).

    Rückgabe
    --------
    pd.DataFrame mit Spalten:
        timestamp, dt_s, segment_id, lat, lon, gps_valid,
        speed_raw, speed_smooth, heart_rate, distance, source_file

        dt_s       – effektive Dauer des Samples in Sekunden (Abstand zum
                     Vorpunkt; an Lücken und am Anfang = typisches Intervall)
        segment_id – Nummer des lückenlosen Aufzeichnungsabschnitts
    """

    # ----------------------------------------------------------
    # 1. FIT-File einlesen
    # ----------------------------------------------------------
    fitfile = FitFile(filepath)

    rows = []
    for record in fitfile.get_messages("record"):
        row = {field.name: field.value for field in record}
        rows.append(row)

    if not rows:
        raise ValueError(f"Keine 'record'-Messages in {filepath} gefunden.")

    df = pd.DataFrame(rows)

    # ----------------------------------------------------------
    # 2. Timestamp
    # ----------------------------------------------------------
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    # Zeitbereich trimmen (z.B. Aufwärmen rausschneiden)
    t_start = df["timestamp"].min() + pd.Timedelta(minutes=trim_start_min)
    t_end   = df["timestamp"].max() - pd.Timedelta(minutes=trim_end_min)
    df = df[(df["timestamp"] >= t_start) & (df["timestamp"] <= t_end)].copy()

    # Doppelte Zeitstempel entfernen (dt = 0 würde Division durch 0 erzeugen)
    df = df.drop_duplicates(subset="timestamp", keep="first").reset_index(drop=True)

    # ----------------------------------------------------------
    # 2b. Abtastintervall prüfen & Aufzeichnungslücken erkennen
    # ----------------------------------------------------------
    dt_raw = df["timestamp"].diff().dt.total_seconds()
    nominal_dt = float(dt_raw.median()) if dt_raw.notna().any() else 1.0

    is_gap = dt_raw > MAX_GAP_S
    df["segment_id"] = is_gap.cumsum().astype(int)

    # Effektive Sample-Dauer: an Lücken und am ersten Punkt das typische
    # Intervall, sonst der echte Abstand zum Vorpunkt
    df["dt_s"] = dt_raw.where(~is_gap & dt_raw.notna(), nominal_dt)

    n_gaps = int(is_gap.sum())
    if n_gaps or abs(nominal_dt - 1.0) > 1e-6:
        gap_info = ", ".join(f"{g:.0f}s" for g in dt_raw[is_gap])
        warnings.warn(
            f"{os.path.basename(filepath)}: typisches Intervall {nominal_dt:g}s, "
            f"{n_gaps} Aufzeichnungslücke(n) > {MAX_GAP_S:g}s"
            + (f" ({gap_info})" if n_gaps else "")
        )

    # ----------------------------------------------------------
    # 3. GPS-Koordinaten
    # ----------------------------------------------------------
    if "position_lat" in df.columns and "position_long" in df.columns:
        df["lat"] = df["position_lat"] * SEMICIRCLE_TO_DEG
        df["lon"] = df["position_long"] * SEMICIRCLE_TO_DEG
    else:
        warnings.warn(f"{filepath}: Keine GPS-Koordinaten gefunden.")
        df["lat"] = np.nan
        df["lon"] = np.nan

    # GPS-Gültigkeit: nur Punkte mit beiden Koordinaten
    df["gps_valid"] = df["lat"].notna() & df["lon"].notna()

    # ----------------------------------------------------------
    # 4. Speed
    # ----------------------------------------------------------
    # Garmin liefert enhanced_speed (m/s), fallback auf speed
    if "enhanced_speed" in df.columns:
        df["speed_raw"] = pd.to_numeric(df["enhanced_speed"], errors="coerce")
    elif "speed" in df.columns:
        df["speed_raw"] = pd.to_numeric(df["speed"], errors="coerce")
    else:
        warnings.warn(f"{filepath}: Kein Speed-Feld gefunden.")
        df["speed_raw"] = np.nan

    df["speed_raw"] = df["speed_raw"].fillna(0.0)

    # Physikalisch unrealistische Werte als NaN markieren
    df.loc[df["speed_raw"] > MAX_SPEED_MS, "speed_raw"] = np.nan
    # Interpolation und Glättung je lückenlosem Abschnitt, damit nicht
    # über eine Aufzeichnungspause hinweg gemittelt wird
    seg = df.groupby("segment_id", sort=False)["speed_raw"]
    df["speed_raw"] = seg.transform(
        lambda s: s.interpolate(method="linear", limit_direction="both")
    ).fillna(0.0)

    # Glättung (rolling mean, zentriert)
    df["speed_smooth"] = (
        df.groupby("segment_id", sort=False)["speed_raw"]
        .transform(lambda s: s.rolling(window=SMOOTH_WINDOW, center=True,
                                       min_periods=1).mean())
    )

    # ----------------------------------------------------------
    # 5. Herzfrequenz
    # ----------------------------------------------------------
    if "heart_rate" in df.columns:
        df["heart_rate"] = pd.to_numeric(df["heart_rate"], errors="coerce")
    else:
        df["heart_rate"] = np.nan

    # ----------------------------------------------------------
    # 6. Distanz
    # ----------------------------------------------------------
    if "distance" in df.columns:
        df["distance"] = pd.to_numeric(df["distance"], errors="coerce")
    else:
        df["distance"] = np.nan

    # ----------------------------------------------------------
    # 7. Quelldatei als Spalte (wichtig bei Multi-Session-Analysen)
    # ----------------------------------------------------------
    df["source_file"] = os.path.basename(filepath)

    # ----------------------------------------------------------
    # 8. Nur relevante Spalten zurückgeben
    # ----------------------------------------------------------
    keep = [
        "timestamp", "dt_s", "segment_id", "lat", "lon", "gps_valid",
        "speed_raw", "speed_smooth",
        "heart_rate", "distance",
        "source_file"
    ]
    df = df[[c for c in keep if c in df.columns]].reset_index(drop=True)

    return df


# ============================================================
# MEHRERE FILES LADEN (ganzer Ordner)
# ============================================================

def load_multiple(folder_or_files,
                  trim_start_min: float = 0.0,
                  trim_end_min: float = 0.0,
                  verbose: bool = True) -> pd.DataFrame:
    """
    Lädt alle .fit Dateien aus einem Ordner (oder einer Liste von Pfaden)
    und gibt einen kombinierten DataFrame zurück.

    Nützlich für:
    - Berechnung der persönlichen Maximalgeschwindigkeit
    - Vergleich mehrerer Trainings
    - Datenbasis für Validierung

    Parameter
    ----------
    folder_or_files : str oder list
        Ordnerpfad (str) oder Liste von .fit Dateipfaden.
    trim_start_min / trim_end_min : float
        Wird auf alle Files gleich angewendet.
    verbose : bool
        Gibt Ladestatus aus.

    Rückgabe
    --------
    pd.DataFrame (alle Sessions zusammen, unterscheidbar via source_file)
    """

    if isinstance(folder_or_files, str):
        files = sorted(glob.glob(os.path.join(folder_or_files, "*.fit")))
    else:
        files = list(folder_or_files)

    if not files:
        raise FileNotFoundError(f"Keine .fit Dateien gefunden in: {folder_or_files}")

    dfs = []
    for f in files:
        try:
            df = load_fit(f, trim_start_min, trim_end_min)
            dfs.append(df)
            if verbose:
                print(f"  ✓ {os.path.basename(f):40s}  {len(df):>5} Punkte")
        except Exception as e:
            warnings.warn(f"  ✗ {f}: {e}")

    combined = pd.concat(dfs, ignore_index=True)

    if verbose:
        print(f"\n  Gesamt: {len(files)} Sessions, {len(combined)} Datenpunkte")

    return combined


# ============================================================
# HILFSFUNKTIONEN (für spätere Sprint/Play-Module)
# ============================================================

def session_summary(df: pd.DataFrame) -> dict:
    """
    Gibt eine kompakte Übersicht einer geladenen Session zurück.
    Nützlich um alle Trainings schnell zu verstehen (Schritt 1).
    """
    duration = df["dt_s"].sum() / 60   # aufgezeichnete Zeit ohne Pausen

    return {
        "source_file":    df["source_file"].iloc[0],
        "date":           df["timestamp"].iloc[0].date(),
        "duration_min":   round(duration, 1),
        "n_points":       len(df),
        "n_gaps":         int(df["segment_id"].max()),
        "gps_coverage_%": round(df["gps_valid"].mean() * 100, 1),
        "speed_max_ms":   round(df["speed_raw"].max(), 2),
        "speed_max_kmh":  round(df["speed_raw"].max() * 3.6, 1),
        "speed_mean_ms":  round(df["speed_smooth"].mean(), 2),
        "hr_mean_bpm":    round(df["heart_rate"].mean(), 1) if df["heart_rate"].notna().any() else None,
        "hr_max_bpm":     int(df["heart_rate"].max()) if df["heart_rate"].notna().any() else None,
    }


def personal_max_speed(df_all: pd.DataFrame) -> float:
    """
    Berechnet die persönliche Maximalgeschwindigkeit über alle Sessions.

    Warum: Science for Sport [7] empfiehlt Sprint-Schwellwerte relativ
    zur individuellen Maximalgeschwindigkeit zu setzen, nicht absolut.
    Typisch: Sprint = > 70-80% der persönlichen Maximalgeschwindigkeit.

    Rückgabe: v_max in m/s
    """
    v_abs  = df_all["speed_raw"].max()
    v_p999 = df_all["speed_raw"].quantile(0.9999)
    
    print(f"  Absolutes Maximum:        {v_abs:.2f} m/s  ({v_abs*3.6:.1f} km/h)")
    #print(f"  99.99. Perzentil (robust): {v_p999:.2f} m/s  ({v_p999*3.6:.1f} km/h)")
    #print(f"  → Empfehlung Sprint-Basis: {v_p999:.2f} m/s")
    
    return v_p999


# ============================================================
# QUICK-START WENN DIREKT AUSGEFÜHRT
# ============================================================

if __name__ == "__main__":
    import sys

    # Einzelfile: python loader.py mein_training.fit
    # Ordner:     python loader.py data/

    target = sys.argv[1] if len(sys.argv) > 1 else "."

    if os.path.isfile(target):
        df = load_fit(target)
        print(pd.DataFrame([session_summary(df)]).T.to_string())

    elif os.path.isdir(target):
        print(f"\nLade alle .fit Dateien aus: {target}\n")
        df_all = load_multiple(target)

        summaries = []
        for src, group in df_all.groupby("source_file"):
            summaries.append(session_summary(group))

        print("\n--- Session Übersicht ---")
        print(pd.DataFrame(summaries).to_string(index=False))

        print("\n--- Persönliche Maximalgeschwindigkeit ---")
        personal_max_speed(df_all)

    else:
        print(f"Pfad nicht gefunden: {target}")