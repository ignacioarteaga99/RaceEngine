"""Carga de OSM vía Overpass y construcción del RaceGraph.

Nota de arquitectura: esto es el pipeline OFFLINE (doc 09 §5.1). Se ejecuta una vez
por región y se cachea. En el camino caliente de una petición NO se llama nunca a
Overpass — ése es el principio 3 de doc 09 ("own the graph").
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from raceengine_contracts import haversine

from .graph import (
    ALL_CLASSES, SURFACE_PAVED, SURFACE_UNPAVED, GraphBuilder, RaceGraph,
    compute_coverage,
)

OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

_TRACKED_TAGS = ("surface", "lit", "oneway", "width", "smoothness")


def _query(q: str, timeout: int = 300) -> dict:
    import requests  # import diferido: solo se necesita al construir una región

    last = None
    for attempt in range(6):
        url = OVERPASS_ENDPOINTS[attempt % len(OVERPASS_ENDPOINTS)]
        try:
            print(f"  [overpass] {url} (intento {attempt + 1})")
            r = requests.post(
                url, data={"data": q},
                headers={"User-Agent": "RaceEngine/2.0 (+https://raceengine.app)"},
                timeout=timeout,
            )
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
            print(f"    {last}; reintentando…")
        except Exception as exc:  # noqa: BLE001
            last = str(exc)
            print(f"    error: {last}")
        time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"Overpass no respondió: {last}")


def download_radius(lat: float, lon: float, radius_m: int, cache_dir: Path,
                    use_cache: bool = True) -> dict:
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = f"osm_{lat:.5f}_{lon:.5f}_{int(radius_m)}.json".replace("-", "m")
    path = cache_dir / key
    if use_cache and path.exists():
        print(f"[geodata] OSM desde caché: {path.name}")
        return json.loads(path.read_text(encoding="utf-8"))

    r = int(radius_m)
    q = f"""[out:json][timeout:300];
