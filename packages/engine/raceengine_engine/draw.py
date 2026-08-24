"""Modo creativo — trazado manual asistido, con las mismas reglas duras.

Qué es
──────
El usuario dibuja la ruta él mismo, clic a clic. Cada clic no traza una recta: se
engancha a la red vial y calcula el tramo de coste mínimo **bajo exactamente los
mismos pesos** que usa el generador automático. Es decir, el trazado manual no es
un lienzo libre sino una negociación con el grafo: si un tramo no es admisible, no
se puede dibujar.

Por qué el servidor decide y no el navegador
────────────────────────────────────────────
Las restricciones duras viven en `weights.build_weights`: superficies, zonas
prohibidas, pendiente máxima, radio de búsqueda, túneles, vías privadas. Duplicar
esa lógica en JavaScript sería garantizarse dos verdades distintas. El navegador
manda coordenadas; aquí se decide si el tramo existe.

Las tres reglas que bloquean el trazado
───────────────────────────────────────
1. **Admisibilidad**: si no hay camino admisible hasta el punto pedido, se rechaza.
   Esto cubre gratis las zonas prohibidas y la pendiente máxima.
2. **Distancia**: no basta con que el trazado quepa en la banda; tiene que caber
   CONTANDO EL REGRESO. Si añadir este tramo deja la ruta sin forma de volver a
   la salida sin pasarse del máximo, se rechaza antes de dibujarlo. Sin esto el
   usuario descubriría el problema al cerrar, con veinte clics ya invertidos.
3. **Desnivel**: si el desnivel positivo acumulado supera el máximo configurado,
   se rechaza.

El cierre exige además estar DENTRO de la banda de tolerancia, no solo por debajo
del máximo: una ruta de 7 km no es un 10K.

Puntuación con n = 1
────────────────────
DESVIACIÓN DOCUMENTADA respecto a doc 06 §4. La normalización aprobada usa cotas
RELATIVAS al conjunto de candidatos. Con un único candidato —que es justo el caso
del trazado manual— toda cota relativa colapsa: `min == max` y cada métrica
normaliza a 1.0, produciendo un score cercano a 100 que no significa nada.

Aquí se normaliza contra un par de referencias sintéticas (una ruta ideal y una
mala) escaladas a la distancia objetivo, de modo que el score del trazado manual
sea una lectura ABSOLUTA. No es directamente comparable con el score de una ruta
generada, que sí se normaliza contra sus rivales reales; la interfaz lo indica.
"""
from __future__ import annotations

import uuid

import numpy as np

from raceengine_contracts import (
    ConstraintModel, RawMetrics, RouteCandidate, RouteShape,
)

from .context import GraphContext
from .determinism import ENGINE_VERSION
from .messages import msg
from .metrics import (
    compliant_distance, compliant_target, compute_metrics, hard_violations,
    in_tolerance, ops_flags_for,
)
from .repair import evaluate_all, zone_masks
from .scoring import compute_indices, explain_all, global_score, normalize_set
from .snap import anchor, usable_node_mask

LANGS = ("es", "en")

# Cuán lejos del clic se acepta enganchar. Más allá, el usuario está pinchando
# donde no hay calle y conviene decírselo en vez de saltar media manzana.
MAX_SNAP_M = 260.0


# ── Referencias sintéticas para normalizar con un solo candidato ─────────────
def _reference_pair(distance_m: float) -> list[RawMetrics]:
    """Una ruta ideal y una mala a la misma distancia. Solo fijan las cotas."""
    km = max(distance_m / 1000.0, 0.2)
    good, bad = RawMetrics(), RawMetrics()
    good.distance_m = bad.distance_m = distance_m

    good.crossings, bad.crossings = 0, int(14 * km)
    good.lights, bad.lights = 0, int(5 * km)
    good.turns, bad.turns = 0, int(16 * km)
    good.smoothness, bad.smoothness = 1.0, 0.0
    good.gain_m, bad.gain_m = 0.0, 40.0 * km
    good.grad_max_pct, bad.grad_max_pct = 0.0, 12.0
    good.grad_avg_pct, bad.grad_avg_pct = 0.0, 6.0
    good.closure_impact, bad.closure_impact = 0.0, distance_m * 2.2
    good.enclosed_intersections, bad.enclosed_intersections = 0, int(45 * km)
    good.self_crossings, bad.self_crossings = 0, 6
    good.green_share_pct, bad.green_share_pct = 100.0, 0.0
    good.lit_share_pct, bad.lit_share_pct = 100.0, 0.0
    good.hosp_prox_m, bad.hosp_prox_m = 0.0, 3000.0
    return [good, bad]


