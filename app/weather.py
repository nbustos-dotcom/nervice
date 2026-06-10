import app.net  # noqa  (truststore: Norton TLS)

import httpx

# Approximate coords only (Oswego, IL area) — no PII, no account, one keyless HTTPS call.
_LAT, _LON = 41.68, -88.35


def _code_word(code: int) -> str:
    """WMO weather_code -> one short word."""
    if code in (0, 1):
        return "clear"
    if code in (2, 3):
        return "cloudy"
    if code in (45, 48):
        return "fog"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if 95 <= code <= 99:
        return "storm"
    return "clear"


async def get_weather() -> str | None:
    """Compact human string like '58°F and clear, high 71 low 49', or None on any failure."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get("https://api.open-meteo.com/v1/forecast", params={
                "latitude": _LAT, "longitude": _LON,
                "current": "temperature_2m,weather_code",
                "daily": "temperature_2m_max,temperature_2m_min",
                "temperature_unit": "fahrenheit",
                "timezone": "America/Chicago",
            })
        if r.status_code != 200:
            return None
        d = r.json()
        cur, daily = d["current"], d["daily"]
        temp = round(cur["temperature_2m"])
        word = _code_word(int(cur["weather_code"]))
        hi = round(daily["temperature_2m_max"][0])
        lo = round(daily["temperature_2m_min"][0])
        return f"{temp}°F and {word}, high {hi} low {lo}"
    except Exception:
        return None
