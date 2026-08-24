"""Búsqueda de lugares (geocodificación) contra Nominatim.

Por qué existe
──────────────
El catálogo de `REGIONS` es una lista curada: sirve para arrancar rápido pero no
puede anticipar dónde va a querer trabajar el usuario. Añadir una ciudad era editar
un fichero Python, o sea, no era una operación de producto sino de desarrollo.

Con esto, cualquier lugar del planeta que Nominatim sepa nombrar se puede convertir
en una región: se busca, se elige, se fija un radio y se construye con el mismo
`build_region` de siempre. El catálogo curado sigue estando, ahora como atajo.

Sobre el uso de Nominatim
─────────────────────────
Es un servicio público y gratuito con una política de uso estricta: identificarse
con un User-Agent real y no exceder ~1 petición por segundo. Ambas cosas se
respetan aquí. Para un despliegue comercial hay que auto-hospedar Nominatim o
contratar un geocodificador — igual que con los teselados (doc 07 §2.4).
"""
from __future__ import annotations

import re
import threading
import time
import unicodedata

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "RaceEngine/2.0 (route design tool; contact: self-hosted)"
MIN_INTERVAL_S = 1.1          # política de uso de Nominatim

_lock = threading.Lock()
_last_call = 0.0

# Radio por defecto según el tipo de lugar. Un barrio no necesita 20 km de grafo
# y un área metropolitana no cabe en 6: descargar de más cuesta minutos y memoria.
DEFAULT_RADIUS_M = {
    "city": 12000, "town": 8000, "municipality": 12000, "village": 5000,
    "borough": 8000, "suburb": 6000, "neighbourhood": 3500, "quarter": 3500,
    "county": 15000, "state": 20000, "island": 10000, "hamlet": 3000,
}


def _throttle() -> None:
    global _last_call
    with _lock:
        wait = MIN_INTERVAL_S - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()


def slugify(name: str) -> str:
    """Identificador estable a partir del nombre. Se usa como nombre de fichero."""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()
    return (s or "region")[:48]


def _radius_for(entry: dict) -> int:
    """Radio sugerido: del bounding box si lo hay, si no del tipo de lugar."""
    bb = entry.get("boundingbox")
    if bb and len(bb) == 4:
        try:
            south, north, west, east = (float(v) for v in bb)
            import math
            lat_m = (north - south) * 111_320 / 2
            lon_m = (east - west) * 111_320 * math.cos(math.radians((north + south) / 2)) / 2
            r = int(max(lat_m, lon_m))
            # Acotado: por debajo de 2 km no hay red suficiente ni para un 5K, y
            # por encima de 25 km la descarga de Overpass se vuelve impracticable.
            return max(2500, min(25000, r))
        except (TypeError, ValueError):
            pass
    return DEFAULT_RADIUS_M.get(entry.get("type", ""), 8000)


def search(query: str, limit: int = 8, timeout: float = 20.0,
           any_kind: bool = False) -> list[dict]:
    """Devuelve candidatos `{name, display, lat, lon, kind, radius_m, id}`.

    `any_kind=True` levanta el filtro de clases. Para CREAR una región sólo
    interesan lugares habitados —hace falta un radio con sentido—, pero para IR a
    un sitio del mapa vale cualquier cosa: un cerro, un embalse, un refugio. Es
    justo lo que se busca cuando se traza una ruta de montaña.
    """
    q = (query or "").strip()
    if len(q) < 2:
        return []
    import requests  # import diferido: la app arranca sin red

    _throttle()
    resp = requests.get(
        NOMINATIM_URL,
        params={"q": q, "format": "jsonv2", "limit": max(1, min(limit, 20)),
                "addressdetails": 1, "accept-language": "es,en"},
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
    )
    resp.raise_for_status()
    out: list[dict] = []
    for e in resp.json():
        if not any_kind and e.get("class") not in {"place", "boundary", "landuse"}:
            continue
        display = e.get("display_name", "")
        name = e.get("name") or display.split(",")[0]
        addr = e.get("address") or {}
        country = addr.get("country") or ""
        label = f"{name} · {country}" if country and country not in name else name
        out.append({
            "id": f"c_{slugify(name)}_{int(abs(float(e['lat'])) * 100)}"
                  f"{int(abs(float(e['lon'])) * 100)}",
            "name": name,
            "label": label,
            "display": display,
            "lat": float(e["lat"]),
            "lon": float(e["lon"]),
            "kind": e.get("type", ""),
            "country": country,
            "radius_m": _radius_for(e),
        })
    return out
