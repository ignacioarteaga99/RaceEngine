"""Distance Adjuster — lleva la longitud del circuito dentro de tolerancia.

El MVP reparaba con paseos aleatorios: `insert_detour` elegía una arista al azar,
un ángulo al azar y probaba 7 radios — hasta 112 pares de A* por iteración, hasta
80 iteraciones (doc 10 §1.4).

Aquí hay tres operadores DIRIGIDOS, cada uno con efecto predecible en signo, y la
reparación es transaccional: si un operador rompe la cobertura de obligatorios, se
revierte y se prueba el siguiente.
"""
from __future__ import annotations

import math
import time

import numpy as np

from raceengine_contracts import ConstraintModel
from raceengine_routing import RoutingWeights

from ..context import GraphContext
from ..generate.anchors import order_ring, target_ring_radius, two_opt_ring
from ..generate.loop_generator import RawLoop, route_chain


def _covers_all(nodes: list[int], masks: dict[str, np.ndarray]) -> bool:
    return all(any(m[n] for n in nodes) for m in masks.values()) if masks else True


def _bulge(ctx: GraphContext, ring: list[int], start_node: int, delta_m: float,
           reach: np.ndarray, rng: np.random.Generator,
           end_node: int | None = None) -> list[int] | None:
    """Circuito CORTO → insertar un waypoint desplazado lateralmente.

    Se elige el tramo ancla→ancla más largo y se busca un nodo alcanzable a
    ~delta/2 de distancia perpendicular a su punto medio.
    """
    g = ctx.graph
    if len(ring) < 2:
        return None
    chain = [start_node] + ring + [start_node if end_node is None else end_node]
    spans = [
        (math.hypot(float(g.x[a] - g.x[b]), float(g.y[a] - g.y[b])), i)
        for i, (a, b) in enumerate(zip(chain, chain[1:]))
    ]
    spans.sort(reverse=True)

    for _, i in spans[:3]:
        a, b = chain[i], chain[i + 1]
        mx, my = (float(g.x[a] + g.x[b]) / 2, float(g.y[a] + g.y[b]) / 2)
        dx, dy = float(g.x[b] - g.x[a]), float(g.y[b] - g.y[a])
        norm = math.hypot(dx, dy) or 1.0
        # Perpendicular unitaria.
        px, py = -dy / norm, dx / norm
        offset = max(150.0, abs(delta_m) * 0.42)
        for sign in (1.0, -1.0):
            tx, ty = mx + px * offset * sign, my + py * offset * sign
            lat, lon = g.unproject(tx, ty)
            cand = g.nearest_node(lat, lon, mask=np.isfinite(reach) & g.is_intersection)
            if cand < 0 or cand in ring or cand == start_node:
                continue
            pos = i if i < len(ring) else len(ring)
            return ring[:pos] + [cand] + ring[pos:]
    return None


def _protected(ring: list[int], masks: dict[str, np.ndarray]) -> set[int]:
    """Anclas que NO se pueden eliminar: las que materializan una zona obligatoria.

    Sin esto, `_chord` intentaba una y otra vez quitar el ancla de mayor desvío
    —que suele ser justamente la de una zona obligatoria—, la reparación
    transaccional lo rechazaba por romper la cobertura, y el circuito se quedaba
    atascado kilómetros por encima del objetivo. Es la causa del 42 km → 64 km.
    """
    out: set[int] = set()
    for m in masks.values():
        for a in ring:
            if m[a]:
                out.add(a)
    return out


def _chord(ctx: GraphContext, ring: list[int], start_node: int,
           protected: set[int] | None = None,
           end_node: int | None = None) -> list[int] | None:
    """Circuito LARGO → eliminar el ancla eliminable que más desvío añade."""
    g = ctx.graph
    if len(ring) <= 3:
        return None
    protected = protected or set()
    chain = [start_node] + ring + [start_node if end_node is None else end_node]

    def d(a: int, b: int) -> float:
        return math.hypot(float(g.x[a] - g.x[b]), float(g.y[a] - g.y[b]))

    best, best_gain = None, -math.inf
    for i in range(1, len(chain) - 1):
        if chain[i] in protected:
            continue
        gain = d(chain[i - 1], chain[i]) + d(chain[i], chain[i + 1]) - d(chain[i - 1], chain[i + 1])
        if gain > best_gain:
            best, best_gain = chain[i], gain
    if best is None:
        return None
    return [a for a in ring if a != best]


