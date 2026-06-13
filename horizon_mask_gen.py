#!/usr/bin/env python3
"""
horizon_mask_gen.py

Convert the horizon profile (horizon_profile.csv, produced by
horizon_from_pv.py) into a precomputed hourly mask table as a Jinja2 macro
file for Home Assistant custom_templates.

For every table day (keyed "MM-DD") and every local hour the script computes
which fraction of that hour the sun is above the *local* horizon (terrain),
sampled in 5-min substeps. Hours with the sun behind a ridge but above the
astronomical horizon are reduced to the diffuse fraction:

    factor = frac_unblocked + (1 - frac_unblocked) * diffuse_fraction

Hours where the sun is below 0 deg the whole time get factor 1.0 (the
forecast is ~0 there anyway, nothing to correct).

Solstice symmetry
-----------------
The solar declination is symmetric around both solstices: day (solstice - n)
has the same sun path as day (solstice + n). With half_year_table=True only
the arc from the summer solstice (Jun 21) to the winter solstice (Dec 21) is
stored - it covers the full declination range. The Jinja macro mirrors any
other date onto this arc, so missing keys (including Feb 29) are always
resolved via the symmetric day, never via a flat 1.0 fallback.
(Equation-of-time asymmetry between mirrored days is < ~15 min in clock
time - negligible at hourly granularity.)

Output: horizon_mask.jinja  ->  copy to  config/custom_templates/
Reload in HA: Developer Tools -> YAML -> "Custom Jinja2 templates"
(or service homeassistant.reload_custom_templates).

Usage in templates:

    {% from 'horizon_mask.jinja' import horizon_factor_ts %}
    {{ horizon_factor_ts('2026-06-12T07:00:00+02:00') | float(1) }}

Dependencies: numpy, astral  (same venv as horizon_from_pv.py)
"""

import csv
import datetime as dt
import json
import sys
from zoneinfo import ZoneInfo

import numpy as np
from astral import Observer
from astral.sun import azimuth as sun_azimuth, elevation as sun_elevation

# ---------------------------------------------------------------------------
CONFIG = {
    "profile_csv": "horizon_profile.csv",
    "out_jinja": "horizon_mask.jinja",
    "latitude": 47.49,            # Woergl
    "longitude": 12.07,
    "timezone": "Europe/Vienna",
    "diffuse_fraction": 0.18,     # power fraction remaining while beam is blocked
    "substep_min": 5,             # intra-hour sampling
    "ref_year": 2024,             # leap year used as reference calendar
    "half_year_table": True,      # store only Jun 21 .. Dec 21, mirror the rest
}
# Day-of-year of the solstices in the (leap) reference year 2024:
SUMMER_DOY = 173                  # Jun 21
WINTER_DOY = 356                  # Dec 21
# ---------------------------------------------------------------------------


def load_profile(path):
    az, el = [], []
    with open(path) as f:
        for row in csv.DictReader(f):
            az.append(float(row["azimuth_deg"]))
            el.append(float(row["elevation_deg"]))
    if not az:
        sys.exit(f"No rows in {path}")
    az = np.asarray(az)
    el = np.asarray(el)
    order = np.argsort(az)
    return az[order], el[order]


def make_horizon_lookup(az, el):
    # wrap-around interpolation over 0..360
    a = np.concatenate([az, az[:1] + 360.0])
    e = np.concatenate([el, el[:1]])

    def horizon_at(az_query):
        return float(np.interp(az_query % 360.0, a, e))

    return horizon_at


