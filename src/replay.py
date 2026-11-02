"""
replay.py
---------
Interaktiver Session-Replay: erzeugt eine eigenständige HTML-Seite, auf der
man ein Intervall einer Einheit (z.B. die letzte Stunde Gametime) auswählt und
die eigenen Laufwege wie ein Video über das Feld abspielt.

Funktionen der Seite:
    - Session-Auswahl (alle übergebenen .fit-Dateien in einer Seite)
    - Intervall per Ziehen in der Timeline oder Presets ("Letzte 60 min")
    - Abspielen mit 1×–60×, Spurlänge wählbar, Spur nach Speed eingefärbt
    - Antritte (≥ 2 m/s²) und Läufe (≥ 60 % v_max) im Replay markiert
    - Liste der Antritte im Intervall – Klick springt zur Szene
    - "Als Video speichern" nimmt das Intervall als .webm auf

Unter dem Feld liegt ein Satellitenbild (Esri World Imagery), das mit dem
Feld gedreht wird. Die Ausrichtung kommt aus FIELDS (pro Platz am
Satellitenbild ausgemessen) und lässt sich auf der Seite prüfen
("Norden oben") und in 1°-Schritten nachjustieren.

Verwendung:
    python src/replay.py data/trainingMonarchs/
    python src/replay.py data/trainingMonarchs/26-10-29.fit
    python src/replay.py data/            (alle Unterordner)

Ausgabe: output/replay.html (öffnet sich automatisch im Browser)
"""

import os
import sys
import glob
import json
import warnings
import contextlib
import io

import numpy as np
import pandas as pd

from loader import load_fit, load_multiple, personal_max_speed
from sprint_detector import (detect_sprints, detect_accelerations,
                             accel_summary, sprint_summary,
                             ZONE_BOUNDARIES_PCT, ACCEL_HIGH_MS2)

LOCAL_TZ = "Europe/Berlin"

# ------------------------------------------------------------
# BEKANNTE PLÄTZE: feste Feldausrichtung
# ------------------------------------------------------------
# Die Drehung wird NICHT aus den Laufwegen geschätzt (PCA lieferte für
# denselben Platz je nach Session 57°, 90° oder 119°), sondern pro Platz
# einmal am Satellitenbild ausgemessen.
#   angle_deg = Richtung der Feld-Längsachse, gemessen von Osten gegen
#               den Uhrzeigersinn (90° = Nord–Süd). Die Seite dreht diese
#               Achse horizontal.
#   radius_m  = Sessions, deren Mittelpunkt näher liegt, nutzen diesen Platz.
# Neuer Platz: im Replay mit "Norden oben" prüfen, mit ⟲/⟳ ausrichten,
# den angezeigten Winkel hier eintragen.
FIELDS = [
    {"name": "Monarchs-Platz", "lat": 51.07610, "lon": 13.74165, "angle_deg": 91.2, "radius_m": 250},
    {"name": "Uni-Sportplatz", "lat": 51.03008, "lon": 13.75243, "angle_deg": 113.0, "radius_m": 250},
]
POS_SMOOTH_WINDOW = 3      # Glättung der Positionen nur für die Darstellung
ZONES = list(ZONE_BOUNDARIES_PCT.keys())


# ============================================================
# DATEN AUFBEREITEN
# ============================================================

M_PER_DEG_LAT = 110_540.0
M_PER_DEG_LON = 111_320.0   # × cos(lat0)


def _to_local_xy(lat, lon):
    """Lat/Lon → lokale Meter (x = Ost, y = Nord; equirektangulär,
    für Spielfeldgröße exakt genug). Gibt auch den Bezugspunkt zurück."""
    lat0, lon0 = float(np.nanmean(lat)), float(np.nanmean(lon))
    x = (lon - lon0) * np.cos(np.radians(lat0)) * M_PER_DEG_LON
    y = (lat - lat0) * M_PER_DEG_LAT
    return x, y, lat0, lon0


def _field_for(lat0, lon0):
    """Nächster bekannter Platz im Umkreis → (Name, Winkel in rad) oder (None, 0)."""
    best, best_d = None, float("inf")
    for f in FIELDS:
        dx = (f["lon"] - lon0) * np.cos(np.radians(lat0)) * M_PER_DEG_LON
        dy = (f["lat"] - lat0) * M_PER_DEG_LAT
        d = float(np.hypot(dx, dy))
        if d <= f["radius_m"] and d < best_d:
            best, best_d = f, d
    if best is None:
        return None, 0.0          # unbekannter Platz → Norden oben
    return best["name"], float(np.radians(best["angle_deg"]))


