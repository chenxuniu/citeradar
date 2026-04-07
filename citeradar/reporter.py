"""
reporter.py — Summary Statistics and Interactive World Map

Reads the author-profile data and produces:

1. A plain-text summary report (``summary.txt``) with:
   - Total unique researchers, countries, institutions, cities
   - Bar-chart breakdown by country
   - Top institutions
   - Most-cited papers

2. An interactive HTML world map (``citation_map.html``) built with Folium:
   - Heat-map layer showing global citation density
   - Circle-marker layer with per-city popups listing researcher names
     and institutions
   - Colour encodes researcher count: blue (1) → amber (4–6) → red (11+)
   - Circle radius scales as sqrt(count) so areas are proportional
   - Legend and title overlays

Geocoding
---------
City coordinates are obtained via the Nominatim geocoder (OpenStreetMap),
which is free but rate-limited to 1 request/second.  A module-level cache
prevents duplicate look-ups within a single run.
"""

import math
import time
import json
from collections import Counter, defaultdict
from typing import Optional

import folium
from folium.plugins import HeatMap
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------

_geo_cache: dict[str, Optional[tuple[float, float]]] = {}


def geocode(city: str, country: str, geolocator) -> Optional[tuple[float, float]]:
    """Return ``(lat, lng)`` for a city, with caching and polite delays."""
    key    = f"{city}|{country}"
    if key in _geo_cache:
        return _geo_cache[key]
    query  = f"{city}, {country}" if country else city
    result = None
    for _ in range(2):
        try:
            loc = geolocator.geocode(query, timeout=10)
            if loc:
                result = (loc.latitude, loc.longitude)
            break
        except (GeocoderTimedOut, GeocoderServiceError):
            time.sleep(2)
    _geo_cache[key] = result
    time.sleep(1.1)   # Nominatim rate limit: 1 req/sec
    return result


# ---------------------------------------------------------------------------
# Colour / size helpers
# ---------------------------------------------------------------------------

def _count_to_colour(n: int) -> str:
    if n == 1:   return "#4A90D9"   # light blue
    if n <= 3:   return "#2166AC"   # blue
    if n <= 6:   return "#F4A32A"   # amber
    if n <= 10:  return "#E06C1A"   # orange
    return               "#C0392B"  # red


def _count_to_radius(n: int) -> int:
    """Circle radius grows with log₂(n) so areas remain proportional."""
    return max(7, int(7 + 10 * math.log(n + 1, 2)))


# ---------------------------------------------------------------------------
# Summary computation
# ---------------------------------------------------------------------------

def compute_summary(authors: list[dict]) -> dict:
    """
    Aggregate unique researcher, country, institution, and city statistics.

    Returns a dict with keys:
        unique_researchers, unique_countries, unique_affiliations,
        unique_cities, country_counter, affil_counter, city_counter,
        paper_counter, researcher_location
    """
    unique_researchers  = set()
    unique_countries    = set()
    unique_affiliations = set()
    unique_cities       = set()
    country_counter     = Counter()
    affil_counter       = Counter()
    city_counter        = Counter()
    paper_counter       = Counter()
    researcher_location: dict[str, dict] = {}

    for a in authors:
        name    = a.get("full_name",        "").strip()
        country = a.get("country",          "").strip()
        affil   = a.get("institution",      "").strip().split(",")[0].strip()
        city    = a.get("city",             "").strip()
        cited   = a.get("cited_paper_title","").strip()

        if not name:
            continue

        unique_researchers.add(name)
        paper_counter[cited] += 1

        if country:
            unique_countries.add(country)
            country_counter[country] += 1
        if affil:
            unique_affiliations.add(affil)
            affil_counter[affil] += 1
        if city and country:
            unique_cities.add((city, country))
            city_counter[(city, country)] += 1

        # Keep the richest location record per researcher
        if name not in researcher_location or (
            city and not researcher_location[name].get("city")
        ):
            researcher_location[name] = {
                "city": city, "country": country, "institution": affil,
            }

    return {
        "unique_researchers":  len(unique_researchers),
        "unique_countries":    len(unique_countries),
        "unique_affiliations": len(unique_affiliations),
        "unique_cities":       len(unique_cities),
        "country_counter":     country_counter,
        "affil_counter":       affil_counter,
        "city_counter":        city_counter,
        "paper_counter":       paper_counter,
        "researcher_location": researcher_location,
    }