def day_factors(day, horizon_at, obs, tz, cfg):
    n_sub = 60 // cfg["substep_min"]
    hours = []
    for h in range(24):
        above = blocked = 0
        for i in range(n_sub):
            local = dt.datetime(day.year, day.month, day.day, h,
                                i * cfg["substep_min"] + cfg["substep_min"] // 2,
                                tzinfo=tz)
            when = local.astimezone(dt.timezone.utc)
            e = sun_elevation(obs, when)
            if e <= 0.0:
                continue
            above += 1
            if e < horizon_at(sun_azimuth(obs, when)):
                blocked += 1
        if above == 0:
            hours.append(1.0)
        else:
            frac_open = (above - blocked) / above
            f = frac_open + (1.0 - frac_open) * cfg["diffuse_fraction"]
            hours.append(round(f, 2))
    return hours


JINJA_MACROS = """
{{# Mirror any date onto the stored solstice-to-solstice arc.            #}}
{{# Declination is symmetric around both solstices, so a missing day is  #}}
{{# resolved via its mirror day instead of a flat fallback.              #}}
{{% macro _canonical_md(month_day) -%}}
{{%- set ref = strptime('2024-01-01', '%Y-%m-%d') -%}}
{{%- set doy = (strptime('2024-' ~ month_day, '%Y-%m-%d') - ref).days + 1 -%}}
{{%- if doy < {summer} -%}}{{%- set doy = 2 * {summer} - doy -%}}{{%- endif -%}}
{{%- if doy > {winter} -%}}{{%- set doy = 2 * {winter} - doy -%}}{{%- endif -%}}
{{{{ (ref + timedelta(days=doy - 1)).strftime('%m-%d') }}}}
{{%- endmacro %}}

{{# factor by ('MM-DD', hour) #}}
{{% macro horizon_factor(month_day, hour) -%}}
{{%- set key = month_day if month_day in HORIZON_MASK
    else _canonical_md(month_day) | string | trim -%}}
{{{{ HORIZON_MASK.get(key, [1.0] * 24)[hour] }}}}
{{%- endmacro %}}

{{# factor by iso timestamp or datetime, evaluated in local time #}}
{{% macro horizon_factor_ts(ts) -%}}
{{%- set d = as_local(as_datetime(ts)) -%}}
{{{{ horizon_factor('%02d-%02d' % (d.month, d.day), d.hour) }}}}
{{%- endmacro %}}
"""


def main():
    cfg = CONFIG
    az, el = load_profile(cfg["profile_csv"])
    horizon_at = make_horizon_lookup(az, el)
    obs = Observer(latitude=cfg["latitude"], longitude=cfg["longitude"])
    tz = ZoneInfo(cfg["timezone"])

    ref = dt.date(cfg["ref_year"], 1, 1)
    if cfg["half_year_table"]:
        doys = range(SUMMER_DOY, WINTER_DOY + 1)   # Jun 21 .. Dec 21
    else:
        year_len = (dt.date(cfg["ref_year"] + 1, 1, 1) - ref).days
        doys = range(1, year_len + 1)

    table = {}
    for doy in doys:
        day = ref + dt.timedelta(days=doy - 1)
        key = f"{day.month:02d}-{day.day:02d}"
        table[key] = day_factors(day, horizon_at, obs, tz, cfg)

    n_affected = sum(1 for v in table.values() for f in v if f < 1.0)
    print(f"Built mask for {len(table)} days "
          f"({'half-year + mirroring' if cfg['half_year_table'] else 'full year'}), "
          f"{n_affected} affected hours")

    lines = [
        "{#- auto-generated by horizon_mask_gen.py - do not edit manually -#}",
        "{#- factor = fraction of forecast power to keep for that hour    -#}",
        "{%- set HORIZON_MASK = {",
    ]
    for k, v in table.items():
        lines.append(f'  "{k}": {json.dumps(v)},')
    lines.append("} -%}")
    lines.append(JINJA_MACROS.format(summer=SUMMER_DOY, winter=WINTER_DOY))

    with open(cfg["out_jinja"], "w") as f:
        f.write("\n".join(lines))
    print(f"Wrote {cfg['out_jinja']} "
          f"-> copy to config/custom_templates/ and reload custom templates")


if __name__ == "__main__":
    main()
