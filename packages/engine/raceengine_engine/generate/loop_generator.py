"""Loop Generator — genera K circuitos factibles a partir de anillos de anclas.

Contrato: nunca devuelve vacío si el grafo lo permite. Su salida son loops
"crudos" (posiblemente fuera de tolerancia y sin cubrir obligatorios); de eso se
encargan Mandatory Repair y Distance Adjuster.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

from raceengine_contracts import ConstraintModel
from raceengine_geodata import RaceGraph
from raceengine_routing import RoutingWeights

from ..context import GraphContext
from .anchors import (
    anchor_count, order_ring, sample_ring_anchors, two_opt_ring,
)


@dataclass
class RawLoop:
    """Circuito crudo: secuencia de nodos y aristas, más el anillo que lo generó."""

    nodes: list[int] = field(default_factory=list)
    edges: list[int] = field(default_factory=list)
    anchors: list[int] = field(default_factory=list)
    distance_m: float = 0.0
    generator: str = ""

    @property
    def ok(self) -> bool:
        return len(self.nodes) > 3


def route_chain(ctx: GraphContext, w: RoutingWeights, chain: list[int],
                reuse_penalty: float = 0.65,
                max_leg_cost: float | None = None,
                penalty0: dict[int, float] | None = None) -> RawLoop | None:
    """Rutea una cadena de nodos penalizando (no prohibiendo) reutilizar aristas.

    Diferencia importante con el MVP: allí `HARD_AVOID_USED_NODES=True` prohibía
    todos los nodos ya usados, lo que hacía fallar A* con frecuencia, disparaba un
    reintento en modo blando y duplicaba el trabajo (doc 10 §3.5). Aquí la
    reutilización es CARA, no imposible: la búsqueda siempre encuentra algo.
    """
    nodes: list[int] = [chain[0]]
    edges: list[int] = []
    # `penalty0` permite arrancar con aristas ya encarecidas. Es lo que hace
    # posible pedir una VARIANTE: la misma cadena, pero evitando lo ya propuesto.
    # Antes esto no se podía inyectar desde fuera porque la primera línea del
    # bucle machacaba `w.penalty` con el diccionario vacío de aquí.
    penalty: dict[int, float] = dict(penalty0 or {})
    g = ctx.graph

    for a, b in zip(chain, chain[1:]):
        w.penalty = penalty
        leg = ctx.provider.shortest_path(a, b, w, max_distance_m=max_leg_cost)
        if leg is None or not leg.ok:
            w.penalty = {}
            return None
        nodes.extend(leg.nodes[1:])
        edges.extend(leg.edges)
        for e in leg.edges:
            extra = reuse_penalty * float(g.elen[e])
            penalty[e] = penalty.get(e, 0.0) + extra
            # También la arista inversa: reutilizar en sentido contrario es
            # igual de indeseable en un circuito.
            rev = _reverse_edge(g, e)
            if rev >= 0:
                penalty[rev] = penalty.get(rev, 0.0) + extra

    w.penalty = {}
    loop = RawLoop(nodes=nodes, edges=edges)
    loop.distance_m = float(g.elen[edges].sum()) if edges else 0.0
    return loop


def _reverse_edge(g: RaceGraph, e: int) -> int:
    """Índice de la arista inversa. La tabla vive en el propio grafo."""
    r = int(g.reverse[e])
    return -1 if r == int(e) else r


def mandatory_seed_anchors(ctx: GraphContext, masks: dict, reach: np.ndarray,
                           start_node: int) -> list[int]:
    """Un ancla por zona obligatoria: el nodo alcanzable más «central» de la zona.

    Se prefiere una intersección — un ancla en mitad de una manzana obliga a entrar
    y salir por el mismo sitio.
    """
    g = ctx.graph
    out: list[int] = []
    for mask in masks.values():
        ok = mask & np.isfinite(reach)
        cand = np.nonzero(ok & g.is_intersection)[0]
        if not len(cand):
            cand = np.nonzero(ok)[0]
        if not len(cand):
            continue
        # El más cercano al centroide de los nodos utilizables de la zona.
        cx, cy = float(g.x[cand].mean()), float(g.y[cand].mean())
        d2 = (g.x[cand] - cx) ** 2 + (g.y[cand] - cy) ** 2
        node = int(cand[int(np.argmin(d2))])
        if node != start_node and node not in out:
            out.append(node)
    return out


def generate_loops(
    ctx: GraphContext,
    model: ConstraintModel,
    w: RoutingWeights,
    start_node: int,
    reach: np.ndarray,
    rng: np.random.Generator,
    k: int = 10,
    budget_ms: int = 1200,
    must: list[int] | None = None,
) -> list[RawLoop]:
    """Genera hasta `k` circuitos diversos. Se detiene al agotar el presupuesto.

    `must` son anclas que TODOS los anillos deben contener: una por zona
    obligatoria. Es la diferencia entre generar y reparar.

    Antes las zonas obligatorias no participaban en la generación: se producían
    anillos por muestreo de la isócrona y después Mandatory Repair intentaba
    insertarlas una a una. Con una zona funciona; con cinco repartidas por una
    ciudad grande, no: cada inserción deforma el anillo, el presupuesto se agota
    y el resultado sale con la mitad de las zonas sin cubrir. Sembrando el anillo
    con las anclas obligatorias la cobertura es gratis desde el primer circuito,
    y a las etapas siguientes sólo les queda ajustar la longitud.
    """
    t0 = time.perf_counter()
    target = model.distance.target_m
    n_anchors = anchor_count(target)
    must = [int(m) for m in (must or []) if int(m) != start_node]
    loops: list[RawLoop] = []
    seen_signatures: set[tuple[int, ...]] = set()

    # Compacidad derivada de `geometry.smoothness`: más suavidad ⇒ forma más
    # redonda ⇒ más área encerrada con el mismo perímetro.
    base_compactness = 0.55 + 0.40 * model.geometry.smoothness

    attempts = 0
    max_leg = target * 0.9
    best_reject: RawLoop | None = None

    while len(loops) < k and attempts < k * 3:
        if (time.perf_counter() - t0) * 1000 > budget_ms and loops:
            break
        attempts += 1
        i = attempts - 1

        rotation = 2 * math.pi * (i % max(k, 1)) / max(k, 1)
        compactness = base_compactness * (1.0 + 0.13 * math.sin(i * 1.7))
        n_a = n_anchors + (0 if i % 3 == 0 else (1 if i % 3 == 1 else -1))
        n_a = max(3, min(12, n_a))

        # Las obligatorias van siempre; las libres rellenan hasta la forma pedida.
        n_free = max(0, n_a - len(must))
        anchors = list(must)
        if n_free:
            free = sample_ring_anchors(
                ctx.graph, start_node, reach, target, compactness, n_free, rotation, rng
            )
            anchors += [a for a in free if a not in anchors and a != start_node]
        if len(anchors) < 3:
            # Con una o dos zonas obligatorias el anillo se queda corto para
            # cerrar una forma: se completa con libres aunque n_a ya estuviera
            # cubierto por las obligatorias.
            extra = sample_ring_anchors(
                ctx.graph, start_node, reach, target, compactness,
                3 - len(anchors) + 1, rotation, rng
            )
            anchors += [a for a in extra if a not in anchors and a != start_node]
        if len(anchors) < 3:
            continue
        ring = two_opt_ring(ctx.graph, start_node, order_ring(ctx.graph, start_node, anchors))

        sig = tuple(sorted(ring))
        if sig in seen_signatures:
            continue
        seen_signatures.add(sig)

        loop = route_chain(ctx, w, [start_node] + ring + [start_node],
                           max_leg_cost=max_leg)
        if loop is None or not loop.ok:
            continue
        loop.anchors = ring
        loop.generator = f"ring/rot{i}/c{compactness:.2f}/n{len(ring)}"

        # Descarte barato: solo lo irreparable. Con anclas obligatorias el margen
        # es mayor, porque son ellas las que fijan la forma y el ajustador de
        # distancia tendrá que trabajar más.
        slack = 0.60 if must else 0.45
        if abs(loop.distance_m - target) > target * slack:
            if best_reject is None or (abs(loop.distance_m - target)
                                       < abs(best_reject.distance_m - target)):
                best_reject = loop
            continue

        loops.append(loop)

    # El contrato dice que esta etapa no devuelve vacío si el grafo lo permite:
    # más vale un circuito con la longitud mal que ninguno. Las etapas siguientes
    # sí saben arreglar la longitud; lo que no saben es inventarse un circuito.
    if not loops and best_reject is not None:
        loops.append(best_reject)
    return loops



# ══════════════════════════════════════════════════════════════════════════════
# Recorrido ABIERTO (inicio ≠ meta) — doc 04 L2
# ══════════════════════════════════════════════════════════════════════════════
def _arc_length_factor(amplitude_ratio: float, samples: int = 64) -> float:
    """Longitud de una sinusoide de amplitud relativa `a`, en unidades de cuerda.

    La trayectoria es  y(t) = a·L·sin(πt)  sobre  x(t) = L·t,  con t ∈ [0,1].
    Devuelve  longitud_arco / L  — siempre ≥ 1.
    """
    total = 0.0
    prev = (0.0, 0.0)
    for i in range(1, samples + 1):
        t = i / samples
        cur = (t, amplitude_ratio * math.sin(math.pi * t))
        total += math.hypot(cur[0] - prev[0], cur[1] - prev[1])
        prev = cur
    return total


def solve_amplitude(chord_m: float, target_m: float) -> float:
    """Amplitud relativa que hace que el arco mida `target_m` sobre una cuerda dada.

    Bisección: `_arc_length_factor` es monótona creciente en la amplitud.
    """
    need = target_m / max(chord_m, 1.0)
    if need <= 1.02:
        return 0.0
    lo, hi = 0.0, 2.5
    for _ in range(40):
        mid = (lo + hi) / 2
        if _arc_length_factor(mid) < need:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def generate_open_routes(
    ctx,
    model,
    w: RoutingWeights,
    start_node: int,
    finish_node: int,
    reach: np.ndarray,
    rng: np.random.Generator,
    k: int = 8,
    budget_ms: int = 1200,
) -> list[RawLoop]:
    """Genera recorridos abiertos A → B con la longitud objetivo.

    En vez de un anillo, las anclas se reparten sobre un arco sinusoidal entre
    inicio y meta: la cuerda fija los extremos y la amplitud aporta la distancia
    que falta. La amplitud se resuelve analíticamente en vez de tantear.
    """
    t0 = time.perf_counter()
    g = ctx.graph
    target = model.distance.target_m

    sx, sy = float(g.x[start_node]), float(g.y[start_node])
    fx, fy = float(g.x[finish_node]), float(g.y[finish_node])
    chord = math.hypot(fx - sx, fy - sy)
    if chord < 1.0:
        return []

    # Vector unitario a lo largo de la cuerda y su perpendicular.
    ux, uy = (fx - sx) / chord, (fy - sy) / chord
    px, py = -uy, ux

    amp = solve_amplitude(chord, target * 0.92)   # el resto lo afina el ajustador
    n_anchors = int(max(2, min(10, round(target / 3500.0))))
    mask = np.isfinite(reach) & g.is_intersection
    loops: list[RawLoop] = []
    seen: set[tuple[int, ...]] = set()

    for i in range(k * 2):
        if len(loops) >= k or (time.perf_counter() - t0) * 1000 > budget_ms:
            break
        sign = 1.0 if i % 2 == 0 else -1.0
        scale = 1.0 + 0.10 * ((i // 2) - k / 4) / max(k / 4, 1)
        a = amp * scale

        chain: list[int] = [start_node]
        for j in range(1, n_anchors + 1):
            tt = j / (n_anchors + 1)
            off = a * chord * math.sin(math.pi * tt) * sign
            cx = sx + ux * chord * tt + px * off
            cy = sy + uy * chord * tt + py * off
            lat, lon = g.unproject(cx, cy)
            node = g.nearest_node(lat, lon, mask=mask)
            if node >= 0 and node not in chain:
                chain.append(node)
        chain.append(finish_node)
        if len(chain) < 3:
            continue

        sig = tuple(chain)
        if sig in seen:
            continue
        seen.add(sig)

        lp = route_chain(ctx, w, chain, max_leg_cost=target * 0.9)
        if lp is None or not lp.ok:
            continue
        if abs(lp.distance_m - target) > target * 0.55:
            continue
        lp.anchors = chain[1:-1]
        lp.generator = f"arc/a{a:.2f}/{'+' if sign > 0 else '-'}/n{len(lp.anchors)}"
        loops.append(lp)

    return loops
