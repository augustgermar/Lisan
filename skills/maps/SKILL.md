# maps

Geocoding, reverse geocoding, nearby POI search, road distance/directions,
timezones, and area lookups — all via free OpenStreetMap services
(Nominatim, Overpass, OSRM, TimeAPI). No API key, no credentials.

The bundled `maps_client.py` (ported from the Hermes agent skill, stdlib
only) does the actual work; `tool.py` maps the `action` parameter onto its
subcommands and runs it in a subprocess.

Notes:
- Nominatim enforces ~1 request/second; the client rate-limits and retries
  itself. Heavy repeated calls will be slow by design.
- `nearby` accepts either `near` (a place name, geocoded first) or explicit
  `lat`/`lon`, plus a `category` like `cafe`, `pharmacy`, `park`,
  `supermarket`, `gas_station`.
- `distance`/`directions` take `origin`, `destination`, and optional `mode`
  (driving/walking/cycling).
