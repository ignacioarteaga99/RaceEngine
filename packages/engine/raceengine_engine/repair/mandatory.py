"""Mandatory Repair — inserta las zonas obligatorias en un circuito ya formado.

Inversión de orden respecto al MVP: allí los POIs obligatorios eran VÉRTICES de la
figura generadora, lo que deformaba el circuito y provocaba pasadas en pivote.
Aquí el circuito se genera primero por su calidad geométrica, y la obligatoriedad
se resuelve después como una reparación local, eligiendo el operador según la
distancia del circuito a la zona (doc 11 §3.4).
"""
from __future__ import annotations

import math

import numpy as np

from raceengine_contracts import ConstraintModel, CustomZone
from raceengine_routing import RoutingWeights

from ..context import GraphContext
from ..generate.anchors import order_ring, two_opt_ring
from ..generate.loop_generator import RawLoop, route_chain
from ..weights import node_mask_for_zone

SNAP_M = 300.0        # el circuito ya roza la zona → mover un ancla
DETOUR_M = 2000.0     # cerca → insertar un waypoint
# más lejos → reanclar (sustituir el ancla menos útil)


def zone_masks(ctx: GraphContext, model: ConstraintModel) -> dict[str, np.ndarray]:
    out: dict[str, np.ndarray] = {}
    for z in model.mandatory_zones:
        out[z.id or z.name] = node_mask_for_zone(ctx.graph, z)
    return out


def _zone_nodes(ctx: GraphContext, mask: np.ndarray, reach: np.ndarray) -> np.ndarray:
    """Nodos utilizables dentro de la zona, preferentemente intersecciones."""
    g = ctx.graph
    ok = mask & np.isfinite(reach)
    inter = np.nonzero(ok & g.is_intersection)[0]
    return inter if len(inter) else np.nonzero(ok)[0]


def _closest_pair(g, nodes_a: np.ndarray, path_nodes: list[int]) -> tuple[int, int, float]:
    """(nodo de zona, nodo del circuito, distancia) más cercanos entre sí."""
    if len(nodes_a) == 0 or not path_nodes:
        return -1, -1, math.inf
    pn = np.array(path_nodes, dtype=np.int64)
    px, py = g.x[pn], g.y[pn]
    best = (-1, -1, math.inf)
    for z in nodes_a[: min(len(nodes_a), 400)]:
        d2 = (px - g.x[z]) ** 2 + (py - g.y[z]) ** 2
        j = int(np.argmin(d2))
        d = math.sqrt(float(d2[j]))
        if d < best[2]:
            best = (int(z), int(pn[j]), d)
    return best


def _best_insert_position(g, ring: list[int], node: int) -> int:
    """Posición del anillo donde insertar `node` con el menor desvío añadido."""
    if not ring:
        return 0

    def d(a: int, b: int) -> float:
        return math.hypot(float(g.x[a] - g.x[b]), float(g.y[a] - g.y[b]))

    best_pos, best_cost = 0, math.inf
    for i in range(len(ring)):
        a = ring[i]
        b = ring[(i + 1) % len(ring)]
        cost = d(a, node) + d(node, b) - d(a, b)
        if cost < best_cost:
            best_pos, best_cost = i + 1, cost
    return best_pos


def repair_mandatory(
    ctx: GraphContext,
    model: ConstraintModel,
    w: RoutingWeights,
    loop: RawLoop,
    start_node: int,
    reach: np.ndarray,
    masks: dict[str, np.ndarray],
    budget_ms: int = 800,
    end_node: int | None = None,
) -> tuple[RawLoop, list[str]]:
    """Devuelve (circuito reparado, nombres de zonas que siguen sin cubrirse)."""
    import time

    _end = start_node if end_node is None else end_node
    t0 = time.perf_counter()
    g = ctx.graph
    zones = model.mandatory_zones
    if not zones:
        return loop, []

    ring = list(loop.anchors)
    current = loop
    missing: list[str] = []
    target = model.distance.target_m

    for z in zones:
        if (time.perf_counter() - t0) * 1000 > budget_ms:
            missing.append(z.name or z.id)
            continue

        key = z.id or z.name
        mask = masks.get(key)
        if mask is None or not mask.any():
            missing.append(z.name or z.id)
            continue

        # ¿Ya está cubierta?
        if any(mask[n] for n in current.nodes):
            continue

        znodes = _zone_nodes(ctx, mask, reach)
        if len(znodes) == 0:
            missing.append(z.name or z.id)
            continue

        zn, _pn, dist = _closest_pair(g, znodes, current.nodes)
        if zn < 0:
            missing.append(z.name or z.id)
            continue

        # Elección del operador según la distancia del circuito a la zona.
        if dist <= SNAP_M and ring:
            op = "snap"
            j = min(range(len(ring)),
                    key=lambda i: math.hypot(float(g.x[ring[i]] - g.x[zn]),
                                             float(g.y[ring[i]] - g.y[zn])))
            new_ring = list(ring)
            new_ring[j] = zn
        elif dist <= DETOUR_M or len(ring) >= 12:
            op = "detour"
            pos = _best_insert_position(g, ring, zn)
            new_ring = ring[:pos] + [zn] + ring[pos:]
        else:
            op = "reanchor"
            # Sustituye el ancla cuyo hueco angular es menos útil.
            pos = _best_insert_position(g, ring, zn)
            new_ring = ring[:pos] + [zn] + ring[pos:]
            if len(new_ring) > 12:
                sx, sy = float(g.x[start_node]), float(g.y[start_node])
                drop = min(
                    (a for a in new_ring if a != zn),
                    key=lambda a: math.hypot(float(g.x[a]) - sx, float(g.y[a]) - sy),
                )
                new_ring = [a for a in new_ring if a != drop]

        # Reordenar tras insertar: sin esto el anillo queda con las anclas en un
        # orden arbitrario y el circuito se vuelve una maraña que cruza la ciudad
        # varias veces. Es lo que producía recorridos de 64 km para objetivos de 42.
        if _end == start_node:
            new_ring = two_opt_ring(g, start_node, order_ring(g, start_node, new_ring))

        candidate = route_chain(ctx, w, [start_node] + new_ring + [_end],
                                max_leg_cost=target * 0.9)
        if candidate is None or not candidate.ok:
            missing.append(z.name or z.id)
            continue
        if not any(mask[n] for n in candidate.nodes):
            missing.append(z.name or z.id)
            continue

        candidate.anchors = new_ring
        candidate.generator = f"{current.generator}+{op}"
        current, ring = candidate, new_ring

    return current, missing