def _reason(key: str, **kw) -> dict:
    return {"key": key, "i18n": {lg: msg(key, lg, **kw) for lg in LANGS},
            "params": {k: v for k, v in kw.items()}}


def _coords(ctx: GraphContext, nodes: list[int]) -> list[list[float]]:
    g = ctx.graph
    n = np.asarray(nodes, dtype=np.int64)
    return [[round(float(lo), 6), round(float(la), 6), round(float(el), 1)]
            for lo, la, el in zip(g.lon[n], g.lat[n], g.elev[n])]


def _gain_of(ctx: GraphContext, nodes: list[int]) -> float:
    if len(nodes) < 2 or not ctx.elevation_available():
        return 0.0
    e = ctx.graph.elev[np.asarray(nodes, dtype=np.int64)]
    d = np.diff(e)
    return float(d[d > 0].sum())


def _return_distance(ctx: GraphContext, model: ConstraintModel, anc, node: int,
                     target: int, cap_m: float) -> float | None:
    """Metros de vuelta desde `node` hasta `target` por calle. None si no hay."""
    if node == target:
        return 0.0
    # Para el circuito, `anc.reach` ya tiene la distancia real desde la salida y
    # el grafo es simétrico: es una consulta O(1) en el caso normal.
    if target == anc.start:
        d = float(anc.reach[node])
        if np.isfinite(d):
            return d
    leg = ctx.provider.shortest_path(node, target, anc.weights, max_distance_m=cap_m)
    return None if leg is None else leg.distance_m


def _snap_target(ctx: GraphContext, anc, lat: float, lon: float) -> tuple[int, float]:
    g = ctx.graph
    usable = usable_node_mask(g, anc.weights.allowed_edges, ctx.esrc)
    node = g.nearest_node(lat, lon, mask=usable)
    if node < 0:
        return -1, 0.0
    px, py = g.project(lat, lon)
    d = float(np.hypot(float(g.x[node]) - px, float(g.y[node]) - py))
    return int(node), d


def draw_state(ctx: GraphContext, model: ConstraintModel, nodes: list[int],
               edges: list[int], closed: bool = False) -> dict:
    """Evalúa el trazado actual: métricas, índices, score y presupuesto restante."""
    g = ctx.graph
    anc = anchor(ctx, model)
    end_target = anc.finish if model.routetype.shape == RouteShape.OPEN else anc.start

    if len(nodes) < 2 or not edges:
        return {
            "nodes": list(nodes), "edges": list(edges),
            "coordinates": _coords(ctx, nodes) if nodes else [],
            "distance_m": 0.0, "gain_m": 0.0, "closed": False,
            "remaining_m": model.distance.max_m,
            "return_m": 0.0, "route": None,
            "start": {"lat": float(g.lat[anc.start]), "lon": float(g.lon[anc.start])}
            if anc.start >= 0 else None,
        }

    m = compute_metrics(g, nodes, edges, model, g.coverage)
    rc = RouteCandidate(
        route_id=uuid.uuid4().hex[:12],
        coordinates=[(c[0], c[1], c[2]) for c in _coords(ctx, nodes)],
        node_path=list(nodes),
        metrics=m,
    )
    rc.in_tolerance = in_tolerance(m, model)
    rc.hard_violations = hard_violations(m, model, ctx.elevation_available())
    rc.ops_flags = ops_flags_for(m, model)

    masks = zone_masks(ctx, model)
    touches = evaluate_all(g, nodes, edges, model.mandatory_zones, masks,
                           model.touch_policy, model.lang)
    rc.mandatory_covered = [t.name for t in touches if t.touched]
    rc.mandatory_missing = [t.name for t in touches if not t.touched]

    # Normalización contra referencias sintéticas (ver docstring del módulo).
    pool = [m] + _reference_pair(model.distance.target_m)
    norms = normalize_set(pool, model)
    rc.normalized = norms[0]
    rc.indices = compute_indices(m, norms[0])
    rc.breakdown = global_score(norms[0], rc.indices, model)
    penalty = 12.0 * len(rc.hard_violations) + 8.0 * len(rc.mandatory_missing)
    rc.global_score = max(0.0, rc.breakdown.global_score - penalty)
    rc.breakdown.global_score = rc.global_score
    rc.breakdown.explanation_i18n = explain_all(m, rc.indices, rc.breakdown, model)
    rc.breakdown.explanation = list(rc.breakdown.explanation_i18n.get(model.lang, []))
    rc.provenance.generator = "manual"
    rc.provenance.graph_version = ctx.version
    rc.provenance.engine_version = ENGINE_VERSION

    back = _return_distance(ctx, model, anc, nodes[-1], end_target,
                            cap_m=max(model.distance.max_m - m.distance_m, 1.0))
    return {
        "nodes": list(nodes), "edges": list(edges),
        "coordinates": _coords(ctx, nodes),
        "distance_m": m.distance_m,
        "gain_m": m.gain_m,
        "closed": bool(closed),
        "remaining_m": max(0.0, model.distance.max_m - m.distance_m),
        "return_m": back,
        "in_tolerance": rc.in_tolerance,
        "route": rc.model_dump(mode="json"),
        "start": {"lat": float(g.lat[anc.start]), "lon": float(g.lon[anc.start])},
    }


