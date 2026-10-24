"""Cuadrícula mundial: un grafo por celda, para construir sólo lo que se pide.

El catálogo de `regions.py` es curado (comunas, ciudades). Para el resto del
mundo no hay catálogo: el planeta se divide en celdas de ~8 km de lado y cada
celda es un círculo de 8 km de radio alrededor de su centro. Cualquier punto
de la celda queda a 2,3 km o más del borde de su grafo (8 − 5,66 de media
diagonal), lo justo para que una ruta salga para cualquier lado.

Las celdas se nombran `g<fila>_<columna>` y se calculan, no se guardan: el id
dice dónde está la celda, así que construir `g1234_5678` no necesita más datos
que el id. Las filas son franjas de latitud de 8 km; cada fila tiene tantas
columnas como quepan de 8 km en su paralelo (menos cerca de los polos).
"""
from __future__ import annotations

import math
import re

LADO_M = 8000
RADIO_M = 8000
GRUPO = "Zonas"
_M_POR_GRADO = 111_195.0
_PASO_LAT = LADO_M / _M_POR_GRADO
_ID = re.compile(r"^g(\d+)_(\d+)$")


def _columnas(fila: int) -> int:
    lat = -90.0 + (fila + 0.5) * _PASO_LAT
    return max(1, int(360.0 * math.cos(math.radians(lat)) * _M_POR_GRADO / LADO_M))


def celda(lat: float, lon: float) -> str:
    """El id de la celda que contiene el punto."""
    fila = min(int((lat + 90.0) / _PASO_LAT), int(180.0 / _PASO_LAT))
    n = _columnas(fila)
    col = int((lon + 180.0) / (360.0 / n)) % n
    return f"g{fila}_{col}"


def es_celda(region_id: str) -> bool:
    return bool(_ID.match(region_id or ""))


def centro(region_id: str) -> tuple[float, float]:
    m = _ID.match(region_id)
    if not m:
        raise KeyError(f"«{region_id}» no es una celda de la cuadrícula")
    fila, col = int(m[1]), int(m[2])
    n = _columnas(fila)
    if col >= n:
        raise KeyError(f"«{region_id}»: la fila {fila} tiene {n} columnas")
    return (round(-90.0 + (fila + 0.5) * _PASO_LAT, 5),
            round(-180.0 + (col + 0.5) * 360.0 / n, 5))


def spec(region_id: str, label: str | None = None):
    """La región de una celda, lista para `build_region`."""
    from .regions import RegionSpec
    lat, lon = centro(region_id)
    return RegionSpec(region_id, label or f"Zona {lat:.2f}, {lon:.2f}", lat, lon, RADIO_M, group=GRUPO)


def registrar(region_id: str, label: str | None = None):
    """Agrega la celda al catálogo en memoria (si no estaba) y la devuelve."""
    from .regions import REGIONS
    if region_id not in REGIONS:
        REGIONS[region_id] = spec(region_id, label)
    return REGIONS[region_id]
