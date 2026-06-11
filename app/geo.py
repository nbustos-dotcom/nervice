"""Single-user location store. The phone POSTs its GPS to /location; weather reads the latest here.
Until any GPS arrives, defaults to Orland Hills, IL (Nate's home) instead of the old Oswego coords.
Persisted to data/location.json. Reverse-geocodes to a display name via BigDataCloud (keyless)."""
import app.net  # noqa  (truststore: Norton TLS)

import json
import pathlib
import datetime

import httpx

# Default until the phone sends real GPS — Orland Hills, IL (was hardcoded Oswego 41.68,-88.35).
_DEFAULT = {"lat": 41.60, "lon": -87.85, "name": "Orland Hills, IL", "source": "default"}
_STORE = pathlib.Path(__file__).resolve().parent.parent / "data" / "location.json"


def load_location() -> dict:
    """Latest known location {lat, lon, name, source[, updated]} — the phone's GPS if it has ever
    sent one, else the Orland Hills default. Never raises."""
    try:
        if _STORE.exists():
            d = json.loads(_STORE.read_text(encoding="utf-8"))
            if isinstance(d.get("lat"), (int, float)) and isinstance(d.get("lon"), (int, float)):
                return d
    except Exception:
        pass
    return dict(_DEFAULT)


async def _reverse_geocode(lat: float, lon: float) -> str:
    """lat/lon -> 'City, ST' via BigDataCloud reverse-geocode-client (keyless). Coord string on fail."""
    try:
        async with httpx.AsyncClient(timeout=6) as c:
            r = await c.get("https://api.bigdatacloud.net/data/reverse-geocode-client",
                            params={"latitude": lat, "longitude": lon, "localityLanguage": "en"})
        if r.status_code == 200:
            d = r.json()
            place = d.get("locality") or d.get("city") or d.get("principalSubdivision") or ""
            st = (d.get("principalSubdivisionCode") or "").replace("US-", "")
            name = ", ".join([p for p in (place, st) if p]).strip(", ")
            if name:
                return name
    except Exception:
        pass
    return f"{round(lat, 2)}, {round(lon, 2)}"


async def set_location(lat: float, lon: float) -> dict:
    """Store the phone's reported location (reverse-geocoded for display), persisted to disk."""
    lat, lon = float(lat), float(lon)
    name = await _reverse_geocode(lat, lon)
    rec = {"lat": round(lat, 4), "lon": round(lon, 4), "name": name, "source": "gps",
           "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    try:
        _STORE.parent.mkdir(parents=True, exist_ok=True)
        _STORE.write_text(json.dumps(rec), encoding="utf-8")
    except Exception:
        pass
    return rec
