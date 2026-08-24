"""Preflight — álgebra de factibilidad antes de tocar el grafo pesado.

Implementa VR-1..VR-7 de doc 04 §6 y FR-3.5 (explicar qué restricción bloquea y
sugerir una relajación concreta).

Coste objetivo: < 100 ms. El MVP, ante una petición infactible, consumía 240 s y
terminaba con `[autopilot] final_routes: 0` (doc 10 §1.4).
"""
from __future__ import annotations

import math
import time

import numpy as np

from raceengine_contracts import (
    BlockingConstraint, ConstraintModel, InfeasibilityReport, RouteShape,
)

from .context import GraphContext
from .messages import msg
from .snap import anchor, usable_node_mask
from .weights import node_mask_for_zone


# Cuánto se deja pasar por encima del máximo antes de BLOQUEAR. Estrecho a
# propósito: lo que hacía declarar imposible un maratón real no era este margen,
# era la estimación mal ordenada de más abajo. Arreglada esa, ensanchar el margen
# sólo serviría para dejar pasar peticiones que el motor no puede cumplir y que
# el usuario descubriría al final — que es el fallo que VR-5 existe para evitar.
ESTIMATE_MARGIN = 1.03

# Rodeo sobre la línea recta. Con dos puntos lejanos, una retícula urbana añade un
# 20 %. Con catorce puntos repartidos por un recorrido, la propia poligonal YA
# sigue la calle y el factor sobra: aplicarlo igual inflaba la estimación hasta
# declarar imposible un maratón real trazado sobre sus propios kilómetros.
DETOUR_MAX = 1.20
DETOUR_MIN = 1.02
DETOUR_DENSE_N = 12          # a partir de aquí, la poligonal ya es la ruta


def mandatory_span_m(g, model) -> tuple[float, float]:
    """Longitud estimada del recorrido que visita todas las zonas obligatorias.

    Dos correcciones sobre la versión anterior, y las dos venían del mismo sitio:
    la estimación era tan pesimista que bloqueaba peticiones perfectamente
    posibles, y bloquear es lo único que el usuario no puede rebatir.

      1. **El orden.** Se sumaba en el orden en que se pincharon los puntos. Con
         catorce zonas trazadas sobre un maratón real eso daba 58,6 km de línea
         recta para un recorrido de 41,7 km. Ahora se reordena igual que en el
         modo libre, con la misma rutina.
      2. **El factor de rodeo.** Fijo en 1,20. Con muchos puntos la poligonal ya
         es prácticamente la ruta, así que el margen se estrecha con la densidad.
    """
    from .generate.direct import _order_heuristic

    pts = [(model.start.lat, model.start.lon)]
    pts += [z.center for z in model.mandatory_zones]
    pts.append((model.start.lat, model.start.lon))
    xy = [g.project(a, b) for a, b in pts]

    n = len(xy)
    if n > 3:
        d = np.zeros((n, n), dtype=np.float64)
        for i in range(n):
            for j in range(n):
                d[i, j] = math.hypot(xy[i][0] - xy[j][0], xy[i][1] - xy[j][1])
        orden = _order_heuristic(d)
        xy = [xy[i] for i in orden]

    span = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(xy, xy[1:]))

    # Con una o dos zonas el rodeo es el máximo: dos puntos lejanos y una recta
    # entre ellos no se parecen a lo que se recorre por calle. El margen se
    # estrecha SÓLO al añadir puntos intermedios, que es cuando la poligonal
    # empieza a seguir la carretera por sí sola.
    zonas = len(model.mandatory_zones)
    t = min(1.0, max(0.0, (zonas - 2) / float(DETOUR_DENSE_N - 2)))
    factor = DETOUR_MAX + (DETOUR_MIN - DETOUR_MAX) * t
    return span, factor


