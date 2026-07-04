#!/usr/bin/env python3
"""
horizon_from_pv.py



Tuning: diffuse_fraction: 0.18 ist der Restanteil bei verschattetem Direktstrahl. Wenn du nach ein paar klaren Tagen siehst, dass die maskierten Morgenstunden systematisch zu niedrig/hoch liegen, dort nachjustieren (typisch 0.10–0.25; bei Ost-/Westausrichtung der Module eher niedriger) und die Tabelle neu generieren. Die Konsole gibt aus, wie viele Stunden im Jahr überhaupt betroffen sind – Plausibilitätscheck: im Winter deutlich mehr als im Sommer.
Tagessummen-Hinweis: die Maske ändert die Stundenwerte, also auch die Summe. Falls deine pv_forecast_weighted-Sensoren (heute/morgen/übermorgen) die Tagessumme separat aus den Roh-APIs ziehen, solltest du sie stattdessen aus dem maskierten Profil aufsummieren, sonst passen Profil und Tagessumme nicht mehr zusammen.:


Derive the local horizon profile (terrain / building shading) from historical
PV production data stored in the Home Assistant recorder database.

Method
------
1. Read raw state history of the PV power sensor from the SQLite recorder DB
   (works with purge_keep_days=365; statistics tables are too coarse).
2. Resample onto a fixed 5-min grid (forward fill, stale values -> 0).
3. Per day, detect *sharp* power transitions: a window that is consistently
   LOW immediately followed by a window that is consistently HIGH (sun rises
   over a mountain ridge) or vice versa (sun drops behind a ridge).
   Slow ramps (clouds, flat-horizon sunrise) do not satisfy the condition.
4. Convert each edge timestamp to sun azimuth / elevation (astral).
5. Bin the points by azimuth, take the median elevation per bin,
   interpolate gaps, smooth with a rolling median.
6. Export:
     - horizon_points.csv   (all raw detected points, for inspection)
     - horizon_profile.csv  (binned azimuth/elevation profile)
     - forecast.solar "horizon" parameter string (printed to stdout)
     - horizon_plot.png     (optional, if matplotlib is installed)

Dependencies
------------
    pip install numpy astral            # required
    pip install matplotlib              # optional, for the plot

Notes
-----
- Run against a *copy* of home-assistant_v2.db, or rely on the read-only
  URI mode used below. For MariaDB, replace load_states() with a
  mysql-connector query (the SQL is identical).
- Sensor unit (W or kW) does not matter: everything is normalized per day.
- Azimuth coverage comes from the seasonal sweep of sunrise/sunset azimuths,
  plus any midday shading in winter (low sun). Sectors where the sun never
  appears (north) are filled with 0.
"""

import csv
import datetime as dt
import math
import sqlite3
import sys