def _shrink_free_anchors(ctx: GraphContext, ring: list[int], start_node: int,
                         protected: set[int], factor: float,
                         reach: np.ndarray) -> list[int] | None:
    """Acerca al inicio SOLO las anclas libres, dejando fijas las obligatorias.

    Permite acortar un circuito que ya cubre todos los checkpoints sin tener que
    renunciar a ninguno.
    """
    g = ctx.graph
    sx, sy = float(g.x[start_node]), float(g.y[start_node])
    mask = np.isfinite(reach) & g.is_intersection
    out: list[int] = []
    for a in ring:
        if a in protected:
            out.append(a)
            continue
        dx, dy = float(g.x[a]) - sx, float(g.y[a]) - sy
        lat, lon = g.unproject(sx + dx * factor, sy + dy * factor)
        cand = g.nearest_node(lat, lon, mask=mask)
        out.append(cand if cand >= 0 and cand != start_node and cand not in out else a)
    return out if len(out) >= 3 else None


def _rescale(ctx: GraphContext, model: ConstraintModel, start_node: int,
             ring: list[int], factor: float, reach: np.ndarray,
             protected: set[int] | None = None) -> list[int] | None:
    """Re-escala radialmente el anillo, respetando las anclas obligatorias."""
    g = ctx.graph
    protected = protected or set()
    sx, sy = float(g.x[start_node]), float(g.y[start_node])
    mask = np.isfinite(reach) & g.is_intersection
    out: list[int] = []
    for a in ring:
        if a in protected:
            out.append(a)
            continue
        dx, dy = float(g.x[a]) - sx, float(g.y[a]) - sy
        lat, lon = g.unproject(sx + dx * factor, sy + dy * factor)
        cand = g.nearest_node(lat, lon, mask=mask)
        if cand >= 0 and cand != start_node and cand not in out:
            out.append(cand)
    return out if len(out) >= 3 else None


def adjust_distance(
    ctx: GraphContext,
    model: ConstraintModel,
    w: RoutingWeights,
    loop: RawLoop,
    start_node: int,
    reach: np.ndarray,
    masks: dict[str, np.ndarray],
    rng: np.random.Generator,
    budget_ms: int = 700,
    max_iter: int = 8,
    end_node: int | None = None,
) -> tuple[RawLoop, list[str]]:
    """Devuelve (circuito ajustado, operadores aplicados)."""
    _end = start_node if end_node is None else end_node
    t0 = time.perf_counter()
    target = model.distance.target_m
    tol = model.distance.tolerance_m
    best = loop
    best_err = abs(loop.distance_m - target)
    ops: list[str] = []
    ring = list(loop.anchors)
    current = loop
    stalls = 0
    protected = _protected(ring, masks)

    for _ in range(max_iter):
        if (time.perf_counter() - t0) * 1000 > budget_ms:
            break
        delta = target - current.distance_m
        if abs(delta) <= tol:
            break

        if stalls >= 3:
            factor = math.sqrt(max(0.25, min(4.0, target / max(current.distance_m, 1.0))))
            new_ring = _rescale(ctx, model, start_node, ring, factor, reach, protected)
            op = f"rescale×{factor:.2f}"
            stalls = 0
        elif stalls == 2 and delta < 0 and protected:
            # Atascado y largo con checkpoints fijos: encoger solo lo que se puede.
            factor = max(0.55, min(0.95, target / max(current.distance_m, 1.0)))
            new_ring = _shrink_free_anchors(ctx, ring, start_node, protected, factor, reach)
            op = f"shrink_free×{factor:.2f}"
        elif delta > 0:
            new_ring = _bulge(ctx, ring, start_node, delta, reach, rng, _end)
            op = "bulge"
        else:
            new_ring = _chord(ctx, ring, start_node, protected, _end)
            op = "chord"

        if not new_ring:
            stalls += 1
            continue

        if _end == start_node:
            new_ring = order_ring(ctx.graph, start_node, new_ring)
            new_ring = two_opt_ring(ctx.graph, start_node, new_ring)
        cand = route_chain(ctx, w, [start_node] + new_ring + [_end],
                           max_leg_cost=target * 0.9)
        if cand is None or not cand.ok:
            stalls += 1
            continue
        protected = _protected(new_ring, masks)

        # Transaccional: no se acepta un ajuste que rompa la cobertura.
        if not _covers_all(cand.nodes, masks):
            stalls += 1
            continue

        err = abs(cand.distance_m - target)
        if err < best_err - 1.0:
            cand.anchors = new_ring
            cand.generator = current.generator
            best, best_err = cand, err
            current, ring = cand, new_ring
            ops.append(op)
            stalls = 0
        else:
            stalls += 1

    best.generator = (best.generator or "") + ("+" + "+".join(ops) if ops else "")
    return best, ops