def run_preflight(ctx: GraphContext, model: ConstraintModel) -> InfeasibilityReport:
    t0 = time.perf_counter()
    blocking: list[BlockingConstraint] = []
    warnings: list[str] = []
    g = ctx.graph
    L = model.lang

    # En modo LIBRE no hay distancia objetivo, así que las reglas que la vigilan
    # no son «reglas que se cumplen»: son reglas que no aplican. Evaluarlas daba
    # bloqueos absurdos —«el radio no permite el circuito de 10 km»— sobre una
    # petición que nunca habló de 10 km.
    libre = model.distance.is_free

    # Un circuito «sin distancia» y sin puntos de paso es salir y volver al mismo
    # sitio sin más instrucción: no hay nada que resolver. No es un fallo del
    # motor, es una petición incompleta, y decirlo aquí ahorra una espera.
    if libre and model.routetype.shape != RouteShape.OPEN \
            and not model.mandatory_zones:
        blocking.append(BlockingConstraint(
            id="distance.mode", rule="VR-1c",
            detail=msg("pf_free_needs_points", L),
            suggested_relaxation={"routetype.shape": "open"},
        ))

    # VR-3 · suelo de tolerancia
    if not libre and model.distance.tolerance_pct < 1.0:
        warnings.append(
msg("pf_tolerance_tight", L, p=model.distance.tolerance_pct)
        )

    # VR-2 · el radio debe permitir geométricamente el circuito
    if not libre and model.routetype.shape in (RouteShape.CIRCUIT, RouteShape.OUT_AND_BACK):
        min_radius = model.distance.target_m / (2 * math.pi)
        if model.geometry.search_radius_max_m < min_radius:
            blocking.append(BlockingConstraint(
                id="geometry.search_radius_max_m", rule="VR-2",
                detail=msg("pf_radius_small", L, r=model.geometry.search_radius_max_m,
                           d=model.distance.target_m, m=min_radius),
                suggested_relaxation={
                    "geometry.search_radius_max_m": int(min_radius * 1.6)},
            ))

    # El punto de partida debe caer sobre la red utilizable BAJO ESTAS RESTRICCIONES,
    # no sobre el nodo más próximo sin más. Ver `snap.py`: pinchar sobre una avenida
    # real y recibir «no hay ninguna calle utilizable» era un error de anclaje.
    anc = anchor(ctx, model)
    start_node = anc.start
    if start_node < 0:
        blocking.append(BlockingConstraint(
            id="start", rule="reachability",
            detail=msg("pf_no_start", L),
            suggested_relaxation={},
        ))
        return InfeasibilityReport(
            blocking=tuple(blocking), warnings=tuple(warnings),
            preflight_ms=int((time.perf_counter() - t0) * 1000),
        )

    sx, sy = float(g.x[start_node]), float(g.y[start_node])
    d_start = anc.start_moved_m
    if anc.start_relocated and d_start > 25:
        # Se apartó del nodo más cercano a propósito: hay que decirlo, porque la
        # salida real no es exactamente donde el usuario pinchó.
        warnings.append(msg("pf_start_moved", L, d=d_start))
    elif d_start > 400:
        warnings.append(msg("pf_start_snapped", L, d=d_start))

    w, diag = anc.weights, anc.diag

    if diag["edges_allowed"] < 50:
        blocking.append(BlockingConstraint(
            id="hard_constraints", rule="VR-5",
            detail=msg("pf_graph_empty", L, a=diag["edges_allowed"], t=diag["edges_total"]),
            suggested_relaxation={
                "geometry.search_radius_max_m": int(model.geometry.search_radius_max_m * 2),
                "elevation.max_gradient_pct": None,
            },
        ))
        return InfeasibilityReport(
            blocking=tuple(blocking), warnings=tuple(warnings),
            preflight_ms=int((time.perf_counter() - t0) * 1000),
        )

    # Alcanzabilidad REAL: ¿hay suficiente red dentro de L/2 por calle?
    # Ya la calculó `anchor` al elegir el nodo de salida; reutilizarla evita un
    # segundo Dijkstra idéntico.
    budget = anc.reach_budget_m
    dist = anc.reach
    reachable = np.isfinite(dist)
    n_reach = anc.reach_nodes

    if n_reach < 40:
        blocking.append(BlockingConstraint(
            id="graph.connectivity", rule="VR-5",
            detail=msg("pf_disconnected", L, n=n_reach, b=budget),
            suggested_relaxation={
                "geometry.search_radius_max_m": int(model.geometry.search_radius_max_m * 2)},
        ))

    # VR-4 y alcanzabilidad de cada zona obligatoria
    forbidden_mask = np.zeros(g.n_nodes, dtype=bool)
    for z in model.forbidden_zones:
        forbidden_mask |= node_mask_for_zone(g, z)

    # «Contiene calles» debe medirse sobre la red ADMISIBLE: una zona llena de
    # escaleras o de tramos por encima de la pendiente máxima no contiene ninguna
    # calle utilizable aunque el grafo tenga nodos ahí.
    usable = usable_node_mask(g, w.allowed_edges, ctx.esrc)

    # Zonas cercanas que aun así salen inalcanzables: el culpable es la RED, no la
    # zona. Se agrupan y se reportan una sola vez, con el diagnóstico correcto.
    near_unreachable: list[str] = []
    NEAR_FACTOR = 0.45          # «cerca» = menos de la mitad de lo explorado

    for z in model.mandatory_zones:
        zmask = node_mask_for_zone(g, z) & usable
        if not zmask.any():
            blocking.append(BlockingConstraint(
                id=f"zone.mandatory[{z.name or z.id}]", rule="reachability",
                detail=msg("pf_zone_no_streets", L, z=z.name or z.id),
                suggested_relaxation={"buffer_m": max(z.buffer_m * 2, 400)},
            ))
            continue
        if (zmask & forbidden_mask).all():
            blocking.append(BlockingConstraint(
                id=f"zone.mandatory[{z.name or z.id}]", rule="VR-4",
                detail=msg("pf_zone_in_forbidden", L, z=z.name or z.id),
                suggested_relaxation={},
            ))
            continue
        if not (zmask & reachable).any():
            zlat, zlon = z.center
            straight = math.hypot(*(np.array(g.project(zlat, zlon)) - np.array([sx, sy])))
            if straight < NEAR_FACTOR * budget:
                # Está holgadamente dentro de lo que el objetivo permite recorrer.
                # Decir «queda fuera de lo alcanzable» sería técnicamente cierto y
                # completamente inútil: lo que ocurre es que desde la salida no se
                # llega a ninguna parte.
                near_unreachable.append(z.name or z.id)
                continue
            blocking.append(BlockingConstraint(
                id=f"zone.mandatory[{z.name or z.id}]", rule="reachability",
                detail=msg("pf_zone_unreachable", L, z=z.name or z.id, d=straight),
                suggested_relaxation={
                    "distance.target_m": int(max(model.distance.target_m,
                                                 straight * 2.6 / 100) * 100),
                    "geometry.search_radius_max_m": int(max(
                        model.geometry.search_radius_max_m, straight * 1.4)),
                },
            ))

    if near_unreachable:
        blocking.append(BlockingConstraint(
            id="start.connectivity", rule="VR-5",
            detail=msg("pf_start_isolated", L, list=", ".join(near_unreachable),
                       n=n_reach, t=int(g.n_nodes)),
            suggested_relaxation={
                "geometry.search_radius_max_m": int(min(
                    50_000, max(model.geometry.search_radius_max_m * 1.5, 3000))),
            },
        ))

    # La suma de los obligatorios no puede exceder la distancia objetivo
    if model.mandatory_zones and not libre:
        span, factor = mandatory_span_m(g, model)
        min_tour = span * factor
        # Se BLOQUEA sólo cuando el exceso es claro. Esto es una estimación
        # —recta reordenada por un factor de rodeo—, y bloquear al usuario con un
        # número aproximado es exactamente lo que hacía declarar imposible un
        # recorrido que existe. Dentro del margen se avisa y se deja intentar: si
        # de verdad no cabe, el motor lo dirá con la ruta delante.
        if min_tour > model.distance.max_m * ESTIMATE_MARGIN:
            blocking.append(BlockingConstraint(
                id="zones.mandatory", rule="VR-5",
                detail=msg("pf_zones_too_far", L, n=len(model.mandatory_zones),
                           t=min_tour / 1000, s=span / 1000, f=factor,
                           m=model.distance.max_m / 1000),
                suggested_relaxation={
                    "distance.target_m": int(round(min_tour * 1.06, -2)),
                },
            ))
        elif min_tour > model.distance.max_m:
            warnings.append(msg("pf_zones_maybe", L, t=min_tour / 1000,
                                m=model.distance.max_m / 1000))
        elif span > model.distance.target_m * 0.75:
            warnings.append(
msg("pf_zones_tight", L, p=100 * span / model.distance.target_m)
            )

    # Topes de intersecciones manifiestamente irreales para la distancia.
    # Una carrera urbana cruza del orden de 8 intersecciones por kilómetro; poner
    # un máximo de 4 para un maratón garantiza que ninguna ruta lo cumpla.
    km = model.distance.target_m / 1000.0
    expected_crossings = int(km * 8)
    it = model.intersections
    if not libre and it.max_crossings is not None \
            and it.max_crossings < expected_crossings * 0.25:
        warnings.append(
msg("pf_caps_crossings", L, c=it.max_crossings, k=km, e=expected_crossings)
        )
    if it.max_traffic_lights is not None and it.max_traffic_lights < km * 0.4:
        warnings.append(
msg("pf_caps_lights", L, l=it.max_traffic_lights, k=km)
        )

    # Elevación pedida pero sin datos
    if model.elevation.enabled and not ctx.elevation_available():
        if model.elevation.max_gain_m is not None or model.elevation.max_gradient_pct is not None:
            warnings.append(
msg("pf_no_elevation", L)
            )

    return InfeasibilityReport(
        blocking=tuple(blocking), warnings=tuple(warnings),
        preflight_ms=int((time.perf_counter() - t0) * 1000),
    )