def session_payload(path: str, v_max: float, label: str) -> dict:
    with contextlib.redirect_stdout(io.StringIO()):
        df = load_fit(path)
        df = detect_sprints(df, v_max=v_max)
        df = detect_accelerations(df)

    # Positionen: fehlende GPS-Punkte je Abschnitt interpolieren, leicht glätten
    for c in ("lat", "lon"):
        df[c] = df.groupby("segment_id")[c].transform(
            lambda s: s.interpolate(limit_direction="both"))
        df[c] = df.groupby("segment_id")[c].transform(
            lambda s: s.rolling(POS_SMOOTH_WINDOW, center=True, min_periods=1).mean())

    # Koordinaten bleiben nordausgerichtet (x = Ost, y = Nord); die Drehung
    # zur Feldausrichtung macht die Seite, damit Satellitenbild und Laufwege
    # gemeinsam gedreht werden und man sie umschalten/nachjustieren kann.
    x, y, lat0, lon0 = _to_local_xy(df["lat"].to_numpy(), df["lon"].to_numpy())
    field_name, ang = _field_for(lat0, lon0)

    ts = df["timestamp"]
    t = (ts - ts.iloc[0]).dt.total_seconds().to_numpy()
    start_local = (pd.Timestamp(ts.iloc[0]).tz_localize("UTC").tz_convert(LOCAL_TZ))
    start_sod = start_local.hour * 3600 + start_local.minute * 60 + start_local.second

    zone_idx = df["speed_zone"].map({z: i for i, z in enumerate(ZONES)}).fillna(0).astype(int)
    hr = df["heart_rate"].to_numpy()

    acc = accel_summary(df)
    t0 = ts.iloc[0]
    accels = [
        [round((r.time_start - t0).total_seconds(), 1),
         round((r.time_end - t0).total_seconds(), 1),
         float(r.accel_peak), float(r.v_start_kmh), float(r.v_end_kmh),
         1 if r.accel_peak >= ACCEL_HIGH_MS2 else 0]
        for r in acc.itertuples()
    ]
    spr = sprint_summary(df)
    sprints = [
        [round((r.time_start - t0).total_seconds(), 1),
         round((r.time_end - t0).total_seconds(), 1),
         float(r.speed_max_kmh), 1 if r.category == "sprint" else 0]
        for r in spr.itertuples()
    ] if not spr.empty else []

    def r1(a):
        return [None if not np.isfinite(v) else round(float(v), 1) for v in a]

    return {
        "label": label,
        "date": start_local.strftime("%d.%m.%Y"),
        "startSod": int(start_sod),
        "lat0": lat0,
        "lon0": lon0,
        "ang": round(ang, 4),
        "field": field_name,
        "t": r1(t),
        "x": r1(x),
        "y": r1(y),
        "v": [round(float(v), 2) for v in df["speed_raw"]],
        "hr": [None if not np.isfinite(h) else int(h) for h in hr],
        "zone": zone_idx.tolist(),
        "seg": df["segment_id"].astype(int).tolist(),
        "spr": df["is_sprint"].astype(int).tolist(),
        "accels": accels,
        "sprints": sprints,
    }


def build_replay(files, out_file, v_max):
    sessions = []
    for f in files:
        rel = os.path.relpath(f, os.path.commonpath(files) if len(files) > 1 else os.path.dirname(f))
        label = os.path.splitext(rel.replace("\\", "/"))[0]
        folder = os.path.basename(os.path.dirname(f))
        if "/" not in label:
            label = f"{folder}/{label}"
        sessions.append(session_payload(f, v_max, label))
        fld = sessions[-1]["field"]
        print(f"  ✓ {label:30s} {('Platz: ' + fld) if fld else 'unbekannter Platz → Norden oben'}")

    # Satellitenbild: Esri World Imagery. Mit ARCGIS_API_KEY über den
    # Basemap-Dienst mit Key, sonst über den öffentlichen Kachel-Endpunkt.
    api_key = os.environ.get("ARCGIS_API_KEY")
    if api_key:
        tile_url = ("https://ibasemaps-api.arcgis.com/arcgis/rest/services/"
                    "World_Imagery/MapServer/tile/{z}/{y}/{x}?token=" + api_key)
        print("  Kartenhintergrund: Esri World Imagery (mit ARCGIS_API_KEY –"
              " der Key steht damit in der HTML-Datei, nicht weitergeben)")
    else:
        tile_url = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
                    "World_Imagery/MapServer/tile/{z}/{y}/{x}")
        print("  Kartenhintergrund: Esri World Imagery (öffentlicher Endpunkt)")

    payload = {
        "tileUrl": tile_url,
        "vmax": round(float(v_max), 3),
        "zones": ZONES,
        "zoneBounds": [ZONE_BOUNDARIES_PCT[z][0] for z in ZONES],
        "accelHigh": ACCEL_HIGH_MS2,
        "sessions": sessions,
    }
    html = HTML_TEMPLATE.replace("__DATA__", json.dumps(payload, separators=(",", ":")))
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"\n  Replay gespeichert: {out_file}")


# ============================================================
# HTML-TEMPLATE
# ============================================================

HTML_TEMPLATE = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Session Replay</title>
<style>
:root{
  --bg:#0f1412; --panel:#161d1a; --panel2:#1c2521; --line:#2a3630;
  --text:#e8efe9; --muted:#8fa197; --accent:#ffcc33; --hot:#ff4d3d;
  --field:#183326; --field-line:rgba(255,255,255,.10);
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--text);
  font:14px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
button,select{font:inherit;color:var(--text);background:var(--panel2);
  border:1px solid var(--line);border-radius:8px;padding:6px 10px;cursor:pointer}