import numpy as np
from astral import Observer
from astral.sun import azimuth as sun_azimuth, elevation as sun_elevation

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIG = {
    # --- data source ---
    "backend": "mysql",                         # "mysql" or "sqlite"
    # sqlite:
    "db_path": "/config/home-assistant_v2.db",  # recorder DB (a copy is fine)
    # mysql / mariadb:
    "mysql": {
        "host": "192.168.0.12",
        "port": 3306,
        "user": "homeassistant",
        "password": "homeassistant",
        "database": "homeassistant",
    },
    "entity_id": "sensor.zwicknagl_martin_leistung_ac", # Roof power sensor
#    "entity_id": "sensor.pv_production_balcony_power",  # Balcony power sensor
#    "entity_id": "sensor.pv_production_power",          # Both together

    "days_back": 365,

    # --- site ---
    "latitude": 47.49,        # Woergl
    "longitude": 12.07,

    # --- resampling ---
    "step_s": 300,            # grid resolution (s)
    "max_stale_s": 7200,      # no state update for this long -> treat as 0
                              # (handles sensors that go 'unavailable' at night)

    # --- edge detection (all windows in grid samples, 1 sample = step_s) ---
    "pre_window": 4,          # x5 min that must be LOW before a rising edge
    "post_window": 4,         # x5 min that must be HIGH after it
    "gap": 4,                 # transition zone (15 min) between the windows
    "low_frac": 0.10,         # LOW  = below 20 % of the day's reference max
    "high_frac": 0.20,        # HIGH = above 35 % of the day's reference max
    "min_day_max": 3000.0,     # skip days with day max below this (sensor units)
    "min_elevation": 0.0,     # plausibility range for horizon points (deg)
    "max_elevation": 45.0,

    # --- horizon assembly ---
    "az_bin_deg": 2.0,        # azimuth bin width (deg)
    "min_points_per_bin": 2,  # bins with fewer points are interpolated
    "smooth_bins": 3,         # rolling-median window (bins), odd number

    # --- forecast.solar export ---
    "fs_values": 36,          # values in the horizon string (must divide 360)

    # --- output files ---
    "out_points_csv": "horizon_points_roof.csv",
    "out_profile_csv": "horizon_profile_roof.csv",
    "out_plot_png": "horizon_plot_roof.png",   # set to None to skip the plot
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
QUERY_NEW = """
    SELECT s.last_updated_ts, s.state
    FROM states s
    JOIN states_meta m ON s.metadata_id = m.metadata_id
    WHERE m.entity_id = {ph} AND s.last_updated_ts >= {ph}
    ORDER BY s.last_updated_ts
"""
QUERY_LEGACY = """
    SELECT last_updated_ts, state
    FROM states
    WHERE entity_id = {ph} AND last_updated_ts >= {ph}
    ORDER BY last_updated_ts
"""


def load_states(cfg):
    """Return (timestamps_epoch, values) for the configured entity."""
    since = (dt.datetime.now(dt.timezone.utc)
             - dt.timedelta(days=cfg["days_back"])).timestamp()
    params = (cfg["entity_id"], since)

    if cfg["backend"] == "mysql":
        import pymysql  # pip install pymysql
        con = pymysql.connect(**cfg["mysql"], charset="utf8mb4")
        cur = con.cursor()
        try:
            cur.execute(QUERY_NEW.format(ph="%s"), params)
        except pymysql.err.ProgrammingError:
            cur.execute(QUERY_LEGACY.format(ph="%s"), params)
        rows = cur.fetchall()
        con.close()
    else:
        con = sqlite3.connect(f"file:{cfg['db_path']}?mode=ro", uri=True)
        try:
            rows = con.execute(QUERY_NEW.format(ph="?"), params).fetchall()
        except sqlite3.OperationalError:
            rows = con.execute(QUERY_LEGACY.format(ph="?"), params).fetchall()
        con.close()

    ts, val = [], []
    for t, s in rows:
        try:
            v = float(s)
        except (TypeError, ValueError):
            continue  # 'unavailable' / 'unknown'
        if math.isfinite(v) and v >= 0.0:
            ts.append(float(t))
            val.append(v)

    if len(ts) < 100:
        sys.exit(f"Not enough usable states for {cfg['entity_id']} "
                 f"({len(ts)} rows). Check db_path / entity_id.")
    print(f"Loaded {len(ts)} states "
          f"({dt.datetime.fromtimestamp(ts[0]):%Y-%m-%d} .. "
          f"{dt.datetime.fromtimestamp(ts[-1]):%Y-%m-%d})")
    return np.asarray(ts), np.asarray(val)


def resample(ts, val, cfg):
    """Forward-fill onto a fixed grid; values older than max_stale_s -> 0."""
    step = cfg["step_s"]
    t0 = math.floor(ts[0] / step) * step
    t1 = math.ceil(ts[-1] / step) * step
    grid = np.arange(t0, t1 + step, step, dtype=float)

    idx = np.searchsorted(ts, grid, side="right") - 1
    safe = np.clip(idx, 0, None)
    v = val[safe]
    age = grid - ts[safe]
    v = np.where((idx < 0) | (age > cfg["max_stale_s"]), 0.0, v)
    return grid, v


# ---------------------------------------------------------------------------
# Edge detection
# ---------------------------------------------------------------------------
def detect_edges_for_day(t, p, cfg):
    """Return list of (epoch, sign) edges for one day. sign +1 = sun appears."""
    if len(p) < cfg["pre_window"] + cfg["gap"] + cfg["post_window"] + 2:
        return []
    dmax = float(np.percentile(p, 99))  # robust against single spikes
    if dmax < cfg["min_day_max"]:
        return []
    lo = cfg["low_frac"] * dmax
    hi = cfg["high_frac"] * dmax
    pre, post, gap = cfg["pre_window"], cfg["post_window"], cfg["gap"]

    raw = []  # (index, sign)
    for i in range(pre, len(p) - gap - post):
        before = p[i - pre:i]
        after = p[i + gap:i + gap + post]
        if before.max() < lo and after.min() > hi:
            raw.append((i, +1))
        elif before.min() > hi and after.max() < lo:
            raw.append((i, -1))

    # collapse runs of consecutive detections into one edge each
    edges = []
    run = []
    run_sign = 0
    for i, sgn in raw + [(None, 0)]:           # sentinel flushes last run
        if run and (sgn != run_sign or i != run[-1] + 1):
            mid = run[len(run) // 2]
            t_edge = t[mid] + (gap / 2.0) * cfg["step_s"]
            edges.append((t_edge, run_sign))
            run = []
        if i is not None:
            if not run:
                run_sign = sgn
            run.append(i)
    return edges


def collect_points(grid, v, cfg):
    """Split grid into local days, detect edges, return horizon points."""
    obs = Observer(latitude=cfg["latitude"], longitude=cfg["longitude"])

    # group indices by local calendar day (local time from system tz is not
    # needed for the astronomy itself, only for the day split; UTC offset of
    # 1-2 h is irrelevant for splitting since nights are zero anyway)
    day_key = ((grid + 3600) // 86400).astype(int)   # ~CET day split
    points = []
    n_days = 0
    for d in np.unique(day_key):
        sel = day_key == d
        edges = detect_edges_for_day(grid[sel], v[sel], cfg)
        if edges:
            n_days += 1
        for t_edge, sgn in edges:
            when = dt.datetime.fromtimestamp(t_edge, dt.timezone.utc)
            az = sun_azimuth(obs, when)
            el = sun_elevation(obs, when)
            if cfg["min_elevation"] <= el <= cfg["max_elevation"]:
                points.append((az, el, sgn, t_edge))
    print(f"Detected {len(points)} horizon points on {n_days} usable days")
    return points


# ---------------------------------------------------------------------------
# Profile assembly
# ---------------------------------------------------------------------------
def build_profile(points, cfg):
    width = cfg["az_bin_deg"]
    nbins = int(round(360.0 / width))
    centers = (np.arange(nbins) + 0.5) * width

    by_bin = [[] for _ in range(nbins)]
    for az, el, _sgn, _t in points:
        by_bin[int(az // width) % nbins].append(el)

    elev = np.full(nbins, np.nan)
    counts = np.array([len(b) for b in by_bin])
    for b, els in enumerate(by_bin):
        if len(els) >= cfg["min_points_per_bin"]:
            elev[b] = float(np.median(els))

    known = np.flatnonzero(~np.isnan(elev))
    if len(known) < 3:
        sys.exit("Too few populated azimuth bins - collect more history "
                 "or relax the edge-detection thresholds.")

    # interpolate gaps inside the observed azimuth span, 0 outside (north)
    filled = np.zeros(nbins)
    span = slice(known[0], known[-1] + 1)
    filled[span] = np.interp(centers[span], centers[known], elev[known])

    # rolling median smoothing
    w = max(1, cfg["smooth_bins"] | 1)  # force odd
    if w > 1:
        pad = w // 2
        padded = np.pad(filled, pad, mode="edge")
        filled = np.array([np.median(padded[i:i + w]) for i in range(nbins)])

    return centers, filled, counts


def forecast_solar_string(centers, profile, cfg):
    n = cfg["fs_values"]
    if 360 % n:
        sys.exit("fs_values must be a divisor of 360")
    az_out = np.arange(n) * (360.0 / n)
    # wrap-around interpolation
    c = np.concatenate([centers, centers[:1] + 360.0])
    p = np.concatenate([profile, profile[:1]])
    vals = np.interp(az_out, c, p)
    return ",".join(str(int(round(v))) for v in vals)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def write_outputs(points, centers, profile, counts, cfg):
    with open(cfg["out_points_csv"], "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["azimuth_deg", "elevation_deg", "sign", "iso_time"])
        for az, el, sgn, t in sorted(points):
            iso = dt.datetime.fromtimestamp(t).isoformat(timespec="minutes")
            w.writerow([f"{az:.2f}", f"{el:.2f}", sgn, iso])

    with open(cfg["out_profile_csv"], "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["azimuth_deg", "elevation_deg", "n_points"])
        for c, e, n in zip(centers, profile, counts):
            w.writerow([f"{c:.1f}", f"{e:.2f}", n])

    fs = forecast_solar_string(centers, profile, cfg)
    print("\nforecast.solar horizon parameter "
          f"({cfg['fs_values']} values, start=N, clockwise):\n")
    print(f"  &horizon={fs}\n")
    print(f"Wrote {cfg['out_points_csv']} and {cfg['out_profile_csv']}")

    if cfg["out_plot_png"]:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            print("matplotlib not installed - skipping plot")
            return
        fig, ax = plt.subplots(figsize=(11, 4.5))
        pts = np.array([(az, el, sgn) for az, el, sgn, _ in points])
        rising = pts[pts[:, 2] > 0]
        falling = pts[pts[:, 2] < 0]
        ax.scatter(rising[:, 0], rising[:, 1], s=8, alpha=0.4,
                   label="sun appears", color="tab:orange")
        ax.scatter(falling[:, 0], falling[:, 1], s=8, alpha=0.4,
                   label="sun disappears", color="tab:blue")
        ax.plot(centers, profile, color="black", lw=2, label="horizon profile")
        ax.set_xlabel("azimuth (deg, 0 = N, 90 = E)")
        ax.set_ylabel("elevation (deg)")
        ax.set_xlim(0, 360)
        ax.set_ylim(bottom=0)
        ax.grid(alpha=0.3)
        ax.legend()
        fig.tight_layout()
        fig.savefig(cfg["out_plot_png"], dpi=130)
        print(f"Wrote {cfg['out_plot_png']}")


# ---------------------------------------------------------------------------
def main():
    cfg = CONFIG
    ts, val = load_states(cfg)
    grid, v = resample(ts, val, cfg)
    points = collect_points(grid, v, cfg)
    if not points:
        sys.exit("No edges detected. Lower min_day_max / high_frac, "
                 "or shorten pre/post windows.")
    centers, profile, counts = build_profile(points, cfg)
    write_outputs(points, centers, profile, counts, cfg)


if __name__ == "__main__":
    main()