(
  way["highway"](around:{r},{lat},{lon});
  node["highway"="traffic_signals"](around:{r},{lat},{lon});
  node["amenity"="hospital"](around:{r},{lat},{lon});
  way["amenity"="hospital"](around:{r},{lat},{lon});
  way["leisure"="park"](around:{r},{lat},{lon});
);
(._;>;);
out body qt;"""
    print(f"[geodata] descargando OSM · radio {r} m alrededor de ({lat:.5f}, {lon:.5f})…")
    data = _query(q)
    path.write_text(json.dumps(data), encoding="utf-8")
    print(f"[geodata] {len(data.get('elements', [])):,} elementos · cacheado en {path.name}")
    return data


def build_graph_from_osm(data: dict, origin_lat: float, origin_lon: float,
                         version: str = "") -> RaceGraph:
    raw_nodes: dict[int, tuple[float, float]] = {}
    ways: list[dict] = []
    signals: set[int] = set()
    hospitals: list[tuple[float, float]] = []
    park_nodes: set[int] = set()

    for el in data.get("elements", ()):
        if el.get("type") == "node":
            nid = el["id"]
            raw_nodes[nid] = (el["lat"], el["lon"])
            tags = el.get("tags", {})
            if tags.get("highway") == "traffic_signals":
                signals.add(nid)
            if tags.get("amenity") == "hospital":
                hospitals.append((el["lat"], el["lon"]))
            if tags.get("leisure") == "park":
                park_nodes.add(nid)
        elif el.get("type") == "way":
            ways.append(el)

    for w in ways:
        if w.get("tags", {}).get("leisure") == "park":
            park_nodes.update(w.get("nodes", ()))

    b = GraphBuilder(origin_lat, origin_lon)
    b.signals = signals

    counts = {t: 0 for t in _TRACKED_TAGS}
    total_ways = 0

    for w in ways:
        tags = w.get("tags", {})
        hw = tags.get("highway")
        if not hw:
            continue
        total_ways += 1
        for t in _TRACKED_TAGS:
            if t in tags:
                counts[t] += 1

        if hw not in ALL_CLASSES:
            # Único filtro de construcción: lo que no sirve para NINGUNA
            # disciplina. Antes se filtraba aquí a las clases de correr, y esa
            # decisión quedaba congelada en el pickle: un grafo construido para
            # una carrera a pie no tenía carreteras nacionales ni caminos, así
            # que no había forma de usarlo para bici sin reconstruirlo entero.
            # Ahora el grafo guarda el superconjunto y la DISCIPLINA elige.
            continue

        surface = str(tags.get("surface", "")).lower()
        paved = (surface in SURFACE_PAVED) or (surface == "" and hw != "track")
        if surface in SURFACE_UNPAVED:
            paved = False
        lit = str(tags.get("lit", "")).lower() in ("yes", "true", "1")
        access = str(tags.get("access", "")).lower()
        private = access in ("private", "no")
        tunnel = str(tags.get("tunnel", "")).lower() in ("yes", "building_passage", "true")

        ns = w.get("nodes", ())
        for a, bnode in zip(ns, ns[1:]):
            if a not in raw_nodes or bnode not in raw_nodes:
                continue
            la, lo = raw_nodes[a]
            lb, lob = raw_nodes[bnode]
            length = haversine(la, lo, lb, lob)
            if length <= 0:
                continue
            b.add_node(a, la, lo)
            b.add_node(bnode, lb, lob)
            park = a in park_nodes or bnode in park_nodes
            b.add_edge(a, bnode, length, hw, paved=paved, lit=lit, park=park,
                       private=private, tunnel=tunnel)

    g = b.build(version=version)

    # Marcar el nodo más cercano a cada hospital (para safety.near_hospitals).
    for hlat, hlon in hospitals:
        n = g.nearest_node(hlat, hlon)
        if n >= 0:
            g.is_hospital[n] = True

    g.coverage = compute_coverage(g, {t: (counts[t], total_ways) for t in _TRACKED_TAGS})
    print(f"[geodata] grafo: {g.n_nodes:,} nodos · {g.n_edges:,} aristas dirigidas "
          f"· {int(g.is_signal.sum()):,} semáforos · {len(hospitals)} hospitales")
    for t, c in g.coverage.items():
        print(f"           cobertura {t:<11} {c['pct']:>5.1f}%  → {c['level']}")
    return g


# ── Corredor: una franja a lo largo de un trayecto ──────────────────────────
#
# Una ruta de bici de 150 km no cabe en «un círculo alrededor de un punto». El
# círculo que la contuviera tendría 75 km de radio y 17.000 km² — cientos de
# megas de OSM, casi todos de sitios por los que la ruta no pasa. La franja que
# de verdad hace falta —10 km a cada lado del trayecto— son 3.000 km², y de esos
# el corredor sólo usa lo que toca.
#
# Overpass acepta un filtro poligonal (`poly:"lat lon lat lon …"`), así que el
# recorte se hace en el servidor y lo que viaja por la red ya es sólo la franja.
def corridor_polygon(points: list[tuple[float, float]],
                     buffer_m: float) -> list[tuple[float, float]]:
    """Anillo cerrado que envuelve la polilínea a `buffer_m` de distancia.

    Se desplaza la línea a un lado, se vuelve por el otro y se cierra. Los
    extremos se rematan con un cuadrado en vez de un semicírculo: sobra un poco
    de área en las puntas y a cambio el polígono tiene la mitad de vértices, que
    es lo que Overpass acaba pagando.
    """
    import math

    pts = [(float(a), float(b)) for a, b in points]
    if len(pts) < 2:
        if not pts:
            raise ValueError("Un corredor necesita al menos dos puntos.")
        la, lo = pts[0]
        d = buffer_m / 111_320.0
        k = max(math.cos(math.radians(la)), 0.1)
        return [(la - d, lo - d / k), (la - d, lo + d / k),
                (la + d, lo + d / k), (la + d, lo - d / k)]

    lat0 = sum(p[0] for p in pts) / len(pts)
    k = max(math.cos(math.radians(lat0)), 0.1)
    # A metros locales, para desplazar en una geometría plana y volver.
    xy = [((lo - pts[0][1]) * 111_320.0 * k, (la - pts[0][0]) * 111_320.0)
          for la, lo in pts]

    def normal(i: int) -> tuple[float, float]:
        # Perpendicular a la cuerda entre vecinos: estable también en los vértices.
        a = xy[max(i - 1, 0)]
        b = xy[min(i + 1, len(xy) - 1)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = math.hypot(dx, dy) or 1.0
        return (-dy / n, dx / n)

    izq, der = [], []
    for i, (x, y) in enumerate(xy):
        nx, ny = normal(i)
        izq.append((x + nx * buffer_m, y + ny * buffer_m))
        der.append((x - nx * buffer_m, y - ny * buffer_m))

    # Remate recto en las puntas: se prolonga el buffer en la dirección del eje.
    def extremo(i: int, signo: int) -> tuple[float, float]:
        j = i + signo
        dx, dy = xy[j][0] - xy[i][0], xy[j][1] - xy[i][1]
        n = math.hypot(dx, dy) or 1.0
        return (-dx / n * buffer_m, -dy / n * buffer_m)

    e0 = extremo(0, 1)
    e1 = extremo(len(xy) - 1, -1)
    izq[0] = (izq[0][0] + e0[0], izq[0][1] + e0[1])
    der[0] = (der[0][0] + e0[0], der[0][1] + e0[1])
    izq[-1] = (izq[-1][0] + e1[0], izq[-1][1] + e1[1])
    der[-1] = (der[-1][0] + e1[0], der[-1][1] + e1[1])

    anillo = izq + der[::-1]
    return [(pts[0][0] + y / 111_320.0, pts[0][1] + x / (111_320.0 * k))
            for x, y in anillo]


def corridor_area_km2(ring: list[tuple[float, float]]) -> float:
    """Área del anillo en km², por la fórmula del cordón sobre metros locales."""
    import math

    if len(ring) < 3:
        return 0.0
    lat0 = sum(p[0] for p in ring) / len(ring)
    k = max(math.cos(math.radians(lat0)), 0.1)
    xy = [((lo - ring[0][1]) * 111_320.0 * k, (la - ring[0][0]) * 111_320.0)
          for la, lo in ring]
    s = 0.0
    for (x1, y1), (x2, y2) in zip(xy, xy[1:] + xy[:1]):
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0 / 1e6


def download_corridor(points: list[tuple[float, float]], buffer_m: float,
                      cache_dir: Path, use_cache: bool = True) -> dict:
    """Descarga la franja alrededor de un trayecto con filtro poligonal."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    ring = corridor_polygon(points, buffer_m)
    firma = hashlib.blake2b(
        json.dumps([[round(a, 5), round(b, 5)] for a, b in ring]).encode(),
        digest_size=8).hexdigest()
    path = cache_dir / f"osm_corr_{firma}.json"
    if use_cache and path.exists():
        print(f"[geodata] OSM desde caché: {path.name}")
        return json.loads(path.read_text(encoding="utf-8"))

    poly = " ".join(f"{la:.5f} {lo:.5f}" for la, lo in ring)
    q = f"""[out:json][timeout:600];
(
  way["highway"](poly:"{poly}");
  node["highway"="traffic_signals"](poly:"{poly}");
  node["amenity"="hospital"](poly:"{poly}");
  way["amenity"="hospital"](poly:"{poly}");
  way["leisure"="park"](poly:"{poly}");
);
(._;>;);
out body qt;"""
    area = corridor_area_km2(ring)
    print(f"[geodata] descargando corredor · {len(points)} puntos · "
          f"franja de {buffer_m / 1000:.1f} km · {area:,.0f} km²…")
    data = _query(q, timeout=600)
    path.write_text(json.dumps(data), encoding="utf-8")
    print(f"[geodata] {len(data.get('elements', [])):,} elementos · caché {path.name}")
    return data