def draw_extend(ctx: GraphContext, model: ConstraintModel, nodes: list[int],
                edges: list[int], lat: float | None, lon: float | None,
                close: bool = False) -> dict:
    """Intenta añadir un tramo. Devuelve `{ok: False, reason}` si lo prohíbe una regla."""
    g = ctx.graph
    anc = anchor(ctx, model)
    if anc.start < 0:
        return {"ok": False, "reason": _reason("draw_no_start")}

    nodes = list(nodes) or [int(anc.start)]
    edges = list(edges)
    cur_dist = float(g.elen[np.asarray(edges, dtype=np.int64)].sum()) if edges else 0.0
    last = int(nodes[-1])
    is_open = model.routetype.shape == RouteShape.OPEN
    end_target = anc.finish if is_open else anc.start

    if close:
        target, snap_m = end_target, 0.0
        if target == last:
            return {"ok": False, "reason": _reason("draw_already_closed")}
    else:
        if lat is None or lon is None:
            return {"ok": False, "reason": _reason("draw_no_point")}
        target, snap_m = _snap_target(ctx, anc, lat, lon)
        if target < 0:
            # No hay ni un nodo utilizable al alcance del índice: el clic cayó
            # fuera de la región. Decir «la más cercana está a 0 m» sería mentir.
            return {"ok": False, "reason": _reason("draw_outside_region")}
        if snap_m > MAX_SNAP_M:
            return {"ok": False, "reason": _reason("draw_no_street", d=snap_m)}
        if target == last:
            return {"ok": False, "reason": _reason("draw_same_node")}

    room = model.distance.max_m - cur_dist
    if room <= 0:
        return {"ok": False, "reason": _reason(
            "draw_budget_spent", d=cur_dist / 1000, m=model.distance.max_m / 1000)}

    # Regla 1 · ¿existe el tramo bajo las restricciones duras?
    leg = ctx.provider.shortest_path(last, target, anc.weights, max_distance_m=room)
    if leg is None or not leg.ok:
        return {"ok": False, "reason": _reason("draw_unreachable", r=room / 1000)}

    new_dist = cur_dist + leg.distance_m
    new_nodes = nodes + [int(n) for n in leg.nodes[1:]]
    new_edges = edges + [int(e) for e in leg.edges]

    # Regla 2 · tiene que caber CONTANDO EL REGRESO.
    if close:
        if new_dist < model.distance.min_m:
            return {"ok": False, "reason": _reason(
                "draw_close_short", d=new_dist / 1000,
                n=model.distance.min_m / 1000, x=model.distance.max_m / 1000)}
        if new_dist > model.distance.max_m:
            return {"ok": False, "reason": _reason(
                "draw_close_long", d=new_dist / 1000,
                n=model.distance.min_m / 1000, x=model.distance.max_m / 1000)}
    else:
        back = _return_distance(ctx, model, anc, target, end_target,
                                cap_m=max(model.distance.max_m - new_dist, 1.0))
        if back is None:
            return {"ok": False, "reason": _reason(
                "draw_no_return", r=(model.distance.max_m - new_dist) / 1000)}
        if new_dist + back > model.distance.max_m:
            return {"ok": False, "reason": _reason(
                "draw_no_room", d=new_dist / 1000, b=back / 1000,
                t=(new_dist + back) / 1000, x=model.distance.max_m / 1000)}

    # Regla 3 · desnivel positivo acumulado.
    el = model.elevation
    if el.enabled and el.max_gain_m is not None and ctx.elevation_available():
        gain = _gain_of(ctx, new_nodes)
        if gain > el.max_gain_m:
            return {"ok": False, "reason": _reason(
                "draw_gain", g=gain, m=el.max_gain_m)}

    state = draw_state(ctx, model, new_nodes, new_edges, closed=close)
    state["ok"] = True
    state["leg"] = {
        "coordinates": _coords(ctx, [int(n) for n in leg.nodes]),
        "distance_m": leg.distance_m,
        "snap_m": snap_m,
    }
    return state