button:hover,select:hover{border-color:#44564c}
button.on{background:var(--accent);color:#111;border-color:var(--accent);font-weight:600}
.app{display:grid;grid-template-rows:auto 1fr auto;height:100vh;gap:10px;padding:12px}
header{display:flex;flex-wrap:wrap;align-items:center;gap:10px}
header h1{font-size:16px;margin:0 8px 0 0;letter-spacing:.02em}
header .grow{flex:1}
.group{display:flex;gap:4px;align-items:center}
.group .lbl{color:var(--muted);font-size:12px;margin-right:4px}
main{display:grid;grid-template-columns:1fr 300px;gap:10px;min-height:0}
.fieldwrap{position:relative;background:var(--field);border-radius:12px;overflow:hidden;min-height:260px}
#field{position:absolute;inset:0;width:100%;height:100%}
aside{display:flex;flex-direction:column;gap:10px;min-height:0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px}
.big{font-size:40px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1}
.unit{font-size:14px;color:var(--muted);font-weight:500;margin-left:4px}
.row{display:flex;justify-content:space-between;gap:8px;font-variant-numeric:tabular-nums}
.row span:first-child{color:var(--muted)}
.zone{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600;color:#111}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.stat .v{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
.stat .k{font-size:11px;color:var(--muted)}
#events{overflow:auto;flex:1;min-height:80px;padding:6px}
#events .ev{display:flex;gap:8px;align-items:center;padding:6px;border-radius:8px;cursor:pointer;font-variant-numeric:tabular-nums}
#events .ev:hover{background:var(--panel2)}
#events .ev.past{opacity:.55}
.tri{width:0;height:0;border-left:6px solid transparent;border-right:6px solid transparent;border-bottom:10px solid var(--hot)}
.tri.mod{border-bottom-color:#ff9a8f}
footer{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px 12px;display:grid;gap:8px}
.controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
#clock{font-variant-numeric:tabular-nums;font-weight:600;min-width:150px}
#timeline{width:100%;height:90px;display:block;cursor:crosshair;border-radius:8px}
.hint{color:var(--muted);font-size:12px}
.overlay{position:absolute;left:10px;top:10px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;
  background:rgba(15,20,18,.78);border:1px solid var(--line);border-radius:10px;padding:6px;backdrop-filter:blur(4px)}
.overlay button{padding:4px 8px;font-size:13px}
.overlay .sep{width:1px;height:20px;background:var(--line);margin:0 2px}
#angle{font-variant-numeric:tabular-nums;font-size:12px;color:var(--muted);min-width:150px}
#mapNote{position:absolute;left:10px;bottom:40px;font-size:12px;color:#ffd27a;display:none}
#rec.recording{background:var(--hot);border-color:var(--hot);color:#fff}
@media (max-width:860px){
  .app{height:auto;min-height:100vh;display:flex;flex-direction:column}
  main{grid-template-columns:1fr}
  .fieldwrap{min-height:55vh}
  #events{max-height:180px}
}
</style>
</head>
<body>
<div class="app">
  <header>
    <h1>Session Replay</h1>
    <select id="sess"></select>
    <div class="group"><span class="lbl">Intervall</span>
      <button data-preset="all">Ganze Einheit</button>
      <button data-preset="last60">Letzte 60 min</button>
      <button data-preset="last30">Letzte 30 min</button>
    </div>
    <div class="grow"></div>
    <div class="group"><span class="lbl">Spur</span>
      <button data-trail="10">10 s</button>
      <button data-trail="30" class="on">30 s</button>
      <button data-trail="120">2 min</button>
      <button data-trail="0">Alles</button>
    </div>
  </header>

  <main>
    <div class="fieldwrap">
      <canvas id="field"></canvas>
      <div class="overlay">
        <button id="mapBtn" class="on" title="Satellitenbild ein/aus">Satellit</button>
        <span class="sep"></span>
        <button id="orAuto" class="on" title="Feld-Längsachse horizontal (Ausrichtung des Platzes)">Feld</button>
        <button id="orNorth" title="Norden oben – zum Überprüfen der Drehung">Norden oben</button>
        <span class="sep"></span>
        <button id="rotM" title="1° gegen den Uhrzeigersinn (Shift: 10°)">⟲ 1°</button>
        <button id="rotP" title="1° im Uhrzeigersinn (Shift: 10°)">⟳ 1°</button>
        <button id="rot90" title="Um 90° drehen">90°</button>
        <button id="rotReset" title="Auf die hinterlegte Platz-Ausrichtung zurücksetzen">Reset</button>
        <span id="angle">–</span>
      </div>
      <div id="mapNote">Satellitenbild konnte nicht geladen werden (offline?)</div>
    </div>
    <aside>
      <div class="card">
        <div class="row" style="margin-bottom:6px"><span id="clockSmall">–</span><span id="zone" class="zone">–</span></div>
        <div><span class="big" id="speed">0.0</span><span class="unit">km/h</span></div>
        <div class="row" style="margin-top:8px"><span>Herzfrequenz</span><span id="hr">–</span></div>
        <div class="row"><span>Beschleunigung</span><span id="acc">–</span></div>
      </div>
      <div class="card">
        <div class="hint" style="margin-bottom:8px">Im Intervall bis jetzt</div>
        <div class="stats">
          <div class="stat"><div class="v" id="sDist">0</div><div class="k">Distanz (m)</div></div>
          <div class="stat"><div class="v" id="sTop">0.0</div><div class="k">Top-Speed (km/h)</div></div>
          <div class="stat"><div class="v" id="sAcc">0</div><div class="k">Antritte (hoch)</div></div>
          <div class="stat"><div class="v" id="sRun">0</div><div class="k">Läufe ≥ 60 %</div></div>
        </div>
      </div>
      <div class="card" style="flex:1;display:flex;flex-direction:column;min-height:0;padding:6px">
        <div class="hint" style="padding:6px 6px 2px">Antritte im Intervall · Klick springt hin</div>
        <div id="events"></div>
      </div>
    </aside>
  </main>

  <footer>
    <div class="controls">
      <button id="play" style="min-width:90px">▶ Play</button>
      <div class="group"><span class="lbl">Tempo</span>
        <button data-rate="1">1×</button>
        <button data-rate="5">5×</button>
        <button data-rate="10" class="on">10×</button>
        <button data-rate="30">30×</button>
        <button data-rate="60">60×</button>
      </div>
      <span id="clock">–</span>
      <div class="grow" style="flex:1"></div>
      <button id="rec" title="Nimmt das gewählte Intervall mit dem aktuellen Tempo als .webm auf">● Als Video speichern</button>
    </div>
    <canvas id="timeline"></canvas>
    <div class="hint">Timeline: <b>ziehen</b> = Intervall wählen · <b>klicken</b> = zu Zeitpunkt springen · Leertaste = Play/Pause · ←/→ = ±5 s</div>
  </footer>
</div>

<script>
const DATA = __DATA__;
const ZONE_COLORS = ["#5b7a8c","#4caf7a","#d8d04a","#ff9f2e","#ff4d3d"];
const ZONE_NAMES  = {walking:"Gehen",jogging:"Joggen",running:"Laufen",high_speed:"High Speed",sprint:"Sprint"};

const $ = id => document.getElementById(id);
const field = $("field"), fctx = field.getContext("2d");
const tl = $("timeline"), tctx = tl.getContext("2d");

let S = null;               // aktuelle Session
let t0 = 0, t1 = 0, cur = 0;
let playing = false, rate = 10, trail = 30;
let lastFrame = null, recorder = null;
let view = null;            // Feld-Transformation fürs Intervall
let rot = 0;                // Anzeige-Drehung (rad, gegen Uhrzeigersinn = Norden wandert nach links)
let mapOn = true, northUp = false;
const tiles = new Map(); let tileFails = 0, tileOk = 0, drawPending = false;

// ---------- Hilfen ----------
function fmtClock(sec){ const s=Math.round(S.startSod+sec);
  const h=Math.floor(s/3600)%24, m=Math.floor(s/60)%60, ss=s%60;
  return `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(ss).padStart(2,"0")}`; }
function fmtDur(sec){ sec=Math.max(0,Math.round(sec)); const m=Math.floor(sec/60), s=sec%60;
  return `${m}:${String(s).padStart(2,"0")}`; }
function idxAt(t){ const a=S.t; let lo=0, hi=a.length-1;
  if(t<=a[0]) return 0; if(t>=a[hi]) return hi;
  while(hi-lo>1){ const m=(lo+hi)>>1; if(a[m]<=t) lo=m; else hi=m; } return lo; }
function lerp(a,b,f){ return a+(b-a)*f; }
function stateAt(t){
  const i=idxAt(t), j=Math.min(i+1,S.t.length-1);
  const same = S.seg[i]===S.seg[j] && j>i;
  const f = same ? (t-S.t[i])/(S.t[j]-S.t[i]||1) : 0;
  return { i, x:lerp(S.x[i],S.x[j],same?f:0), y:lerp(S.y[i],S.y[j],same?f:0),
           v:lerp(S.v[i],S.v[j],same?f:0), hr:S.hr[i], zone:S.zone[i] };
}
function speedColor(v){            // v in m/s relativ zu v_max → Farbe
  const p = Math.min(1, v/DATA.vmax);
  const stops=[[0,[70,110,140]],[.3,[76,175,122]],[.45,[216,208,74]],[.6,[255,159,46]],[.8,[255,77,61]],[1,[255,255,255]]];
  for(let k=1;k<stops.length;k++){ if(p<=stops[k][0]){
    const [pa,ca]=stops[k-1],[pb,cb]=stops[k], f=(p-pa)/(pb-pa);
    return `rgb(${ca.map((c,n)=>Math.round(c+(cb[n]-c)*f)).join(",")})`; } }
  return "#fff";
}

// ---------- Session / Intervall ----------
function rotKey(){ return "replayRot:"+S.label; }
function loadSession(k){
  S = DATA.sessions[k];
  let saved=null; try{ saved=localStorage.getItem(rotKey()); }catch(e){}
  rot = saved!==null && !isNaN(+saved) ? +saved : S.ang;
  northUp=false; syncOrientButtons();
  setInterval_(0, S.t[S.t.length-1]);
  // Gametime: bei Trainings > 75 min standardmäßig die letzte Stunde
  if (S.t[S.t.length-1] > 75*60) preset("last60");
}
function setInterval_(a,b){
  t0=Math.max(0,Math.min(a,b)); t1=Math.min(S.t[S.t.length-1],Math.max(a,b));
  if(t1-t0<5) t1=Math.min(S.t[S.t.length-1],t0+5);
  cur=t0; computeView(); buildEvents(); draw();
}
function preset(p){
  const end=S.t[S.t.length-1];
  if(p==="all") setInterval_(0,end);
  if(p==="last60") setInterval_(Math.max(0,end-3600),end);
  if(p==="last30") setInterval_(Math.max(0,end-1800),end);
}
// Lokale Meter (Ost/Nord) → Anzeige-Koordinaten (gedreht)
function curRot(){ return northUp ? 0 : rot; }
function disp(x,y){ const r=-curRot(), c=Math.cos(r), s=Math.sin(r); return [x*c-y*s, x*s+y*c]; }
function computeView(){
  const i0=idxAt(t0), i1=idxAt(t1); const xs=[], ys=[];
  for(let i=i0;i<=i1;i++){ if(S.x[i]!=null){ const d=disp(S.x[i],S.y[i]); xs.push(d[0]); ys.push(d[1]); } }
  if(!xs.length){ view={cx:0,cy:0,w:50,h:30}; return; }
  const q=(a,p)=>{ const s=[...a].sort((m,n)=>m-n); return s[Math.floor((s.length-1)*p)]; };
  const x0=q(xs,.005), x1=q(xs,.995), y0=q(ys,.005), y1=q(ys,.995);
  view={cx:(x0+x1)/2, cy:(y0+y1)/2, w:Math.max(20,x1-x0)*1.12, h:Math.max(12,y1-y0)*1.12};
}

// ---------- Ereignisliste ----------
function buildEvents(){
  const box=$("events"); box.innerHTML="";
  const ev=S.accels.filter(a=>a[1]>=t0 && a[0]<=t1);
  if(!ev.length){ box.innerHTML='<div class="hint" style="padding:6px">Keine Antritte im Intervall.</div>'; return; }
  for(const a of ev){
    const d=document.createElement("div"); d.className="ev"; d.dataset.t=a[0];
    d.innerHTML=`<span class="tri ${a[5]?"":"mod"}"></span><span>${fmtClock(a[0])}</span>`+
      `<span style="color:var(--muted)">${a[3].toFixed(0)}→${a[4].toFixed(0)} km/h</span>`+
      `<span style="margin-left:auto;font-weight:600">${a[2].toFixed(1)} m/s²</span>`;
    d.onclick=()=>{ cur=Math.max(t0,a[0]-3); draw(); };
    box.appendChild(d);
  }
}

// ---------- Zeichnen: Feld ----------
function resize(){
  const dpr=window.devicePixelRatio||1;
  for(const c of [field,tl]){ const r=c.getBoundingClientRect();
    c.width=Math.max(1,Math.round(r.width*dpr)); c.height=Math.max(1,Math.round(r.height*dpr)); }
  draw();
}
function drawField(ctx,W,H,hud){
  ctx.fillStyle="#183326"; ctx.fillRect(0,0,W,H);
  const sc=Math.min(W/view.w, H/view.h);
  const X=x=>W/2+(x-view.cx)*sc, Y=y=>H/2-(y-view.cy)*sc;     // Anzeige-Koord. → Pixel
  const P=(x,y)=>{ const d=disp(x,y); return [X(d[0]),Y(d[1])]; }; // lokale Meter → Pixel

  if(mapOn) drawTiles(ctx,W,H,sc,P);

  // Raster alle 10 m (Yard-Linien-Optik) – liegt parallel zur Feldausrichtung
  ctx.strokeStyle=mapOn?"rgba(255,255,255,.18)":"rgba(255,255,255,.08)"; ctx.lineWidth=1;
  const gx0=Math.floor((view.cx-W/sc/2)/10)*10, gx1=view.cx+W/sc/2;
  for(let g=gx0; g<=gx1; g+=10){ ctx.beginPath(); ctx.moveTo(X(g),0); ctx.lineTo(X(g),H); ctx.stroke(); }
  const gy0=Math.floor((view.cy-H/sc/2)/10)*10, gy1=view.cy+H/sc/2;
  for(let g=gy0; g<=gy1; g+=10){ ctx.beginPath(); ctx.moveTo(0,Y(g)); ctx.lineTo(W,Y(g)); ctx.stroke(); }

  const i0=idxAt(t0), ic=idxAt(cur);
  // Geisterpfad: gesamtes Intervall
  ctx.strokeStyle=mapOn?"rgba(255,255,255,.22)":"rgba(255,255,255,.07)"; ctx.lineWidth=1.2*(W/1000+.5);
  ctx.beginPath(); let pen=false;
  for(let i=i0;i<=idxAt(t1);i++){
    if(S.x[i]==null){pen=false;continue;}
    const p=P(S.x[i],S.y[i]);
    if(!pen || S.seg[i]!==S.seg[i-1]){ ctx.moveTo(p[0],p[1]); pen=true; }
    else ctx.lineTo(p[0],p[1]);
  }
  ctx.stroke();

  // Spur bis jetzt, nach Speed eingefärbt
  const ts = trail>0 ? Math.max(t0,cur-trail) : t0;
  const is=idxAt(ts); const lw=Math.max(3, W/220);
  ctx.lineCap="round";
  const now=stateAt(cur);
  for(let i=Math.max(is,i0)+1;i<=ic+1 && i<S.t.length;i++){
    if(S.seg[i]!==S.seg[i-1]||S.x[i]==null||S.x[i-1]==null) continue;
    // letztes Stück nur bis zur interpolierten aktuellen Position
    const end = i===ic+1 ? now : {x:S.x[i],y:S.y[i]};
    const age = trail>0 ? (cur-S.t[i-1])/trail : 0;
    ctx.globalAlpha = trail>0 ? Math.max(.15,1-age) : .9;
    ctx.strokeStyle=speedColor(S.v[i]);
    ctx.lineWidth = lw*(S.spr[i]?1.8:1);
    const pa=P(S.x[i-1],S.y[i-1]), pb=P(end.x,end.y);
    if(mapOn){ ctx.strokeStyle="rgba(0,0,0,.55)"; ctx.lineWidth=lw*(S.spr[i]?1.8:1)+2.5;
      ctx.beginPath(); ctx.moveTo(pa[0],pa[1]); ctx.lineTo(pb[0],pb[1]); ctx.stroke();
      ctx.strokeStyle=speedColor(S.v[i]); ctx.lineWidth=lw*(S.spr[i]?1.8:1); }
    ctx.beginPath(); ctx.moveTo(pa[0],pa[1]); ctx.lineTo(pb[0],pb[1]); ctx.stroke();
  }
  ctx.globalAlpha=1;

  // Antritte als Dreiecke (bereits passiert, im Spurfenster bzw. Intervall)
  for(const a of S.accels){
    if(a[1]<ts || a[1]>cur || a[1]<t0) continue;
    const k=idxAt(a[1]); if(S.x[k]==null) continue;
    const [px,py]=P(S.x[k],S.y[k]), r=(a[5]?9:6)*(W/1000+.5);
    ctx.fillStyle=a[5]?"#ff4d3d":"#ff9a8f"; ctx.strokeStyle="#111"; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(px,py-r); ctx.lineTo(px+r*.87,py+r*.5); ctx.lineTo(px-r*.87,py+r*.5); ctx.closePath();
    ctx.fill(); ctx.stroke();
  }

  // Aktuelle Position
  if(now.x!=null && !isNaN(now.x)){
    const [px,py]=P(now.x,now.y), r=Math.max(6,W/120);
    ctx.fillStyle="rgba(255,204,51,.22)"; ctx.beginPath(); ctx.arc(px,py,r*2.2,0,7); ctx.fill();
    ctx.fillStyle="#ffcc33"; ctx.strokeStyle="#111"; ctx.lineWidth=2;
    ctx.beginPath(); ctx.arc(px,py,r,0,7); ctx.fill(); ctx.stroke();
  }

  // Maßstab
  const m10=10*sc, pad=14*(W/1000+.6);
  ctx.strokeStyle="rgba(255,255,255,.6)"; ctx.lineWidth=2; ctx.beginPath();
  ctx.moveTo(pad,H-pad); ctx.lineTo(pad+m10,H-pad); ctx.moveTo(pad,H-pad-5); ctx.lineTo(pad,H-pad+5);
  ctx.moveTo(pad+m10,H-pad-5); ctx.lineTo(pad+m10,H-pad+5); ctx.stroke();
  ctx.fillStyle="rgba(255,255,255,.7)"; ctx.font=`${Math.round(12*(W/1000+.6))}px system-ui`;
  ctx.fillText("10 m", pad, H-pad-9);

  // Nordpfeil
  { const fsz=W/1000+.6, cx=W-34*fsz, cy=(hud?70:34)*fsz, L=18*fsz;
    const n=disp(0,1), dx=n[0], dy=-n[1];
    ctx.save(); ctx.translate(cx,cy);
    ctx.fillStyle="rgba(0,0,0,.45)"; ctx.beginPath(); ctx.arc(0,0,L*1.35,0,7); ctx.fill();
    ctx.strokeStyle="#fff"; ctx.fillStyle="#ff4d3d"; ctx.lineWidth=2;
    ctx.beginPath(); ctx.moveTo(-dx*L*.7,-dy*L*.7); ctx.lineTo(dx*L,dy*L); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(dx*L,dy*L); ctx.lineTo(dx*L*.45-dy*L*.3,dy*L*.45+dx*L*.3);
    ctx.lineTo(dx*L*.45+dy*L*.3,dy*L*.45-dx*L*.3); ctx.closePath(); ctx.fill();
    ctx.fillStyle="#fff"; ctx.font=`600 ${Math.round(11*fsz)}px system-ui`; ctx.textAlign="center"; ctx.textBaseline="middle";
    ctx.fillText("N", -dx*L*1.05, -dy*L*1.05); ctx.restore(); }

  if(mapOn){ const fsz=Math.round(10*(W/1000+.6)); ctx.font=`${fsz}px system-ui`;
    const tx="Tiles © Esri — Maxar, Earthstar Geographics"; const tw=ctx.measureText(tx).width;
    ctx.fillStyle="rgba(0,0,0,.5)"; ctx.fillRect(W-tw-12,H-fsz-10,tw+12,fsz+10);
    ctx.fillStyle="rgba(255,255,255,.8)"; ctx.fillText(tx,W-tw-6,H-7); }

  // HUD (fürs Video)
  if(hud){
    const fs=Math.round(18*(W/1000+.5));
    ctx.fillStyle="rgba(0,0,0,.45)"; ctx.fillRect(0,0,W,fs*2.4);
    ctx.fillStyle="#fff"; ctx.font=`600 ${fs}px system-ui`;
    ctx.fillText(`${S.label} · ${S.date} · ${fmtClock(cur)}`, fs*.8, fs*1.55);
    const txt=`${(now.v*3.6).toFixed(1)} km/h` + (now.hr?`  ·  ${now.hr} bpm`:"");
    const w=ctx.measureText(txt).width; ctx.fillStyle="#ffcc33"; ctx.fillText(txt, W-w-fs*.8, fs*1.55);
  }
}

// ---------- Satellitenbild (Esri-Kacheln, mitgedreht) ----------
const D2R=Math.PI/180;
function lon2tx(lon,z){ return (lon+180)/360*2**z; }
function lat2ty(lat,z){ const r=lat*D2R; return (1-Math.log(Math.tan(r)+1/Math.cos(r))/Math.PI)/2*2**z; }
function tx2lon(x,z){ return x/2**z*360-180; }
function ty2lat(y,z){ const n=Math.PI-2*Math.PI*y/2**z; return Math.atan(Math.sinh(n))/D2R; }
function requestDraw(){ if(drawPending||playing) return; drawPending=true; requestAnimationFrame(()=>{ drawPending=false; draw(); }); }
function getTile(z,x,y){
  const k=`${z}/${x}/${y}`; let t=tiles.get(k);
  if(!t){ const img=new Image(); img.crossOrigin="anonymous";
    t={img,ok:false}; tiles.set(k,t);
    img.onload=()=>{ t.ok=true; tileOk++; $("mapNote").style.display="none"; requestDraw(); };
    img.onerror=()=>{ tileFails++; if(!tileOk && tileFails>3) $("mapNote").style.display="block"; };
    img.src=DATA.tileUrl.replace("{z}",z).replace("{x}",x).replace("{y}",y); }
  return t;
}
function drawTiles(ctx,W,H,sc,P){
  const kx=Math.cos(S.lat0*D2R)*111320, ky=110540;
  const toLocal=(lat,lon)=>[(lon-S.lon0)*kx,(lat-S.lat0)*ky];
  // Bildschirmecken → lokale Meter → Lat/Lon (für den Kachelbereich)
  const r=curRot(), c=Math.cos(r), s=Math.sin(r);
  const inv=(px,py)=>{ const dx=(px-W/2)/sc+view.cx, dy=-(py-H/2)/sc+view.cy; return [dx*c-dy*s, dx*s+dy*c]; };
  let la0=90,la1=-90,lo0=180,lo1=-180;
  for(const [px,py] of [[0,0],[W,0],[0,H],[W,H]]){ const [x,y]=inv(px,py);
    const lat=S.lat0+y/ky, lon=S.lon0+x/kx; la0=Math.min(la0,lat); la1=Math.max(la1,lat); lo0=Math.min(lo0,lon); lo1=Math.max(lo1,lon); }
  // Zoom so wählen, dass 1 Kachelpixel ≈ 1 Bildschirmpixel
  const z=Math.max(3,Math.min(19,Math.ceil(Math.log2(156543.03*Math.cos(S.lat0*D2R)*sc))));
  const x0=Math.floor(lon2tx(lo0,z)), x1=Math.floor(lon2tx(lo1,z));
  const y0=Math.floor(lat2ty(la1,z)), y1=Math.floor(lat2ty(la0,z));
  if((x1-x0+1)*(y1-y0+1)>200) return;
  // Affine Abbildung lokale Meter → Pixel (exakt, da Drehung + Skalierung)
  const O=P(0,0), E=P(1,0), N=P(0,1); const ex=[E[0]-O[0],E[1]-O[1]], ey=[N[0]-O[0],N[1]-O[1]];
  for(let ty=y0; ty<=y1; ty++) for(let tx=x0; tx<=x1; tx++){
    const t=getTile(z,tx,ty); if(!t.ok) continue;
    const [xw,yn]=toLocal(ty2lat(ty,z),tx2lon(tx,z)), [xe,ys]=toLocal(ty2lat(ty+1,z),tx2lon(tx+1,z));
    const mx=(xe-xw)/256, my=(yn-ys)/256;
    const sx=O[0]+ex[0]*xw+ey[0]*yn, sy=O[1]+ex[1]*xw+ey[1]*yn;
    ctx.setTransform(ex[0]*mx, ex[1]*mx, -ey[0]*my, -ey[1]*my, sx, sy);
    ctx.drawImage(t.img,0,0,256.6,256.6);    // leichte Überlappung gegen Nähte
  }
  ctx.setTransform(1,0,0,1,0,0);
  ctx.fillStyle="rgba(0,0,0,.18)"; ctx.fillRect(0,0,W,H);   // leicht abdunkeln → Spur bleibt lesbar
}

// ---------- Zeichnen: Timeline ----------
function drawTimeline(){
  const W=tl.width, H=tl.height, end=S.t[S.t.length-1];
  const X=t=>t/end*W;
  tctx.fillStyle="#111815"; tctx.fillRect(0,0,W,H);
  // Intervall
  tctx.fillStyle="rgba(255,204,51,.10)"; tctx.fillRect(X(t0),0,X(t1)-X(t0),H);
  // Speed als Balken nach Zone
  const vmax=Math.max(DATA.vmax, ...S.v.slice(0,1));
  const step=Math.max(1,Math.floor(S.t.length/W));
  for(let i=0;i<S.t.length;i+=step){
    let v=0, z=0; for(let k=i;k<Math.min(i+step,S.t.length);k++){ if(S.v[k]>v){v=S.v[k]; z=S.zone[k];} }
    const h=Math.min(1,v/vmax)*(H-14);
    const inside=S.t[i]>=t0&&S.t[i]<=t1;
    tctx.fillStyle=ZONE_COLORS[z]; tctx.globalAlpha=inside?.95:.35;
    tctx.fillRect(X(S.t[i]),H-h,Math.max(1,W/S.t.length*step),h);
  }
  tctx.globalAlpha=1;
  // Antritte
  for(const a of S.accels){ tctx.fillStyle=a[5]?"#ff4d3d":"#ff9a8f"; tctx.fillRect(X(a[0]),0,Math.max(1.5,devicePixelRatio),6*devicePixelRatio); }
  // Intervallgrenzen + Playhead
  tctx.fillStyle="#ffcc33"; tctx.fillRect(X(t0)-1,0,2,H); tctx.fillRect(X(t1)-1,0,2,H);
  tctx.fillStyle="#fff"; tctx.fillRect(X(cur)-1.5,0,3,H);
  // Uhrzeiten
  tctx.fillStyle="rgba(255,255,255,.55)"; tctx.font=`${11*devicePixelRatio}px system-ui`;
  const every = end>5400?900:end>1800?600:300;
  for(let s=0;s<=end;s+=every){ tctx.fillText(fmtClock(s).slice(0,5), X(s)+3, 18*devicePixelRatio); }
}

// ---------- Panel ----------
function updatePanel(){
  const st=stateAt(cur);
  $("speed").textContent=(st.v*3.6).toFixed(1);
  $("hr").textContent=st.hr?`${st.hr} bpm`:"–";
  // Zone passend zur angezeigten (rohen) Geschwindigkeit
  let zi=0; DATA.zoneBounds.forEach((lo,k)=>{ if(st.v>=lo*DATA.vmax) zi=k; });
  const z=DATA.zones[zi]; const ze=$("zone");
  ze.textContent=ZONE_NAMES[z]||z; ze.style.background=ZONE_COLORS[zi];
  const i=st.i; const a = i>0 && S.seg[i]===S.seg[i-1] ? (S.v[i]-S.v[i-1])/((S.t[i]-S.t[i-1])||1) : 0;
  $("acc").textContent=`${a.toFixed(1)} m/s²`;
  $("clockSmall").textContent=`${S.date} · ${fmtClock(cur)}`;
  $("clock").textContent=`${fmtClock(cur)}  ·  ${fmtDur(cur-t0)} / ${fmtDur(t1-t0)}`;

  // Intervall-Statistik bis jetzt
  const i0=idxAt(t0); let dist=0, top=0;
  for(let k=i0+1;k<=i;k++){ if(S.seg[k]===S.seg[k-1]) dist+=S.v[k]*(S.t[k]-S.t[k-1]); if(S.v[k]>top) top=S.v[k]; }
  $("sDist").textContent=Math.round(dist);
  $("sTop").textContent=(top*3.6).toFixed(1);
  const acc=S.accels.filter(e=>e[0]>=t0&&e[1]<=cur);
  $("sAcc").textContent=`${acc.length} (${acc.filter(e=>e[5]).length})`;
  $("sRun").textContent=S.sprints.filter(e=>e[0]>=t0&&e[0]<=cur).length;
  for(const d of $("events").children){ if(d.dataset.t!==undefined) d.classList.toggle("past", +d.dataset.t<=cur); }
}

function draw(){
  if(!S||!view) return;
  drawField(fctx, field.width, field.height, !!recorder);
  drawTimeline(); updatePanel();
}

// ---------- Abspielen ----------
function setPlaying(p){
  playing=p; $("play").textContent=p?"❚❚ Pause":"▶ Play"; lastFrame=null;
  if(p){ if(cur>=t1) cur=t0; requestAnimationFrame(tick); }
}
function tick(ts){
  if(!playing) return;
  if(lastFrame!=null){
    cur+= (ts-lastFrame)/1000*rate;
    // Aufzeichnungslücken überspringen
    const i=idxAt(cur); if(i+1<S.t.length && S.seg[i+1]!==S.seg[i] && cur>S.t[i]+1) cur=S.t[i+1];
    if(cur>=t1){ cur=t1; draw(); setPlaying(false); if(recorder) stopRec(); return; }
  }
  lastFrame=ts; draw(); requestAnimationFrame(tick);
}

// ---------- Video-Aufnahme ----------
function startRec(){
  if(!field.captureStream||!window.MediaRecorder){ alert("Video-Aufnahme wird von diesem Browser nicht unterstützt (Chrome/Edge/Firefox nutzen)."); return; }
  const stream=field.captureStream(30);
  const mime=["video/webm;codecs=vp9","video/webm;codecs=vp8","video/webm"].find(m=>MediaRecorder.isTypeSupported(m));
  const chunks=[]; recorder=new MediaRecorder(stream,{mimeType:mime, videoBitsPerSecond:6e6});
  recorder.ondataavailable=e=>{ if(e.data.size) chunks.push(e.data); };
  recorder.onstop=()=>{
    const blob=new Blob(chunks,{type:"video/webm"}); const a=document.createElement("a");
    a.href=URL.createObjectURL(blob);
    a.download=`replay_${S.label.replace(/[\/\\]/g,"_")}_${fmtClock(t0).replace(/:/g,"")}-${fmtClock(t1).replace(/:/g,"")}_${rate}x.webm`;
    a.click(); setTimeout(()=>URL.revokeObjectURL(a.href),5000);
  };
  $("rec").classList.add("recording"); $("rec").textContent="■ Aufnahme stoppen";
  cur=t0; recorder.start(); setPlaying(true);
}
function stopRec(){
  if(!recorder) return; const r=recorder; recorder=null; r.stop();
  $("rec").classList.remove("recording"); $("rec").textContent="● Als Video speichern"; draw();
}

// ---------- Interaktion ----------
function tlTime(e){ const r=tl.getBoundingClientRect(); return Math.max(0,Math.min(1,(e.clientX-r.left)/r.width))*S.t[S.t.length-1]; }
let drag=null;
tl.addEventListener("pointerdown",e=>{ tl.setPointerCapture(e.pointerId); drag={start:tlTime(e), x:e.clientX, moved:false}; });
tl.addEventListener("pointermove",e=>{ if(!drag) return;
  if(Math.abs(e.clientX-drag.x)>4) drag.moved=true;
  if(drag.moved){ t0=Math.min(drag.start,tlTime(e)); t1=Math.max(drag.start,tlTime(e)); cur=t0; drawTimeline(); }
});
tl.addEventListener("pointerup",e=>{ if(!drag) return;
  if(drag.moved) setInterval_(t0,t1);
  else { const t=tlTime(e); if(t<t0||t>t1) setInterval_(0,S.t[S.t.length-1]); cur=Math.max(t0,Math.min(t1,t)); draw(); }
  drag=null;
});
function syncOrientButtons(){
  $("orAuto").classList.toggle("on",!northUp); $("orNorth").classList.toggle("on",northUp);
  const deg=v=>((Math.round(v/D2R)%360)+360)%360;
  $("angle").textContent = northUp ? "Norden oben"
    : `Achse ${deg(rot)}°` + (S&&S.field ? ` · ${S.field} ${deg(S.ang)}°` : " · Platz unbekannt");
  for(const id of ["rotM","rotP","rot90","rotReset"]) $(id).disabled=northUp;
}
function setRot(r){ rot=r; try{ localStorage.setItem(rotKey(), String(rot)); }catch(e){}
  syncOrientButtons(); computeView(); draw(); }
$("mapBtn").onclick=()=>{ mapOn=!mapOn; $("mapBtn").classList.toggle("on",mapOn); draw(); };
$("orAuto").onclick=()=>{ northUp=false; syncOrientButtons(); computeView(); draw(); };
$("orNorth").onclick=()=>{ northUp=true; syncOrientButtons(); computeView(); draw(); };
// disp() dreht um −rot: größeres rot ⇒ Inhalt dreht im Uhrzeigersinn
$("rotM").onclick=e=>setRot(rot-(e.shiftKey?10:1)*D2R);
$("rotP").onclick=e=>setRot(rot+(e.shiftKey?10:1)*D2R);
$("rot90").onclick=()=>setRot(rot+90*D2R);
$("rotReset").onclick=()=>{ try{ localStorage.removeItem(rotKey()); }catch(e){} rot=S.ang; syncOrientButtons(); computeView(); draw(); };
$("play").onclick=()=>setPlaying(!playing);
$("rec").onclick=()=>recorder?stopRec():startRec();
document.querySelectorAll("[data-rate]").forEach(b=>b.onclick=()=>{
  rate=+b.dataset.rate; document.querySelectorAll("[data-rate]").forEach(x=>x.classList.toggle("on",x===b)); });
document.querySelectorAll("[data-trail]").forEach(b=>b.onclick=()=>{
  trail=+b.dataset.trail; document.querySelectorAll("[data-trail]").forEach(x=>x.classList.toggle("on",x===b)); draw(); });
document.querySelectorAll("[data-preset]").forEach(b=>b.onclick=()=>preset(b.dataset.preset));
document.addEventListener("keydown",e=>{
  if(e.target.tagName==="SELECT") return;
  if(e.code==="Space"){ e.preventDefault(); setPlaying(!playing); }
  if(e.code==="ArrowRight"){ cur=Math.min(t1,cur+5); draw(); }
  if(e.code==="ArrowLeft"){ cur=Math.max(t0,cur-5); draw(); }
});
const sel=$("sess");
DATA.sessions.forEach((s,k)=>{ const o=document.createElement("option"); o.value=k; o.textContent=`${s.label}  (${s.date})`; sel.appendChild(o); });
sel.onchange=()=>{ setPlaying(false); loadSession(+sel.value); };
window.addEventListener("resize",resize);

loadSession(DATA.sessions.length-1); sel.value=DATA.sessions.length-1;
resize();
</script>
</body>
</html>
"""


# ============================================================
# QUICK-START
# ============================================================

if __name__ == "__main__":
    warnings.simplefilter("ignore")
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(project_root, "data")

    if os.path.isfile(target):
        files = [target]
    elif os.path.isdir(target):
        files = sorted(glob.glob(os.path.join(target, "**", "*.fit"), recursive=True))
    else:
        print(f"Pfad nicht gefunden: {target}")
        sys.exit(1)
    if not files:
        print(f"Keine .fit Dateien gefunden in: {target}")
        sys.exit(1)

    # v_max immer über ALLE Sessions im data/-Ordner → gleiche Farbskala überall
    all_files = sorted(glob.glob(os.path.join(project_root, "data", "**", "*.fit"), recursive=True)) or files
    with contextlib.redirect_stdout(io.StringIO()):
        v_max = personal_max_speed(load_multiple(all_files, verbose=False))
    print(f"v_max (alle Sessions): {v_max:.2f} m/s ({v_max*3.6:.1f} km/h)\n")

    # Sessions nach Datum sortieren (Dateiname beginnt mit JJ-MM-TT)
    files = sorted(files, key=lambda f: os.path.basename(f))

    out_file = os.path.join(project_root, "output", "replay.html")
    build_replay(files, out_file, v_max)

    import webbrowser, pathlib
    webbrowser.open(pathlib.Path(out_file).resolve().as_uri())
