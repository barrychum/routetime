import os
"""
Route Time Checker — Flask app
Run:  python app.py
Then open http://localhost:5000

Routes are saved to routes.json automatically.
"""

import json
import re
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

ROUTES_FILE = Path(os.environ.get("DATA_DIR", ".")) / "routes.json"


# ─── PERSISTENCE ─────────────────────────────────────────────────────────────

def load_routes():
    if ROUTES_FILE.exists():
        try:
            return json.loads(ROUTES_FILE.read_text())
        except Exception:
            pass
    return {
        "api_key": "",
        "mode": "driving",
        "routes": [
            {"label": "Route 1", "url": ""},
            {"label": "Route 2", "url": ""},
        ]
    }


def save_routes(data):
    ROUTES_FILE.write_text(json.dumps(data, indent=2))


# ─── URL PARSER ──────────────────────────────────────────────────────────────

def parse_gmaps_url(url):
    parsed_url = urlparse(url)
    segments = parsed_url.path.split('/')
    path_coords = []
    has_place_name = False

    for seg in segments:
        if seg.startswith('@'):
            continue
        m = re.match(r'^(-?\d+\.\d+),\+?(-?\d+\.\d+)$', seg)
        if m:
            path_coords.append((float(m.group(1)), float(m.group(2))))
        elif seg and seg not in ('maps', 'dir', '') and not seg.startswith('data=') and not seg.startswith('am='):
            has_place_name = True

    decoded = unquote(url)

    # Extract all coords from data= blob. Google uses several prefixes:
    #   !2m2!1d<lng>!2d<lat>  — place name resolved coords
    #   !3m4!1m2!1d<lng>!2d<lat>  — waypoints added via dragging / Add stop
    data_coords = [
        (float(lat), float(lng))
        for lng, lat in re.findall(r'!(?:2m2|3m4!1m2)!1d(-?\d+\.\d+)!2d(-?\d+\.\d+)', decoded)
    ]

    def near(a, b):
        return abs(a[0] - b[0]) < 0.00001 and abs(a[1] - b[1]) < 0.00001

    def dedup(lst):
        seen = []
        for c in lst:
            if not any(near(c, s) for s in seen):
                seen.append(c)
        return seen

    # Coords that appear in data= but not already in the path
    extra = [c for c in data_coords if not any(near(c, p) for p in path_coords)]

    if has_place_name and data_coords:
        # Place name: first data= coord resolves the text origin to lat/lng.
        # Remaining extra data= coords (if any) are mid-route waypoints.
        origin = data_coords[0]
        mid_waypoints = [c for c in extra if not near(c, data_coords[0])]
        coords = dedup([origin] + mid_waypoints + path_coords)
    else:
        # All path segments are coords. Extra data= coords are mid-route
        # waypoints (e.g. from dragging the route) inserted between origin/dest.
        coords = dedup([path_coords[0]] + extra + path_coords[1:]) if extra else path_coords

    if len(coords) < 2:
        raise ValueError("Could not find at least 2 coordinate pairs in URL")

    return {
        "origin":      coords[0],
        "destination": coords[-1],
        "waypoints":   coords[1:-1],
    }



# ─── DIRECTIONS API ──────────────────────────────────────────────────────────

def get_route_time(parsed, api_key, mode="driving"):
    origin      = f"{parsed['origin'][0]},{parsed['origin'][1]}"
    destination = f"{parsed['destination'][0]},{parsed['destination'][1]}"
    waypoints   = "|".join(f"via:{lat},{lng}" for lat, lng in parsed["waypoints"])

    # Build query string manually — requests would percent-encode commas in
    # coordinates (53.51,-2.65 → 53.51%2C-2.65) which the Directions API rejects.
    from urllib.parse import quote
    qs = (
        f"origin={origin}"
        f"&destination={destination}"
        f"&mode={mode}"
        f"&departure_time=now"
        f"&key={api_key}"
    )
    if waypoints:
        qs += f"&waypoints={quote(waypoints, safe=':,|')}"

    resp = requests.get(
        f"https://maps.googleapis.com/maps/api/directions/json?{qs}",
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()

    if data["status"] != "OK":
        raise ValueError(f"API error: {data['status']}")

    legs        = data["routes"][0]["legs"]
    summary     = data["routes"][0].get("summary", "")
    has_traffic = any("duration_in_traffic" in leg for leg in legs)
    total_secs  = sum(leg.get("duration_in_traffic", leg["duration"])["value"] for leg in legs)
    base_secs   = sum(leg["duration"]["value"] for leg in legs)
    dist_m      = sum(leg["distance"]["value"] for leg in legs)

    return {
        "time_str":       fmt_secs(total_secs),
        "base_str":       fmt_secs(base_secs) if has_traffic else None,
        "distance_str":   f"{dist_m / 1000:.1f} km",
        "total_seconds":  total_secs,
        "base_seconds":   base_secs if has_traffic else None,
        "summary":        summary,
        "waypoint_count": len(parsed["waypoints"]),
    }


def fmt_secs(seconds):
    h = seconds // 3600
    m = round((seconds % 3600) / 60)
    return f"{h} hr {m} min" if h else f"{m} min"


# ─── FLASK ROUTES ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", saved=load_routes())


@app.route("/save", methods=["POST"])
def save():
    save_routes(request.json)
    return jsonify({"ok": True})


@app.route("/check", methods=["POST"])
def check():
    body    = request.json
    routes  = body.get("routes", [])
    api_key = body.get("api_key", "")
    mode    = body.get("mode", "driving")

    if not api_key:
        return jsonify({"error": "API key is required"}), 400

    results = []
    for i, route in enumerate(routes, 1):
        url   = route.get("url", "").strip()
        label = route.get("label") or f"Route {i}"
        if not url:
            results.append({"index": i, "label": label, "error": "No URL provided"})
            continue
        try:
            parsed = parse_gmaps_url(url)
            result = get_route_time(parsed, api_key, mode)
            results.append({"index": i, "label": label, **result})
        except ValueError as e:
            results.append({"index": i, "label": label, "error": str(e)})
        except requests.RequestException as e:
            results.append({"index": i, "label": label, "error": f"Network error: {e}"})

    valid = [r for r in results if "error" not in r]
    if len(valid) > 1:
        min(valid, key=lambda r: r["total_seconds"])["fastest"] = True

    return jsonify(results)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5005, debug=False)
