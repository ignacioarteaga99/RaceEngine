"""Operadores de búsqueda local sobre el anillo de anclas.

Cada operador transforma un anillo en otro. El motor los aplica, re-rutea y acepta
solo si el score global mejora (monotonía del incumbente, doc 11 §3.7).

La selección de operadores usa un bandit por crédito: los que funcionan en esta
topología urbana se aplican más. Es lo que permite que el mismo motor generalice
entre una comuna de baja densidad y una retícula densa sin reconfigurarlo.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from raceengine_geodata import RaceGraph


@dataclass
class OperatorCredit:
    """Bandit sencillo: probabilidad proporcional al crédito acumulado."""

    names: list[str]
    credit: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for n in self.names:
            self.credit.setdefault(n, 1.0)

    def pick(self, rng: np.random.Generator) -> str:
        w = np.array([self.credit[n] for n in self.names], dtype=float)
        w = np.maximum(w, 0.05)
        return self.names[int(rng.choice(len(self.names), p=w / w.sum()))]

    def reward(self, name: str, amount: float = 1.0) -> None:
        self.credit[name] = min(12.0, self.credit.get(name, 1.0) + amount)

    def penalize(self, name: str, amount: float = 0.08) -> None:
        self.credit[name] = max(0.05, self.credit.get(name, 1.0) - amount)


def _reachable_intersections(g: RaceGraph, reach: np.ndarray) -> np.ndarray:
    return np.isfinite(reach) & g.is_intersection


def _movable(ring: list[int], protected: frozenset) -> list[int]:
    """Índices del anillo que un operador puede tocar.

    Las anclas que materializan una zona obligatoria NO se mueven ni se borran.
    Sin esta regla el bucle de mejora erosionaba la cobertura: cada `drop_anchor`
    o `shift_anchor` podía sacar un ancla de su zona, y como el criterio de
    aceptación mira primero el error de distancia, un candidato que arreglaba la
    longitud a costa de perder una zona entraba igual. El efecto observado era el
    peor posible: MÁS presupuesto daba PEOR cobertura.
    """
    return [i for i, a in enumerate(ring) if a not in protected]


def shift_anchor(g: RaceGraph, ring: list[int], start_node: int, reach: np.ndarray,
                 rng: np.random.Generator, radius_m: float = 500.0,
                 protected: frozenset = frozenset()) -> list[int] | None:
    """Mueve un ancla a una intersección cercana alcanzable."""
    libres = _movable(ring, protected)
    if not libres:
        return None
    i = libres[int(rng.integers(0, len(libres)))]
    a = ring[i]
    mask = _reachable_intersections(g, reach)
    near = g.grid.within(float(g.x[a]), float(g.y[a]), radius_m)
    near = [int(n) for n in near if mask[n] and n != start_node and n not in ring]
    if not near:
        return None
    out = list(ring)
    out[i] = near[int(rng.integers(0, len(near)))]
    return out


def swap_anchor_pair(g: RaceGraph, ring: list[int], start_node: int, reach: np.ndarray,
                     rng: np.random.Generator,
                     protected: frozenset = frozenset()) -> list[int] | None:
    """Invierte un subtramo del anillo (movimiento 2-opt aleatorio).

    Es seguro con anclas protegidas: reordena el anillo pero no cambia el conjunto
    de anclas, así que ninguna zona obligatoria se pierde.
    """
    if len(ring) < 4:
        return None
    i = int(rng.integers(0, len(ring) - 2))
    j = int(rng.integers(i + 2, len(ring)))
    out = list(ring)
    out[i:j] = list(reversed(out[i:j]))
    return out


def drop_anchor(g: RaceGraph, ring: list[int], start_node: int, reach: np.ndarray,
                rng: np.random.Generator,
                protected: frozenset = frozenset()) -> list[int] | None:
    """Elimina un ancla LIBRE: acorta y simplifica la forma."""
    libres = _movable(ring, protected)
    if len(ring) <= 3 or not libres:
        return None
    i = libres[int(rng.integers(0, len(libres)))]
    return [a for k, a in enumerate(ring) if k != i]


def add_anchor(g: RaceGraph, ring: list[int], start_node: int, reach: np.ndarray,
               rng: np.random.Generator,
               protected: frozenset = frozenset()) -> list[int] | None:
    """Inserta un ancla en el hueco más largo: alarga y da forma."""
    if len(ring) >= 12 or len(ring) < 2:
        return None
    chain = [start_node] + ring + [start_node]
    spans = [
        (math.hypot(float(g.x[a] - g.x[b]), float(g.y[a] - g.y[b])), i)
        for i, (a, b) in enumerate(zip(chain, chain[1:]))
    ]
    _, i = max(spans)
    a, b = chain[i], chain[i + 1]
    mx, my = (float(g.x[a] + g.x[b]) / 2, float(g.y[a] + g.y[b]) / 2)
    mask = _reachable_intersections(g, reach)
    near = g.grid.within(mx, my, 700.0)
    near = [int(n) for n in near if mask[n] and n != start_node and n not in ring]
    if not near:
        return None
    pos = min(i, len(ring))
    return ring[:pos] + [near[int(rng.integers(0, len(near)))]] + ring[pos:]


def avoid_hotspot(g: RaceGraph, ring: list[int], start_node: int, reach: np.ndarray,
                  rng: np.random.Generator,
                  protected: frozenset = frozenset()) -> list[int] | None:
    """Aleja el ancla LIBRE situada en la zona con más semáforos alrededor."""
    libres = _movable(ring, protected)
    if not libres:
        return None
    scores = []
    for i in libres:
        a = ring[i]
        near = g.grid.within(float(g.x[a]), float(g.y[a]), 350.0)
        scores.append(int(g.is_signal[near].sum()) if len(near) else 0)
    i = libres[int(np.argmax(scores))]
    if max(scores) == 0:
        return None
    a = ring[i]
    mask = _reachable_intersections(g, reach)
    near = g.grid.within(float(g.x[a]), float(g.y[a]), 800.0)
    cands = [int(n) for n in near if mask[n] and n != start_node and n not in ring]
    if not cands:
        return None
    best = min(cands, key=lambda n: int(g.is_signal[g.grid.within(
        float(g.x[n]), float(g.y[n]), 350.0)].sum()))
    out = list(ring)
    out[i] = best
    return out


def seek_flatter(g: RaceGraph, ring: list[int], start_node: int, reach: np.ndarray,
                 rng: np.random.Generator,
                 protected: frozenset = frozenset()) -> list[int] | None:
    """Mueve el ancla LIBRE más alta hacia una cota más baja (reduce desnivel)."""
    libres = _movable(ring, protected)
    if not libres or float(np.ptp(g.elev)) < 1.0:
        return None
    i = libres[int(np.argmax([g.elev[ring[k]] for k in libres]))]
    a = ring[i]
    mask = _reachable_intersections(g, reach)
    near = g.grid.within(float(g.x[a]), float(g.y[a]), 900.0)
    cands = [int(n) for n in near if mask[n] and n != start_node and n not in ring]
    if not cands:
        return None
    best = min(cands, key=lambda n: float(g.elev[n]))
    if float(g.elev[best]) >= float(g.elev[a]) - 1.0:
        return None
    out = list(ring)
    out[i] = best
    return out


OPERATORS = {
    "shift_anchor": shift_anchor,
    "swap_anchor_pair": swap_anchor_pair,
    "drop_anchor": drop_anchor,
    "add_anchor": add_anchor,
    "avoid_hotspot": avoid_hotspot,
    "seek_flatter": seek_flatter,
}
