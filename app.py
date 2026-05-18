import json
import os
import threading
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")

CENSUS_VARS = ",".join([
    "NAME",
    "B02001_001E",  # Total population
    "B02001_002E",  # White alone
    "B02001_003E",  # Black or African American alone
    "B02001_004E",  # American Indian & Alaska Native alone
    "B02001_005E",  # Asian alone
    "B02001_006E",  # Native Hawaiian & Other Pacific Islander alone
    "B02001_007E",  # Some other race alone
    "B02001_008E",  # Two or more races
    "B03003_003E",  # Hispanic or Latino (any race)
])


def get_api_key():
    key = os.environ.get("CENSUS_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f).get("api_key", "").strip()
    except Exception:
        return ""


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def status():
    return jsonify({"has_key": bool(get_api_key())})


@app.route("/api/save-key", methods=["POST"])
def save_key():
    data = request.get_json() or {}
    key = data.get("key", "").strip()
    if not key:
        return jsonify({"error": "No key provided"}), 400

    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump({"api_key": key}, f)
    except OSError as e:
        return jsonify({"error": f"Could not save config: {e}"}), 500

    return jsonify({"ok": True})


@app.route("/api/clear-key", methods=["POST"])
def clear_key():
    try:
        if os.path.exists(CONFIG_FILE):
            os.remove(CONFIG_FILE)
    except OSError:
        pass
    return jsonify({"ok": True})


@app.route("/api/census-data")
def census_data():
    key = get_api_key()
    if not key:
        return jsonify({"error": "no_key"}), 401

    url = (
        f"https://api.census.gov/data/2022/acs/acs5"
        f"?get={CENSUS_VARS}&for=county:*&key={key}"
    )
    try:
        r = requests.get(url, timeout=60)
        body = r.text.strip()
        if not body.startswith("["):
            return jsonify({"error": "invalid_key"}), 401
        return jsonify(r.json())
    except requests.Timeout:
        return jsonify({"error": "Census API timed out — try again in a moment."}), 504
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 502


# ── Chain locations — shared helpers ───────────────────────────────────────

CACHE_MAX_AGE_DAYS = 7
OVERPASS_URL = "https://overpass.kumi.systems/api/interpreter"


def _fetch_chain(name, cache_file, query, cache_var, lock, ready):
    """Generic: load from file cache if fresh, otherwise fetch from Overpass."""
    if os.path.exists(cache_file):
        try:
            age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(cache_file))
            if age < timedelta(days=CACHE_MAX_AGE_DAYS):
                with open(cache_file) as f:
                    data = json.load(f)
                with lock:
                    cache_var[0] = data
                ready.set()
                print(f"  {name}: loaded {data['count']} locations from cache ({age.days}d old)")
                return
        except Exception as e:
            print(f"  {name} cache read error: {e}")

    print(f"  {name}: fetching from OpenStreetMap...")
    try:
        r = requests.post(
            OVERPASS_URL,
            data={"data": query},
            headers={"User-Agent": "minority-map/1.0"},
            timeout=90,
        )
        r.raise_for_status()
        elements = r.json().get("elements", [])

        locations = []
        for el in elements:
            if el["type"] == "node":
                locations.append({"lat": el["lat"], "lon": el["lon"]})
            elif el["type"] == "way" and "center" in el:
                locations.append({"lat": el["center"]["lat"], "lon": el["center"]["lon"]})

        data = {"locations": locations, "count": len(locations)}
        with open(cache_file, "w") as f:
            json.dump(data, f)
        with lock:
            cache_var[0] = data
        print(f"  {name}: fetched and cached {len(locations)} locations")

    except Exception as e:
        print(f"  {name} fetch error: {e}")
    finally:
        ready.set()


# ── Popeyes ────────────────────────────────────────────────────────────────

POPEYES_CACHE_FILE = os.path.join(os.path.dirname(__file__), "popeyes_cache.json")
POPEYES_QUERY = """
[out:json][timeout:120];
area["ISO3166-1"="US"][admin_level=2]->.usa;
(
  node["amenity"="fast_food"]["name"~"^Popeyes",i](area.usa);
  way["amenity"="fast_food"]["name"~"^Popeyes",i](area.usa);
);
out center;
"""

_popeyes_cache = [None]
_popeyes_lock  = threading.Lock()
_popeyes_ready = threading.Event()

threading.Thread(
    target=_fetch_chain,
    args=("Popeyes", POPEYES_CACHE_FILE, POPEYES_QUERY, _popeyes_cache, _popeyes_lock, _popeyes_ready),
    daemon=True,
).start()


@app.route("/api/popeyes")
def popeyes():
    _popeyes_ready.wait(timeout=100)
    with _popeyes_lock:
        if _popeyes_cache[0] is not None:
            return jsonify(_popeyes_cache[0])
    return jsonify({"error": "Popeyes data unavailable — try again shortly."}), 503


# ── KFC ────────────────────────────────────────────────────────────────────

KFC_CACHE_FILE = os.path.join(os.path.dirname(__file__), "kfc_cache.json")
KFC_QUERY = """
[out:json][timeout:120];
area["ISO3166-1"="US"][admin_level=2]->.usa;
(
  node["amenity"="fast_food"]["name"~"^KFC",i](area.usa);
  way["amenity"="fast_food"]["name"~"^KFC",i](area.usa);
);
out center;
"""

_kfc_cache = [None]
_kfc_lock  = threading.Lock()
_kfc_ready = threading.Event()

threading.Thread(
    target=_fetch_chain,
    args=("KFC", KFC_CACHE_FILE, KFC_QUERY, _kfc_cache, _kfc_lock, _kfc_ready),
    daemon=True,
).start()


@app.route("/api/kfc")
def kfc():
    _kfc_ready.wait(timeout=100)
    with _kfc_lock:
        if _kfc_cache[0] is not None:
            return jsonify(_kfc_cache[0])
    return jsonify({"error": "KFC data unavailable — try again shortly."}), 503


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  Minority Map  ->  http://localhost:{port}\n")
    app.run(debug=False, port=port)
