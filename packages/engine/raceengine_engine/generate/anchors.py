"""Muestreo de anclas — el corazón de la generación *coverage-first*.

El MVP hacía *shape-first*: elegía una figura (`box`, `diamond`, `poly7`), la
rotaba y escalaba, y forzaba los POIs obligatorios dentro. Eso produce el defecto
que su propio código comentaba ("mandatory POIs behaving as route polygon
vertices") y un barrido combinatorio de 2.880 semillas.

Aquí se parte del ÁREA que el circuito debe encerrar, se muestrean anclas sobre la
red realmente alcanzable, y los obligatorios se insertan después como reparación.
"""
from __future__ import annotations

import math

import numpy as np

from raceengine_geodata import RaceGraph


def target_ring_radius(length_m: float, compactness: float) -> float:
    """Radio del anillo de anclas para un circuito de longitud `length_m`.

    Para un círculo perfecto de perímetro L: r = L / 2π, y el área encerrada es
    A = L²/4π. `compactness` ∈ (0,1] reduce el área respecto al círculo ideal:
    una forma más irregular encierra menos área con el mismo perímetro.
    """
    c = max(0.15, min(1.0, compactness))
    return (length_m / (2 * math.pi)) * math.sqrt(c)


def anchor_count(length_m: float) -> int:
    """Un ancla cada ~3 km, acotado a [3, 12]."""
    return int(max(3, min(12, round(length_m / 3000.0))))


def sample_ring_anchors(
    g: RaceGraph,
    start_node: int,
    reach: np.ndarray,
    length_m: float,
    compactness: float,
    n_anchors: int,
    rotation: float,
    rng: np.random.Generator,
    jitter: float = 0.16,
) -> list[int]:
    """Devuelve nodos-ancla repartidos angularmente alrededor del inicio.

    `reach` es el vector de distancias reales por calle desde el inicio (no radio
    euclídeo): así las anclas nunca caen al otro lado de un río o una autopista.
    """
    r = target_ring_radius(length_m, compactness)
    sx, sy = float(g.x[start_node]), float(g.y[start_node])

    # Candidatos: intersecciones alcanzables dentro de la banda del anillo.
    reachable = np.isfinite(reach)
    d_euclid = np.sqrt((g.x - sx) ** 2 + (g.y - sy) ** 2)
    band = (d_euclid >= r * 0.62) & (d_euclid <= r * 1.42)
    cand = np.nonzero(reachable & band & g.is_intersection)[0]

    if len(cand) < n_anchors:
        band = (d_euclid >= r * 0.40) & (d_euclid <= r * 1.75)
        cand = np.nonzero(reachable & band)[0]
    if len(cand) == 0:
        cand = np.nonzero(reachable)[0]
    if len(cand) == 0:
        return []

    ang = np.arctan2(g.y[cand] - sy, g.x[cand] - sx)
    anchors: list[int] = []
    used: set[int] = set()

    for k in range(n_anchors):
        theta = rotation + 2 * math.pi * k / n_anchors
        theta += float(rng.normal(0.0, jitter))
        # El mejor candidato es el que mejor combina rumbo y radio deseados.
        dang = np.abs(np.arctan2(np.sin(ang - theta), np.cos(ang - theta)))
        dr = np.abs(d_euclid[cand] - r) / max(r, 1.0)
        cost = dang + 1.15 * dr
        order = np.argsort(cost)
        for oi in order[:60]:
            node = int(cand[oi])
            if node in used or node == start_node:
                continue
            # Separación mínima entre anclas para evitar bucles degenerados.
            too_close = any(
                math.hypot(float(g.x[node] - g.x[a]), float(g.y[node] - g.y[a]))
                < r * 0.35
                for a in anchors
            )
            if too_close:
                continue
            anchors.append(node)
            used.add(node)
            break

    return anchors


def farthest_point_refine(g: RaceGraph, anchors: list[int], k: int) -> list[int]:
    """Conserva las `k` anclas más separadas entre sí (muestreo de punto lejano)."""
    if len(anchors) <= k:
        return anchors
    chosen = [anchors[0]]
    remaining = anchors[1:]
    while len(chosen) < k and remaining:
        best, bestd = None, -1.0
        for a in remaining:
            d = min(
                math.hypot(float(g.x[a] - g.x[c]), float(g.y[a] - g.y[c]))
                for c in chosen
            )
            if d > bestd:
                best, bestd = a, d
        chosen.append(best)          # type: ignore[arg-type]
        remaining.remove(best)       # type: ignore[arg-type]
    return chosen


def order_ring(g: RaceGraph, start_node: int, anchors: list[int]) -> list[int]:
    """Ordena las anclas por rumbo desde el inicio: da un anillo sin cruces."""
    sx, sy = float(g.x[start_node]), float(g.y[start_node])
    return sorted(
        anchors,
        key=lambda a: math.atan2(float(g.y[a]) - sy, float(g.x[a]) - sx),
    )


def two_opt_ring(g: RaceGraph, start_node: int, ring: list[int],
                 max_passes: int = 4) -> list[int]:
    """2-opt sobre el ciclo start → ring → start usando distancia euclídea.

    Con n ≤ 12 anclas esto es trivial y evita depender de una Matrix API
    (que en GraphHopper es de licencia comercial — riesgo GH-R1, doc 11 §6.4).
    """
    if len(ring) < 4:
        return ring

    def d(a: int, b: int) -> float:
        return math.hypot(float(g.x[a] - g.x[b]), float(g.y[a] - g.y[b]))

    tour = [start_node] + list(ring) + [start_node]
    improved = True
    passes = 0
    while improved and passes < max_passes:
        improved = False
        passes += 1
        for i in range(1, len(tour) - 2):
            for j in range(i + 1, len(tour) - 1):
                a, b, c, e = tour[i - 1], tour[i], tour[j], tour[j + 1]
                if d(a, b) + d(c, e) > d(a, c) + d(b, e) + 1e-9:
                    tour[i: j + 1] = reversed(tour[i: j + 1])
                    improved = True
    return tour[1:-1]