def format_summary(s: dict, author_name: str) -> str:
    """Return the summary as a formatted string (also suitable for saving)."""
    lines = []
    lines.append("=" * 58)
    lines.append(f"  CiteRadar — Citation Summary for {author_name}")
    lines.append("=" * 58)
    lines.append(f"  Unique researchers who cited you : {s['unique_researchers']}")
    lines.append(f"  Countries                        : {s['unique_countries']}")
    lines.append(f"  Institutions / affiliations      : {s['unique_affiliations']}")
    lines.append(f"  Cities (with location data)      : {s['unique_cities']}")

    lines.append("\n  Top Countries")
    lines.append("  " + "─" * 40)
    total = sum(s["country_counter"].values()) or 1
    for country, cnt in s["country_counter"].most_common(10):
        bar = "█" * int(cnt / total * 30)
        lines.append(f"  {country:<28} {cnt:>4}  {bar}")

    lines.append("\n  Top Institutions")
    lines.append("  " + "─" * 40)
    for affil, cnt in s["affil_counter"].most_common(8):
        lines.append(f"  {affil[:40]:<40}  {cnt}")

    lines.append("\n  Your Most-Cited Papers")
    lines.append("  " + "─" * 40)
    for paper, cnt in s["paper_counter"].most_common():
        lines.append(f"  [{cnt:>2}x]  {paper[:60]}")

    lines.append("=" * 58)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Map builder
# ---------------------------------------------------------------------------

