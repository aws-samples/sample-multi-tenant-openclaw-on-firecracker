---
name: weather
description: Look up current weather and short forecast for any city or coordinates using free, no-key public APIs (wttr.in and Open-Meteo). Use when the user asks about weather, temperature, rain, or forecast for a place.
metadata: { "openclaw": { "always": false } }
---

# Weather

Fetches real weather from free public APIs that need no API key. Primary: `wttr.in` (human-readable, one call). Fallback: Open-Meteo (structured JSON, needs a geocode step).

## Features

- Current conditions + multi-day forecast for any city name or lat/lon.
- No API key required.
- Plain-text one-liner mode for chat, or JSON for downstream use.

## Usage

### Quick one-line current weather (wttr.in)

```bash
# %l location, %c condition, %t temp, %h humidity, %w wind
curl -fsS "https://wttr.in/Singapore?format=%l:+%c+%t+humidity:%h+wind:%w"
# -> Singapore: ⛅️ +31°C humidity:74% wind:11km/h
```

### 3-day compact forecast

`wttr.in?format=j1` returns full JSON; parse it with a heredoc (avoids shell quote-escaping pitfalls):

```bash
python3 - "Tokyo" <<'PY'
import sys, json, urllib.request
city = sys.argv[1]
d = json.load(urllib.request.urlopen(f"https://wttr.in/{city}?format=j1", timeout=20))
for x in d["weather"]:
    print(f'{x["date"]}: max {x["maxtempC"]}C / min {x["mintempC"]}C')
PY
# -> 2026-06-25: max 22C / min 21C
#    2026-06-26: max 24C / min 21C
#    2026-06-27: max 24C / min 22C
```

### Structured JSON via Open-Meteo (fallback when wttr.in is down)

```bash
# 1) geocode the city name -> lat/lon
LAT_LON=$(curl -fsS "https://geocoding-api.open-meteo.com/v1/search?name=Dubai&count=1" \
  | python3 -c 'import sys,json; r=json.load(sys.stdin)["results"][0]; print(r["latitude"], r["longitude"])')
read LAT LON <<< "$LAT_LON"

# 2) current weather
curl -fsS "https://api.open-meteo.com/v1/forecast?latitude=$LAT&longitude=$LON&current=temperature_2m,relative_humidity_2m,wind_speed_10m"
```

## Rules for OpenClaw

- Prefer wttr.in `format=` for chat (one call, already human-readable). Use Open-Meteo only as fallback or when the user needs structured fields.
- Always `curl -fsS` so HTTP errors fail loudly instead of printing error HTML.
- If a city is ambiguous, take the top geocode hit and state the resolved location (e.g. "Dubai, AE").
- Report temperature with units (°C/°F) and name the source. Don't invent values if the call fails — say the lookup failed and offer a retry.
