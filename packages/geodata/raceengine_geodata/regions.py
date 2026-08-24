"""Registro de regiones.

Añadir una ciudad es un cambio de DATOS, no de código (NFR-2.3). Se declara aquí
su centro y radio, y `build` la materializa; el motor la consume por nombre.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .elevation import make_provider
from .graph import RaceGraph, bake_elevation

DATA_DIR = Path(os.environ.get("RACEENGINE_DATA", Path(__file__).resolve().parents[3] / "data"))
CACHE_DIR = DATA_DIR / "cache"
GRAPH_DIR = DATA_DIR / "graphs"


@dataclass(frozen=True)
class RegionSpec:
    id: str
    label: str
    lat: float
    lon: float
    radius_m: int
    synthetic: bool = False
    group: str = "Otros"

    # Corredor: franja a lo largo de un trayecto, en vez de círculo alrededor de
    # un punto. Es lo que hace posible una ruta de bici de 150 km — el círculo
    # que la contuviera tendría 17.000 km² y casi todos de sitios por los que la
    # ruta no pasa. `lat`/`lon` siguen siendo el centro, para encuadrar el mapa,
    # y `radius_m` la semilongitud, para elegir el zoom.
    corridor: tuple[tuple[float, float], ...] = ()
    buffer_m: int = 0

    @property
    def is_corridor(self) -> bool:
        return len(self.corridor) >= 2 and self.buffer_m > 0


REGIONS: dict[str, RegionSpec] = {
    "demo": RegionSpec("demo", "Demo (ciudad sintética)", -33.4372, -70.6506, 9000, True, group="Demo"),
    "santiago_centro": RegionSpec("santiago_centro", "Santiago Centro", -33.4441, -70.6536, 9000, group="Santiago"),
    "las_condes": RegionSpec("las_condes", "Las Condes", -33.4040, -70.5733, 9000, group="Santiago"),
    "providencia": RegionSpec("providencia", "Providencia", -33.4258, -70.6100, 7000, group="Santiago"),
    "nunoa": RegionSpec("nunoa", "Ñuñoa", -33.4569, -70.5975, 7000, group="Santiago"),
    "vitacura": RegionSpec("vitacura", "Vitacura", -33.3899, -70.5760, 7000, group="Santiago"),
    "santiago_metro": RegionSpec("santiago_metro", "Santiago (área metropolitana)",
                                 -33.4450, -70.6500, 20000, group="Santiago"),

    # ── Chile ────────────────────────────────────────────────────────────────
    "valparaiso": RegionSpec("valparaiso", "Valparaíso", -33.0472, -71.6127, 8000, group="Chile"),
    "vina_del_mar": RegionSpec("vina_del_mar", "Viña del Mar", -33.0245, -71.5518, 8000, group="Chile"),
    "concepcion": RegionSpec("concepcion", "Concepción", -36.8270, -73.0503, 8000, group="Chile"),

    # ── LATAM ────────────────────────────────────────────────────────────────
    "buenos_aires": RegionSpec("buenos_aires", "Buenos Aires", -34.6037, -58.3816, 12000, group="LATAM"),
    "montevideo": RegionSpec("montevideo", "Montevideo", -34.9011, -56.1645, 9000, group="LATAM"),
    "lima": RegionSpec("lima", "Lima", -12.0464, -77.0428, 12000, group="LATAM"),
    "bogota": RegionSpec("bogota", "Bogotá", 4.7110, -74.0721, 12000, group="LATAM"),
    "mexico_city": RegionSpec("mexico_city", "Ciudad de México", 19.4326, -99.1332, 12000, group="LATAM"),
    "sao_paulo": RegionSpec("sao_paulo", "São Paulo", -23.5505, -46.6333, 12000, group="LATAM"),
    "rio": RegionSpec("rio", "Rio de Janeiro", -22.9068, -43.1729, 12000, group="LATAM"),

    # ── Europa ───────────────────────────────────────────────────────────────
    "london": RegionSpec("london", "London", 51.5074, -0.1278, 12000, group="Europa"),
    "paris": RegionSpec("paris", "Paris", 48.8566, 2.3522, 10000, group="Europa"),
    "madrid": RegionSpec("madrid", "Madrid", 40.4168, -3.7038, 10000, group="Europa"),
    "barcelona": RegionSpec("barcelona", "Barcelona", 41.3874, 2.1686, 9000, group="Europa"),
    "berlin": RegionSpec("berlin", "Berlin", 52.5200, 13.4050, 12000, group="Europa"),
    "amsterdam": RegionSpec("amsterdam", "Amsterdam", 52.3676, 4.9041, 8000, group="Europa"),
    "rome": RegionSpec("rome", "Roma", 41.9028, 12.4964, 10000, group="Europa"),
    "lisbon": RegionSpec("lisbon", "Lisboa", 38.7223, -9.1393, 8000, group="Europa"),

    # ── Norteamérica ─────────────────────────────────────────────────────────
    "new_york": RegionSpec("new_york", "New York", 40.7128, -74.0060, 12000, group="Norteamérica"),
    "chicago": RegionSpec("chicago", "Chicago", 41.8781, -87.6298, 12000, group="Norteamérica"),
    "boston": RegionSpec("boston", "Boston", 42.3601, -71.0589, 9000, group="Norteamérica"),
    "san_francisco": RegionSpec("san_francisco", "San Francisco", 37.7749, -122.4194, 9000, group="Norteamérica"),

    # ── Asia-Pacífico ────────────────────────────────────────────────────────
    "tokyo": RegionSpec("tokyo", "Tokyo", 35.6762, 139.6503, 12000, group="Asia-Pacífico"),
    "sydney": RegionSpec("sydney", "Sydney", -33.8688, 151.2093, 10000, group="Asia-Pacífico"),
}

_MEMORY: dict[str, RaceGraph] = {}

# ── Regiones a medida ────────────────────────────────────────────────────────
# El catálogo de arriba es curado; éste lo escribe el usuario desde la interfaz
# buscando un lugar cualquiera. Se persiste en JSON junto a los grafos para que
# sobreviva a un reinicio: si no, reiniciar el servidor haría desaparecer del
# selector una ciudad que ya está descargada en disco.
CUSTOM_GROUP = "Mis ciudades"
CORRIDOR_GROUP = "Mis rutas"
CUSTOM_PATH = DATA_DIR / "custom_regions.json"


def _load_custom() -> None:
    import json
    if not CUSTOM_PATH.exists():
        return
    try:
        raw = json.loads(CUSTOM_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001  — un fichero corrupto no puede tumbar el arranque
        print(f"[regions] no se pudo leer {CUSTOM_PATH}; se ignora")
        return
    for e in raw if isinstance(raw, list) else []:
        try:
            REGIONS[e["id"]] = RegionSpec(
                id=e["id"], label=e["label"], lat=float(e["lat"]), lon=float(e["lon"]),
                radius_m=int(e["radius_m"]), synthetic=False,
                group=e.get("group", CUSTOM_GROUP),
                corridor=tuple((float(a), float(b)) for a, b in e.get("corridor", ())),
                buffer_m=int(e.get("buffer_m", 0)),
            )
        except (KeyError, TypeError, ValueError):
            continue


def _save_custom() -> None:
    import json
    rows = [
        {"id": s.id, "label": s.label, "lat": s.lat, "lon": s.lon,
         "radius_m": s.radius_m, "group": s.group,
         "corridor": [list(p) for p in s.corridor], "buffer_m": s.buffer_m}
        for s in REGIONS.values() if s.group in (CUSTOM_GROUP, CORRIDOR_GROUP)
    ]
    CUSTOM_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CUSTOM_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CUSTOM_PATH)      # escritura atómica


def add_custom_region(region_id: str, label: str, lat: float, lon: float,
                      radius_m: int) -> RegionSpec:
    """Registra una región nueva. Idempotente: reusar el mismo id la actualiza."""
    radius_m = int(max(1500, min(30000, radius_m)))
    spec = RegionSpec(id=region_id, label=label, lat=float(lat), lon=float(lon),
                      radius_m=radius_m, synthetic=False, group=CUSTOM_GROUP)
    REGIONS[region_id] = spec
    _save_custom()
    return spec


# Topes del corredor. No son arbitrarios: por encima de esto la descarga deja de
# ser «unos minutos» y pasa a ser una tarde, y Overpass corta por su cuenta.
MAX_CORRIDOR_KM = 600.0
MAX_CORRIDOR_AREA_KM2 = 12_000.0


def corridor_length_km(points) -> float:
    from raceengine_contracts import haversine

    pts = [(float(a), float(b)) for a, b in points]
    return sum(haversine(a[0], a[1], b[0], b[1])
               for a, b in zip(pts, pts[1:])) / 1000.0


def add_corridor_region(region_id: str, label: str, points, buffer_m: int) -> RegionSpec:
    """Registra una región CORREDOR: una franja a lo largo de un trayecto.

    Es la respuesta a «una ruta de bici no cabe en una ciudad». En vez de pedir un
    radio enorme alrededor de un punto —que descarga sobre todo sitios por los que
    la ruta no pasa—, se declara el trayecto y se descarga sólo su franja.
    """
    from .osm import corridor_area_km2, corridor_polygon

    pts = tuple((float(a), float(b)) for a, b in points)
    if len(pts) < 2:
        raise ValueError("Un corredor necesita al menos dos puntos.")
    buffer_m = int(max(1000, min(25_000, buffer_m)))

    largo = corridor_length_km(pts)
    if largo > MAX_CORRIDOR_KM:
        raise ValueError(
            f"El trayecto mide {largo:,.0f} km y el tope es {MAX_CORRIDOR_KM:,.0f}. "
            "Pártelo en tramos.")
    area = corridor_area_km2(corridor_polygon(list(pts), buffer_m))
    if area > MAX_CORRIDOR_AREA_KM2:
        raise ValueError(
            f"La franja abarca {area:,.0f} km² y el tope es "
            f"{MAX_CORRIDOR_AREA_KM2:,.0f}. Reduce el ancho o acorta el trayecto.")

    lat = sum(p[0] for p in pts) / len(pts)
    lon = sum(p[1] for p in pts) / len(pts)
    spec = RegionSpec(id=region_id, label=label, lat=lat, lon=lon,
                      radius_m=int(max(2000, largo * 1000 / 2)), synthetic=False,
                      group=CORRIDOR_GROUP, corridor=pts, buffer_m=buffer_m)
    REGIONS[region_id] = spec
    _save_custom()
    return spec


def remove_custom_region(region_id: str) -> bool:
    """Borra una región a medida y su grafo. Las curadas no se tocan."""
    spec = REGIONS.get(region_id)
    if spec is None or spec.group not in (CUSTOM_GROUP, CORRIDOR_GROUP):
        return False
    REGIONS.pop(region_id, None)
    _MEMORY.pop(region_id, None)
    p = graph_path(region_id)
    if p.exists():
        p.unlink()
    _save_custom()
    return True


def graph_path(region_id: str) -> Path:
    return GRAPH_DIR / f"{region_id}.pkl"


def build_region(region_id: str, elevation: str = "auto", dem_path: str | None = None,
                 force: bool = False, progress=None) -> RaceGraph:
    """Materializa una región: descarga OSM (o sintetiza), hornea elevación, cachea.

    `progress(pct, mensaje)` se invoca con el porcentaje global 0–100 para que la
    interfaz pueda mostrar una barra real en vez de un spinner indefinido.
    """
    def report(pct: float, msg: str) -> None:
        if progress:
            try:
                progress(max(0.0, min(100.0, pct)), msg)
            except Exception:  # noqa: BLE001
                pass
    spec = REGIONS.get(region_id)
    if spec is None:
        raise KeyError(f"Región desconocida: {region_id}. Disponibles: {sorted(REGIONS)}")

    path = graph_path(region_id)
    if path.exists() and not force:
        print(f"[regions] «{spec.label}» ya construida ({path}). Usa --force para rehacerla.")
        return RaceGraph.load(path)

    t0 = time.time()
    if spec.synthetic:
        from .synthetic import build_synthetic_region
        report(10, "generando ciudad sintética")
        print(f"[regions] generando región sintética «{spec.label}»…")
        g = build_synthetic_region(spec.lat, spec.lon, extent_m=float(spec.radius_m))
        report(95, "guardando")
    else:
        from .osm import build_graph_from_osm, download_corridor, download_radius
        if spec.is_corridor:
            report(3, "descargando la franja del trayecto (puede tardar)")
            data = download_corridor(list(spec.corridor), spec.buffer_m, CACHE_DIR)
        else:
            report(3, "descargando OpenStreetMap (puede tardar unos minutos)")
            data = download_radius(spec.lat, spec.lon, spec.radius_m, CACHE_DIR)
        report(25, "construyendo el grafo vial")
        g = build_graph_from_osm(data, spec.lat, spec.lon,
                                 version=f"{region_id}@{time.strftime('%Y-%m-%d')}")
        report(35, "grafo listo")

        kind = elevation
        if kind == "auto":
            kind = "local_dem" if dem_path else "opentopodata"

        def elev_progress(done: int, total: int, msg: str) -> None:
            # La elevación ocupa del 35% al 92% de la barra global.
            frac = done / max(total, 1)
            report(35 + 57 * frac, msg)

        provider = make_provider(kind, CACHE_DIR, dem_path, progress=elev_progress)
        if provider.name != "null":
            print(f"[regions] muestreando elevación con «{provider.name}»…")
            elev = provider.sample(g.lat, g.lon)
            bake_elevation(g, elev)
            lvl = "reliable" if provider.name == "local_dem" else "partial"
            g.coverage["elevation"] = {
                "covered": g.n_nodes, "total": g.n_nodes, "pct": 100.0, "level": lvl,
                "source": provider.attribution,
            }
        else:
            g.coverage["elevation"] = {
                "covered": 0, "total": g.n_nodes, "pct": 0.0, "level": "deferred",
                "source": "",
            }

    # Conectividad: ¿ha llegado el mapa entero o a trozos? Una descarga
    # incompleta no parece un error, parece un mapa, y el usuario lo descubre al
    # ver un recorrido que se corta. Se mide aquí y se guarda con la cobertura.
    report(94, "comprobando la conectividad del mapa")
    from .graph import connectivity

    con = connectivity(g)
    g.coverage["connectivity"] = {
        "covered": con["largest"], "total": g.n_nodes,
        "pct": con["largest_pct"], "level": con["level"],
        "components": con["components"],
    }
    if con["level"] != "reliable":
        print(f"[regions] ⚠ el mapa llegó en {con['components']} trozos; el mayor "
              f"cubre el {con['largest_pct']:.1f} % de los nodos")

    report(96, "guardando el grafo")
    g.save(path)
    _MEMORY[region_id] = g
    report(100, "listo")
    print(f"[regions] «{spec.label}» lista en {time.time() - t0:.1f}s → {path}")
    print(f"          {g.n_nodes:,} nodos · {g.n_edges:,} aristas · "
          f"desnivel {float(np.ptp(g.elev)):.0f} m de rango")
    return g


MULTI_SEP = "+"


def region_parts(region_id: str) -> list[str]:
    """«las_condes+nunoa» → ['las_condes', 'nunoa']. Ordenado y sin repetidos."""
    parts = [p for p in region_id.split(MULTI_SEP) if p]
    return sorted(dict.fromkeys(parts))


def load_region(region_id: str) -> RaceGraph:
    """Carga desde memoria si es posible. Los workers la pre-calientan al arrancar."""
    if region_id in _MEMORY:
        return _MEMORY[region_id]

    # Región compuesta: se unen los grafos de las partes en uno solo. Es lo que
    # permite trazar un recorrido que cruza tres comunas sin que el motor tenga
    # que fingir que las otras dos no existen.
    parts = region_parts(region_id)
    if len(parts) > 1:
        from .graph import merge_graphs

        g = merge_graphs([load_region(p) for p in parts],
                         version=MULTI_SEP.join(parts))
        _MEMORY[region_id] = g
        return g
    region_id = parts[0] if parts else region_id
    if region_id in _MEMORY:
        return _MEMORY[region_id]

    path = graph_path(region_id)
    if not path.exists():
        if REGIONS.get(region_id) and REGIONS[region_id].synthetic:
            return build_region(region_id)
        raise FileNotFoundError(
            f"La región «{region_id}» no está construida.\n"
            f"Ejecuta:  python -m raceengine_geodata.cli build {region_id}"
        )
    g = RaceGraph.load(path)
    _MEMORY[region_id] = g
    return g


def available_regions() -> list[dict]:
    out = []
    for spec in REGIONS.values():
        built = graph_path(spec.id).exists() or spec.id in _MEMORY
        out.append({
            "id": spec.id, "label": spec.label, "lat": spec.lat, "lon": spec.lon,
            "radius_m": spec.radius_m, "built": built, "synthetic": spec.synthetic,
            "group": spec.group,
            "custom": spec.group in (CUSTOM_GROUP, CORRIDOR_GROUP),
            "corridor": [list(p) for p in spec.corridor],
            "buffer_m": spec.buffer_m,
        })
    return out


# Las regiones a medida guardadas en disco se registran al importar el módulo.
_load_custom()