def build_map(s: dict, output_path: str) -> None:
    """
    Geocode all cities and build an interactive Folium world map.

    Layers:
    - Heat-map (density, toggleable)
    - Circle markers with popup details (researcher names + institutions)
    """
    city_counter        = s["city_counter"]
    researcher_location = s["researcher_location"]

    city_researchers: dict[tuple, list[str]] = defaultdict(list)
    city_institutions: dict[tuple, set]      = defaultdict(set)

    for name, loc in researcher_location.items():
        city    = loc.get("city",        "")
        country = loc.get("country",     "")
        inst    = loc.get("institution", "")
        if city and country:
            city_researchers[(city, country)].append(name)
            if inst:
                city_institutions[(city, country)].add(inst)

    print("Geocoding cities…")
    geolocator = Nominatim(user_agent="citeradar_map_v1", timeout=10)
    city_coords: dict[tuple, tuple[float, float]] = {}
    for city, country in city_researchers:
        coords = geocode(city, country, geolocator)
        if coords:
            city_coords[(city, country)] = coords
            print(f"  ✓ {city}, {country}  →  {coords[0]:.2f}, {coords[1]:.2f}")
        else:
            print(f"  ✗ {city}, {country}  — not found")

    # ── Base map ──────────────────────────────────────────────────────────
    m = folium.Map(location=[20, 0], zoom_start=2, tiles="CartoDB positron")

    # ── Layer 1: heat map ─────────────────────────────────────────────────
    heat_data = []
    for (city, country), (lat, lng) in city_coords.items():
        n = len(city_researchers[(city, country)])
        heat_data.extend([[lat, lng]] * n)
    HeatMap(heat_data, radius=25, blur=20, min_opacity=0.3,
            max_zoom=6, name="Density heatmap").add_to(m)

    # ── Layer 2: circle markers ───────────────────────────────────────────
    marker_layer = folium.FeatureGroup(name="Cities", show=True)
    for (city, country), (lat, lng) in city_coords.items():
        researchers  = city_researchers[(city, country)]
        institutions = city_institutions[(city, country)]
        n = len(researchers)

        inst_html  = "".join(f"<li style='margin:2px 0'>{i}</li>"
                             for i in sorted(institutions))
        names_html = "".join(f"<li style='margin:2px 0'>{r}</li>"
                             for r in sorted(researchers))
        popup_html = f"""
        <div style='font-family:Arial,sans-serif;min-width:220px;max-width:320px'>
          <div style='background:#2C3E50;color:white;padding:8px 12px;
                      border-radius:4px 4px 0 0;font-weight:bold;font-size:14px'>
            📍 {city}, {country}
          </div>
          <div style='padding:10px 12px;background:#f9f9f9;border-radius:0 0 4px 4px'>
            <b style='color:#2C3E50'>{n} researcher{"s" if n > 1 else ""}</b>
            <hr style='margin:6px 0;border-color:#ddd'>
            <b>Institutions:</b>
            <ul style='margin:4px 0;padding-left:16px'>{inst_html}</ul>
            <b>Researchers:</b>
            <ul style='margin:4px 0;padding-left:16px'>{names_html}</ul>
          </div>
        </div>"""

        folium.CircleMarker(
            location=[lat, lng],
            radius=_count_to_radius(n),
            color="white", weight=1.5, fill=True,
            fill_color=_count_to_colour(n), fill_opacity=0.85,
            popup=folium.Popup(popup_html, max_width=340),
            tooltip=f"{city} — {n} researcher{'s' if n > 1 else ''}",
        ).add_to(marker_layer)
    marker_layer.add_to(m)

    # ── Legend ────────────────────────────────────────────────────────────
    legend_html = """
    <div style='position:fixed;bottom:30px;left:30px;z-index:1000;
                background:white;padding:14px 18px;border-radius:8px;
                box-shadow:0 2px 10px rgba(0,0,0,0.2);
                font-family:Arial,sans-serif;font-size:13px'>
      <b style='font-size:14px'>Researchers per city</b><br><br>
      <span style='color:#4A90D9'>&#9679;</span>  1 researcher<br>
      <span style='color:#2166AC'>&#9679;</span>  2&ndash;3 researchers<br>
      <span style='color:#F4A32A'>&#9679;</span>  4&ndash;6 researchers<br>
      <span style='color:#E06C1A'>&#9679;</span>  7&ndash;10 researchers<br>
      <span style='color:#C0392B'>&#9679;</span>  11+ researchers<br>
      <br><i style='color:#888;font-size:11px'>Circle size proportional to count<br>
      Click any circle for details</i>
    </div>"""
    m.get_root().html.add_child(folium.Element(legend_html))

    # ── Title ─────────────────────────────────────────────────────────────
    title_html = """
    <div style='position:fixed;top:12px;left:50%;transform:translateX(-50%);
                z-index:1000;background:white;padding:10px 24px;border-radius:8px;
                box-shadow:0 2px 10px rgba(0,0,0,0.15);
                font-family:Arial,sans-serif;text-align:center'>
      <span style='font-size:18px;font-weight:bold;color:#2C3E50'>
        CiteRadar &mdash; Who cited your papers?
      </span>
    </div>"""
    m.get_root().html.add_child(folium.Element(title_html))

    folium.LayerControl(collapsed=False).add_to(m)
    m.save(output_path)
    print(f"\nMap saved → {output_path}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_report(authors_data: dict, author_name: str,
                    summary_path: str, map_path: str) -> None:
    """
    Generate the text summary and the interactive HTML map.

    Parameters
    ----------
    authors_data : dict  — JSON object with ``"authors"`` list
    author_name  : str   — displayed in the summary header
    summary_path : str   — output path for the plain-text summary
    map_path     : str   — output path for the HTML map
    """
    authors = (
        authors_data.get("authors", authors_data)
        if isinstance(authors_data, dict)
        else authors_data
    )

    s    = compute_summary(authors)
    text = format_summary(s, author_name)
    print(text)

    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"\nSummary saved → {summary_path}")

    build_map(s, map_path)
