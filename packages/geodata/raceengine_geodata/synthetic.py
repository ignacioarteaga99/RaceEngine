"""Generador de región sintética.

Existe por dos razones prácticas:
  1. Permite arrancar la app y probar toda la UI SIN esperar una descarga de
     Overpass ni tener conexión.
  2. Da un grafo determinista y conocido para los tests del motor, de modo que la
     CI no depende de la red (algo que el MVP no tenía).

Genera una retícula urbana irregular con avenidas, un parque, un cerro y algunos
hospitales — suficiente para que las 4 dimensiones de scoring tengan señal real.
"""
from __future__ import annotations

import math

import numpy as np

from .graph import GraphBuilder, RaceGraph, bake_elevation, compute_coverage


def build_synthetic_region(
    center_lat: float = -33.4372,
    center_lon: float = -70.6506,
    extent_m: float = 9000.0,
    block_m: float = 130.0,
    seed: int = 20260814,
) -> RaceGraph:
    """Retícula tipo damero con jitter, avenidas cada 8 manzanas y relieve."""
    rng = np.random.default_rng(seed)
    n = int(extent_m / block_m)
    if n % 2 == 0:
        n += 1
    half = n // 2

    b = GraphBuilder(center_lat, center_lon)
    k = math.cos(math.radians(center_lat))

    def to_latlon(px: float, py: float) -> tuple[float, float]:
        return (
            center_lat + math.degrees(py / 6371008.8),
            center_lon + math.degrees(px / (6371008.8 * k)),
        )

    # Posiciones con jitter para que las calles no sean perfectamente rectas.
    xs = np.array([(i - half) * block_m for i in range(n)], dtype=float)
    ys = np.array([(j - half) * block_m for j in range(n)], dtype=float)
    jx = rng.normal(0, block_m * 0.10, size=(n, n))
    jy = rng.normal(0, block_m * 0.10, size=(n, n))

    def nid(i: int, j: int) -> int:
        return i * 10_000 + j

    coords: dict[int, tuple[float, float, float, float]] = {}
    for i in range(n):
        for j in range(n):
            px = xs[i] + jx[i, j]
            py = ys[j] + jy[i, j]
            lat, lon = to_latlon(px, py)
            coords[nid(i, j)] = (lat, lon, px, py)
            b.add_node(nid(i, j), lat, lon)

    # Un parque cuadrado y un "cerro" gaussiano descentrado.
    park_x0, park_x1 = -2.0 * block_m * 6, 1.0 * block_m * 6
    park_y0, park_y1 = 2.0 * block_m * 5, 5.0 * block_m * 5
    hill_cx, hill_cy, hill_sigma, hill_h = 2600.0, -2100.0, 1500.0, 190.0

    def in_park(px: float, py: float) -> bool:
        return park_x0 <= px <= park_x1 and park_y0 <= py <= park_y1

    def street_class(i: int, j: int, axis: str) -> str:
        major = (i % 8 == 0) if axis == "v" else (j % 8 == 0)
        mid = (i % 4 == 0) if axis == "v" else (j % 4 == 0)
        if major:
            return "primary" if ((i if axis == "v" else j) % 16 == 0) else "secondary"
        if mid:
            return "tertiary"
        return "residential"

    def add(i1, j1, i2, j2, axis):
        a, c = nid(i1, j1), nid(i2, j2)
        la, loa, pxa, pya = coords[a]
        lb, lob, pxb, pyb = coords[c]
        length = math.hypot(pxb - pxa, pyb - pya)
        hw = street_class(i1, j1, axis)
        park = in_park((pxa + pxb) / 2, (pya + pyb) / 2)
        if park:
            hw = "pedestrian" if rng.random() < 0.5 else "residential"
        lit = hw in ("primary", "secondary", "tertiary") or rng.random() < 0.45
        b.add_edge(a, c, length, hw, paved=True, lit=lit, park=park,
                   private=False, tunnel=False)

    for i in range(n):
        for j in range(n):
            # Se omiten ~4% de tramos para crear callejones sin salida realistas.
            if i + 1 < n and rng.random() > 0.04:
                add(i, j, i + 1, j, "h")
            if j + 1 < n and rng.random() > 0.04:
                add(i, j, i, j + 1, "v")

    # Semáforos en las intersecciones de vías mayores.
    for i in range(0, n, 4):
        for j in range(0, n, 4):
            b.signals.add(nid(i, j))

    g = b.build(version=f"synthetic@{seed}")

    # Relieve: cerro gaussiano + pendiente general suave.
    elev = (
        hill_h * np.exp(-((g.x - hill_cx) ** 2 + (g.y - hill_cy) ** 2) / (2 * hill_sigma ** 2))
        + 0.004 * g.y
        + 520.0
    )
    bake_elevation(g, elev.astype(np.float32))

    # Tres hospitales repartidos.
    for hx, hy in ((-2200.0, 1800.0), (2400.0, 900.0), (300.0, -2900.0)):
        lat, lon = g.unproject(hx, hy)
        idx = g.nearest_node(lat, lon)
        if idx >= 0:
            g.is_hospital[idx] = True

    total = g.n_edges // 2
    g.coverage = compute_coverage(g, {
        "surface": (total, total),
        "lit": (int(g.elit.sum()) // 2, total),
        "elevation": (total, total),
        "width": (0, total),
        "smoothness": (0, total),
    })
    return g
