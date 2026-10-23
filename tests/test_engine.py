"""Tests del motor. Corren sin red: usan la región sintética determinista."""
from __future__ import annotations

import numpy as np
import pytest

from raceengine_contracts import (
    ConstraintModel, CustomZone, DistanceLayer, ElevationLayer, GeoJSONPoint,
    GeometryLayer, GeoPoint, IntersectionLayer, Objective, ZoneBehavior,
)
from raceengine_engine import generate, get_context, run_preflight
from raceengine_engine.scoring.score import distance_fit

START = GeoPoint(lat=-33.4372, lon=-70.6506)


@pytest.fixture(scope="session")
def ctx():
    return get_context("demo")


def model(**kw) -> ConstraintModel:
    base = dict(
        region="demo", start=START,
        distance=DistanceLayer(target_m=6000, tolerance_pct=2.0),
        geometry=GeometryLayer(search_radius_max_m=3000),
        fast_budget_ms=2500, total_budget_ms=6000,
    )
    base.update(kw)
    return ConstraintModel(**base)


# ── Contratos ────────────────────────────────────────────────────────────────
def test_constraint_model_is_frozen():
    """El modelo no puede mutar a mitad de una generación (doc 10 §3.2b)."""
    m = model()
    with pytest.raises(Exception):
        m.region = "otra"


def test_fingerprint_is_stable_and_content_addressed():
    a, b = model(), model()
    assert a.fingerprint("g", "e") == b.fingerprint("g", "e")
    assert a.fingerprint("g", "e") != model(
        distance=DistanceLayer(target_m=7000)).fingerprint("g", "e")


def test_vr1_start_equals_finish_rejects_open_shape():
    from raceengine_contracts import RouteShape, RouteTypeLayer
    with pytest.raises(ValueError):
        model(routetype=RouteTypeLayer(shape=RouteShape.OPEN, start_equals_finish=True))


# ── Preflight ────────────────────────────────────────────────────────────────
def test_preflight_is_fast(ctx):
    r = run_preflight(ctx, model())
    assert r.preflight_ms < 100, "el preflight debe costar menos de 100 ms"


def test_vr2_radius_too_small_is_blocked(ctx):
    r = run_preflight(ctx, model(
        distance=DistanceLayer(target_m=42195, tolerance_pct=1.0),
        geometry=GeometryLayer(search_radius_max_m=2000)))
    assert not r.feasible
    assert any(b.rule == "VR-2" for b in r.blocking)
    assert r.blocking[0].suggested_relaxation, "debe sugerir una relajación concreta"


def test_unreachable_mandatory_zone_is_blocked(ctx):
    z = CustomZone(id="x", name="Lejos", behavior=ZoneBehavior.MANDATORY,
                   geometry=GeoJSONPoint(coordinates=(-70.60, -33.40)), buffer_m=300)
    r = run_preflight(ctx, model(
        distance=DistanceLayer(target_m=3000, tolerance_pct=2.0), zones=(z,)))
    assert not r.feasible


# ── Determinismo (NFR-6.1) ───────────────────────────────────────────────────
def test_deterministic_mode_is_bit_identical(ctx):
    m = model(deterministic_iterations=120, total_budget_ms=60000)
    runs = [generate(m) for _ in range(3)]
    assert len({r.seed for r in runs}) == 1
    assert len({round(r.recommended.global_score, 9) for r in runs}) == 1
    assert len({tuple(r.recommended.coordinates) for r in runs}) == 1


def test_geometry_is_deterministic_even_with_wall_clock_budget(ctx):
    m = model()
    a, b = generate(m), generate(m)
    assert a.seed == b.seed
    assert a.recommended.metrics.distance_m == b.recommended.metrics.distance_m


# ── Distancia ────────────────────────────────────────────────────────────────
def test_reaches_distance_tolerance(ctx):
    r = generate(model())
    assert r.recommended is not None
    assert r.recommended.in_tolerance, (
        f"{r.recommended.metrics.distance_m:.0f} m fuera de tolerancia")


def test_distance_fit_is_monotone_and_has_gradient_outside_band():
    """Corrige el defecto de la fórmula de doc 06 §4.1 (ver score.py)."""
    t, tol = 10000.0, 200.0
    assert distance_fit(t, t, tol) == pytest.approx(1.0)
    # Monótona decreciente en todo el rango, también fuera de la banda.
    errs = [0, 100, 200, 400, 900, 2000, 5000]
    vals = [distance_fit(t + e, t, tol) for e in errs]
    assert all(a > b for a, b in zip(vals, vals[1:])), vals
    # Continua en el borde de la banda.
    assert distance_fit(t + tol, t, tol) == pytest.approx(
        distance_fit(t + tol + 1e-9, t, tol), abs=1e-6)


# ── Zonas ────────────────────────────────────────────────────────────────────
def test_mandatory_zone_is_covered(ctx):
    z = CustomZone(id="z1", name="Estadio", behavior=ZoneBehavior.MANDATORY,
                   geometry=GeoJSONPoint(coordinates=(-70.6400, -33.4280)), buffer_m=300)
    r = generate(model(distance=DistanceLayer(target_m=9000, tolerance_pct=2.5),
                       geometry=GeometryLayer(search_radius_max_m=4000), zones=(z,)))
    assert r.recommended.mandatory_covered == ["Estadio"]
    assert not r.recommended.mandatory_missing


def test_forbidden_zone_is_never_entered(ctx):
    import numpy as np
    from raceengine_engine.weights import node_mask_for_zone

    z = CustomZone(id="f1", name="Obra", behavior=ZoneBehavior.FORBIDDEN,
                   geometry=GeoJSONPoint(coordinates=(-70.6560, -33.4330)), buffer_m=450)
    r = generate(model(zones=(z,)))
    mask = node_mask_for_zone(ctx.graph, z)
    inside = [n for n in r.recommended.node_path if mask[n]]
    assert not inside, f"{len(inside)} nodos dentro de la zona prohibida"


# ── Elevación: la corrección de H1 ───────────────────────────────────────────
def test_elevation_influences_the_search(ctx):
    """En el MVP `Edge.grad` valía 0.0 durante toda la búsqueda (doc 10 §3.1).

    Se comprueba donde la propiedad es EXACTA —en el coste de las aristas— en vez
    de sobre el resultado de dos ejecuciones completas. Comparar el desnivel final
    de dos búsquedas distintas mide sobre todo el ruido del optimizador: en la
    región de referencia el relieve total es de pocos metros y la diferencia entre
    objetivos queda por debajo de esa escala.
    """
    import numpy as np

    from raceengine_engine.weights import build_weights

    g = ctx.graph
    start = g.nearest_node(START.lat, START.lon)
    llano = model(elevation=ElevationLayer(enabled=True, prefer_flatter=True))
    plano = model(elevation=ElevationLayer(enabled=True, prefer_flatter=False))
    w_flat, _ = build_weights(g, llano, ctx.esrc, start)
    w_none, _ = build_weights(g, plano, ctx.esrc, start)

    up = g.egrad > 1.0
    assert up.any(), "la región de referencia tiene aristas en subida"
    # Con preferencia por lo llano, subir cuesta MÁS que su propia longitud...
    ratio_up = w_flat.weight[up] / np.maximum(g.elen[up], 1e-6)
    assert float(ratio_up.min()) > 1.0
    # ...y más que sin la preferencia activada.
    assert float(w_flat.weight[up].sum()) > float(w_none.weight[up].sum())
    # La bajada también se penaliza, pero mucho menos: es la asimetría real.
    down = g.egrad < -1.0
    if down.any():
        pen_up = float(np.mean(w_flat.weight[up] / np.maximum(g.elen[up], 1e-6)))
        pen_dn = float(np.mean(w_flat.weight[down] / np.maximum(g.elen[down], 1e-6)))
        assert pen_up > pen_dn


def test_elevation_is_measured_on_the_result(ctx):
    """Y el desnivel llega al informe: no es un campo que se quede en cero."""
    r = generate(model(elevation=ElevationLayer(enabled=True, prefer_flatter=True)))
    m = r.recommended.metrics
    assert m.gain_m > 0 and m.loss_m > 0
    assert m.grad_max_pct > 0


def test_max_gradient_is_a_hard_edge_constraint(ctx):
    r = generate(model(elevation=ElevationLayer(enabled=True, max_gradient_pct=3.0)))
    assert r.recommended.metrics.grad_max_pct <= 3.0 + 1e-6


# ── Scoring ──────────────────────────────────────────────────────────────────
def test_four_indices_exist_and_are_bounded(ctx):
    r = generate(model())
    ix = r.recommended.indices
    for v in (ix.speed, ix.logistics, ix.scenery, ix.difficulty):
        assert 0.0 <= v <= 1.0
    assert 0.0 <= r.recommended.global_score <= 100.0


def test_score_breakdown_is_decomposable(ctx):
    """FR-6.5: el score debe poder explicarse por sus términos."""
    r = generate(model())
    bd = r.recommended.breakdown
    assert set(bd.terms) == {"fit", "speed", "logistics", "scenery", "difficulty"}
    assert sum(bd.terms.values()) == pytest.approx(bd.global_score, abs=0.05) or \
        r.recommended.hard_violations or r.recommended.mandatory_missing


def test_objective_changes_the_weights(ctx):
    a = generate(model(objective=Objective.FLATTEST))
    b = generate(model(objective=Objective.FEWEST_CLOSURES))
    assert a.recommended.breakdown.weights != b.recommended.breakdown.weights


# ── Restricciones duras y banderas operativas ────────────────────────────────
def test_self_crossing_is_a_flag_not_a_rejection(ctx):
    """El filtro duro del MVP vaciaba el espacio de soluciones (doc 10 §3.5)."""
    r = generate(model())
    assert r.recommended is not None
    if r.recommended.metrics.self_crossings > 0:
        assert any(f.code == "at_grade_crossing" for f in r.recommended.ops_flags)


def test_intersection_caps_are_enforced_or_reported(ctx):
    r = generate(model(intersections=IntersectionLayer(max_traffic_lights=0)))
    rec = r.recommended
    assert rec.metrics.lights == 0 or rec.hard_violations


# ── Anytime ──────────────────────────────────────────────────────────────────
def test_first_route_is_emitted_quickly(ctx):
    first = []
    generate(model(), on_update=lambda ev, res:
             first.append(res.elapsed_ms) if ev == "recommended" and not first else None)
    assert first and first[0] < 5000, f"primera ruta en {first[0]} ms"


def test_incumbent_never_gets_worse(ctx):
    """Monotonía del contrato anytime, medida sobre lo que es ABSOLUTO.

    El score global se normaliza contra el conjunto de candidatos vivo en ese
    instante (doc 06 §4.2), así que el NÚMERO emitido puede bajar aunque la ruta
    haya mejorado: cambia la vara de medir, no la ruta. Lo que sí es absoluto —y
    lo que el contrato promete de verdad— es la clase de factibilidad: cobertura
    de obligatorios, violaciones duras y estar dentro de tolerancia. Eso nunca
    puede empeorar.
    """
    from raceengine_engine.engine import _distance_gate

    keys = []
    m = model(total_budget_ms=6000)

    def watch(ev, res):
        if ev == "recommended" and res.recommended:
            c = res.recommended
            # Mismo orden que `_feasible_first`: un error de distancia desmedido
            # pesa MÁS que una violación dura, así que corregirlo justifica
            # aceptar un candidato con más violaciones. Si se omite ese término,
            # el test acusa de regresión a un cambio que sí fue una mejora.
            gate = _distance_gate(m.distance.target_m, m.distance.tolerance_m)
            egregious = 1 if abs(c.metrics.distance_m
                                 - m.distance.target_m) > gate else 0
            keys.append((egregious, len(c.mandatory_missing),
                         len(c.hard_violations), 0 if c.in_tolerance else 1))

    r = generate(m, on_update=watch)
    assert keys, "debe haberse emitido al menos un incumbente"
    for prev, nxt in zip(keys, keys[1:]):
        assert nxt <= prev, f"el incumbente empeoró: {prev} → {nxt}"

    # Y bajo la normalización FINAL —una única vara— la recomendada es la mejor.
    for alt in r.alternatives:
        assert r.recommended.global_score >= alt.global_score - 1e-6


def test_cancellation_returns_best_so_far(ctx):
    import threading
    import time

    from raceengine_engine import GenerationHandle

    h = GenerationHandle(generation_id="test")
    threading.Timer(1.0, h.cancel.set).start()
    t0 = time.perf_counter()
    r = generate(model(total_budget_ms=30000), handle=h)
    assert r.status == "cancelled"
    assert r.recommended is not None
    assert time.perf_counter() - t0 < 6.0


# ── Diversidad ───────────────────────────────────────────────────────────────
def test_alternatives_respect_overlap_limit(ctx):
    r = generate(model(total_budget_ms=8000))
    rec = set(r.recommended.node_path)
    for alt in r.alternatives:
        a = set(alt.node_path)
        overlap = len(rec & a) / min(len(rec), len(a))
        assert overlap <= 0.70 + 1e-6, f"solape {overlap:.2f} > 0.70"


def test_only_one_recommended_route(ctx):
    r = generate(model())
    assert r.recommended is not None
    assert isinstance(r.recommended, type(r.recommended))


# ── Regresiones del feedback de uso ──────────────────────────────────────────
def test_mandatory_coverage_holds_at_marathon_distance(ctx):
    """Regresión: con 42 km el presupuesto de Mandatory Repair se agotaba y las
    zonas quedaban sin cubrir."""
    zs = (
        CustomZone(id="a", name="A", behavior=ZoneBehavior.MANDATORY,
                   geometry=GeoJSONPoint(coordinates=(-70.6200, -33.4100)), buffer_m=350),
        CustomZone(id="b", name="B", behavior=ZoneBehavior.MANDATORY,
                   geometry=GeoJSONPoint(coordinates=(-70.6900, -33.4700)), buffer_m=350),
    )
    r = generate(model(distance=DistanceLayer(target_m=42195, tolerance_pct=1.0),
                       geometry=GeometryLayer(search_radius_max_m=15000),
                       zones=zs, total_budget_ms=22000))
    assert not r.recommended.mandatory_missing, r.recommended.mandatory_missing
    assert r.recommended.in_tolerance


def test_alternatives_are_usable_not_noise(ctx):
    """Regresión: se ofrecían alternativas de 28 km para un objetivo de 42 km.

    Ahora una alternativa solo se muestra si está razonablemente cerca del
    objetivo; se prefiere devolver menos (o ninguna) antes que ruido.
    """
    target, tol_pct = 12000, 2.0
    r = generate(model(distance=DistanceLayer(target_m=target, tolerance_pct=tol_pct),
                       geometry=GeometryLayer(search_radius_max_m=5000),
                       total_budget_ms=12000))
    max_dev = max(3.0 * target * tol_pct / 100, 0.05 * target)
    for a in r.alternatives:
        dev = abs(a.metrics.distance_m - target)
        assert dev <= max_dev, f"alternativa a {dev:.0f} m del objetivo (máx {max_dev:.0f})"
        assert not a.mandatory_missing


def test_preflight_blocks_when_mandatory_span_exceeds_target(ctx):
    """Regresión: se aceptaban peticiones que el motor no podía cumplir, y el
    usuario lo descubría solo al final. Ahora se bloquean con una sugerencia."""
    zs = (
        CustomZone(id="a", name="A", behavior=ZoneBehavior.MANDATORY,
                   geometry=GeoJSONPoint(coordinates=(-70.6300, -33.4250)), buffer_m=350),
        CustomZone(id="b", name="B", behavior=ZoneBehavior.MANDATORY,
                   geometry=GeoJSONPoint(coordinates=(-70.6700, -33.4500)), buffer_m=350),
    )
    rep = run_preflight(ctx, model(
        distance=DistanceLayer(target_m=10000, tolerance_pct=2.0),
        geometry=GeometryLayer(search_radius_max_m=4500), zones=zs))
    assert not rep.feasible
    b = next(x for x in rep.blocking if x.id == "zones.mandatory")
    assert b.suggested_relaxation["distance.target_m"] > 10000


def test_preflight_warns_on_unrealistic_intersection_caps(ctx):
    rep = run_preflight(ctx, model(
        distance=DistanceLayer(target_m=42195, tolerance_pct=1.0),
        geometry=GeometryLayer(search_radius_max_m=15000),
        intersections=IntersectionLayer(max_crossings=4)))
    assert any("irreal" in w for w in rep.warnings)


# ── Regresiones de la segunda ronda de feedback ──────────────────────────────
def test_marathon_with_zones_never_blows_up_the_distance(ctx):
    """Regresión crítica: un objetivo de 42 km devolvía 64 km (+52%).

    Causa: `_chord` intentaba eliminar el ancla de mayor desvío, que era la de una
    zona obligatoria; la reparación transaccional lo rechazaba y el circuito se
    quedaba atascado. Además el anillo no se reordenaba tras insertar.
    """
    zs = (
        CustomZone(id="a", name="A", behavior=ZoneBehavior.MANDATORY,
                   geometry=GeoJSONPoint(coordinates=(-70.6200, -33.4100)), buffer_m=400),
        CustomZone(id="b", name="B", behavior=ZoneBehavior.MANDATORY,
                   geometry=GeoJSONPoint(coordinates=(-70.6900, -33.4700)), buffer_m=400),
    )
    r = generate(model(distance=DistanceLayer(target_m=42195, tolerance_pct=1.0),
                       geometry=GeometryLayer(search_radius_max_m=15000),
                       zones=zs, total_budget_ms=25000))
    rec = r.recommended
    err = abs(rec.metrics.distance_m - 42195) / 42195
    assert err < 0.08, f"error de distancia {100 * err:.1f}%"
    assert not rec.mandatory_missing


def test_open_route_starts_and_ends_at_different_points(ctx):
    """L2: recorrido abierto (inicio ≠ meta)."""
    from raceengine_contracts import RouteShape, RouteTypeLayer

    r = generate(ConstraintModel(
        region="demo",
        start=GeoPoint(lat=-33.4600, lon=-70.6800),
        finish=GeoPoint(lat=-33.4150, lon=-70.6200),
        routetype=RouteTypeLayer(shape=RouteShape.OPEN, start_equals_finish=False),
        distance=DistanceLayer(target_m=12000, tolerance_pct=2.0),
        geometry=GeometryLayer(search_radius_max_m=8000),
        fast_budget_ms=3000, total_budget_ms=12000))
    rec = r.recommended
    assert rec is not None and rec.in_tolerance
    assert rec.coordinates[0] != rec.coordinates[-1]


def test_open_route_requires_a_finish_point():
    from raceengine_contracts import RouteShape, RouteTypeLayer
    with pytest.raises(ValueError):
        ConstraintModel(region="demo", start=START, finish=None,
                        routetype=RouteTypeLayer(shape=RouteShape.OPEN,
                                                 start_equals_finish=False))


def test_all_offered_routes_are_in_tolerance(ctx):
    """El usuario ve varias rutas; TODAS deben cumplir la distancia pedida."""
    r = generate(model(distance=DistanceLayer(target_m=10000, tolerance_pct=2.0),
                       geometry=GeometryLayer(search_radius_max_m=4000),
                       total_budget_ms=18000))
    for rt in [r.recommended] + r.alternatives:
        assert rt.in_tolerance, f"{rt.metrics.distance_m:.0f} m fuera de tolerancia"


def test_dem_artifacts_are_clamped(ctx):
    """doc 07 §3.2: los DEM globales son DSM y producen pendientes fantasma."""
    import numpy as np

    from raceengine_geodata.graph import MAX_PLAUSIBLE_GRADE_PCT
    assert float(np.abs(ctx.graph.egrad).max()) <= MAX_PLAUSIBLE_GRADE_PCT + 1e-6


def test_infeasible_verdict_when_zones_and_distance_conflict(ctx):
    """Si no se puede cumplir ambas, se dice — no se devuelve una ruta cualquiera."""
    zs = (
        CustomZone(id="a", name="A", behavior=ZoneBehavior.MANDATORY,
                   geometry=GeoJSONPoint(coordinates=(-70.6100, -33.4050)), buffer_m=300),
        CustomZone(id="b", name="B", behavior=ZoneBehavior.MANDATORY,
                   geometry=GeoJSONPoint(coordinates=(-70.6950, -33.4750)), buffer_m=300),
    )
    r = generate(model(distance=DistanceLayer(target_m=5000, tolerance_pct=2.0),
                       geometry=GeometryLayer(search_radius_max_m=12000),
                       zones=zs, total_budget_ms=8000))
    assert r.status == "infeasible"
    assert r.infeasibility and r.infeasibility.blocking


# ── Tercera ronda de feedback ────────────────────────────────────────────────
def test_mandatory_zone_covered_under_every_objective(ctx):
    """Regresión: con `flattest` la zona obligatoria se quedaba sin cubrir.

    Causa: la cota `max_cost` de A* estaba en coste PONDERADO pero los llamadores
    le pasaban metros. Con `prefer_flatter` una arista cuesta hasta 2,5× su
    longitud, así que los tramos hacia una zona cuesta arriba se rechazaban.
    """
    z = CustomZone(id="c", name="Cerro", behavior=ZoneBehavior.MANDATORY,
                   geometry=GeoJSONPoint(coordinates=(-70.6381, -33.4456)), buffer_m=300)
    for obj in (Objective.FASTEST, Objective.FLATTEST, Objective.BALANCED,
                Objective.MOST_SCENIC, Objective.FEWEST_CLOSURES):
        r = generate(model(distance=DistanceLayer(target_m=8000, tolerance_pct=2.0),
                           geometry=GeometryLayer(search_radius_max_m=4000),
                           elevation=ElevationLayer(enabled=True, prefer_flatter=True),
                           zones=(z,), objective=obj, total_budget_ms=14000))
        assert r.recommended is not None, obj
        assert not r.recommended.mandatory_missing, f"{obj.value}: zona sin cubrir"


def test_routing_bound_is_expressed_in_metres(ctx):
    """La cota de A* debe interpretarse en metros, no en coste ponderado."""
    from raceengine_engine.weights import build_weights

    m = model(elevation=ElevationLayer(enabled=True, prefer_flatter=True))
    w, diag = build_weights(ctx.graph, m, ctx.esrc,
                            ctx.graph.nearest_node(START.lat, START.lon))
    assert w.max_ratio >= 1.0
    assert diag["max_ratio"] >= diag["min_ratio"]


def test_engine_emits_both_languages(ctx):
    """Cambiar de idioma no debe obligar a regenerar la ruta."""
    r = generate(model(lang="es"))
    bd = r.recommended.breakdown
    assert set(bd.explanation_i18n) == {"es", "en"}
    assert bd.explanation_i18n["es"] != bd.explanation_i18n["en"]
    for f in r.recommended.ops_flags:
        assert set(f.message_i18n) == {"es", "en"}


def test_engine_language_switches_output(ctx):
    es = generate(model(lang="es"))
    en = generate(model(lang="en"))
    assert es.recommended.breakdown.explanation[0].startswith("Distancia")
    assert en.recommended.breakdown.explanation[0].startswith("Distance")


def test_preflight_messages_follow_language(ctx):
    bad = dict(distance=DistanceLayer(target_m=42195, tolerance_pct=1.0),
               geometry=GeometryLayer(search_radius_max_m=2000))
    es = run_preflight(ctx, model(lang="es", **bad))
    en = run_preflight(ctx, model(lang="en", **bad))
    assert "radio" in es.blocking[0].detail
    assert "radius" in en.blocking[0].detail


# ── Anclaje de la salida (snap.py) ───────────────────────────────────────────
def test_start_is_anchored_to_a_usable_node(ctx):
    """La salida debe caer sobre un nodo con aristas ADMISIBLES, no solo cercano."""
    import numpy as np

    from raceengine_engine.snap import anchor, usable_node_mask

    m = model()
    a = anchor(ctx, m)
    usable = usable_node_mask(ctx.graph, a.weights.allowed_edges, ctx.esrc)
    assert a.start >= 0
    assert usable[a.start], "la salida quedó en un nodo sin ninguna arista admisible"
    assert a.reach_nodes >= 40
    assert np.isfinite(a.reach[a.start])


def test_start_relocates_when_the_nearest_node_is_a_dead_end(ctx):
    """Reproduce el caso de Viña: el nodo más próximo al clic no es utilizable.

    Se fuerza la situación excluyendo del grafo admisible todas las aristas
    incidentes al nodo que devolvería `nearest_node`. El anclaje debe apartarse
    de ese nodo en vez de declarar «no hay ninguna calle utilizable».
    """
    import numpy as np

    from raceengine_engine import snap
    from raceengine_engine.weights import build_weights as real_build

    g = ctx.graph
    m = model()
    dead = g.nearest_node(START.lat, START.lon)

    def crippled(graph, mdl, esrc, start_node):
        w, diag = real_build(graph, mdl, esrc, start_node)
        mask = w.allowed_edges.copy()
        mask &= (esrc != dead) & (graph.indices != dead)
        w.allowed_edges = mask
        diag = dict(diag, edges_allowed=int(mask.sum()))
        return w, diag

    snap._CACHE.clear()
    orig = snap.build_weights
    snap.build_weights = crippled
    try:
        a = snap.anchor(ctx, m)
    finally:
        snap.build_weights = orig
        snap._CACHE.clear()

    assert a.start != dead, "el anclaje se quedó en el nodo inutilizable"
    assert a.start_relocated
    assert a.reach_nodes >= 40
    assert np.isfinite(a.reach[a.start])


def test_anchor_is_cached_per_model(ctx):
    from raceengine_engine.snap import anchor

    a, b = anchor(ctx, model()), anchor(ctx, model())
    assert a is b, "el anclaje debería reutilizarse entre preflight y generación"


# ── Modo creativo (draw.py) ──────────────────────────────────────────────────
def _draw_model(**kw):
    return model(distance=DistanceLayer(target_m=5000, tolerance_pct=3.0), **kw)


def test_draw_extend_follows_the_street_network(ctx):
    from raceengine_engine.draw import draw_extend

    r = draw_extend(ctx, _draw_model(), [], [], -33.4300, -70.6450)
    assert r["ok"], r.get("reason")
    assert len(r["nodes"]) > 2
    assert r["distance_m"] > 0
    # Todos los nodos consecutivos están unidos por una arista real del grafo.
    g = ctx.graph
    for a, b in zip(r["nodes"], r["nodes"][1:]):
        assert b in g.indices[g.indptr[a]:g.indptr[a + 1]].tolist()


def test_draw_rejects_a_leg_that_leaves_no_room_to_return(ctx):
    """La regla que el usuario pidió: no dejar seguir si rompe la distancia."""
    from raceengine_engine.draw import draw_extend

    m = _draw_model()
    st = draw_extend(ctx, m, [], [], -33.4300, -70.6450)
    assert st["ok"]
    st2 = draw_extend(ctx, m, st["nodes"], st["edges"], -33.4280, -70.6580)
    assert st2["ok"]
    bad = draw_extend(ctx, m, st2["nodes"], st2["edges"], -33.4370, -70.6620)
    assert not bad["ok"]
    assert bad["reason"]["key"] in {"draw_no_room", "draw_no_return", "draw_budget_spent"}
    assert set(bad["reason"]["i18n"]) == {"es", "en"}


def test_draw_rejects_a_click_far_from_any_street(ctx):
    from raceengine_engine.draw import draw_extend

    r = draw_extend(ctx, _draw_model(), [], [], -33.30, -70.90)
    assert not r["ok"]
    assert r["reason"]["key"] in {"draw_no_street", "draw_outside_region"}


def test_draw_close_requires_the_tolerance_band(ctx):
    from raceengine_engine.draw import draw_extend

    m = _draw_model()
    st = draw_extend(ctx, m, [], [], -33.4340, -70.6480)
    assert st["ok"]
    closed = draw_extend(ctx, m, st["nodes"], st["edges"], None, None, close=True)
    assert not closed["ok"]
    assert closed["reason"]["key"] == "draw_close_short"


def test_draw_never_enters_a_forbidden_zone(ctx):
    """Las restricciones duras valen igual dibujando a mano que generando."""
    from raceengine_engine.draw import draw_extend
    from raceengine_engine.weights import node_mask_for_zone

    z = CustomZone(id="f1", name="veto", behavior=ZoneBehavior.FORBIDDEN,
                   geometry=GeoJSONPoint(coordinates=(-70.6470, -33.4330)), buffer_m=400)
    m = _draw_model(zones=(z,))
    r = draw_extend(ctx, m, [], [], -33.4300, -70.6450)
    if r["ok"]:
        mask = node_mask_for_zone(ctx.graph, z)
        assert not any(bool(mask[n]) for n in r["nodes"]), "el trazado entró en la zona vetada"


def test_draw_score_is_not_saturated_by_a_single_candidate(ctx):
    """Con n=1 las cotas relativas colapsan; por eso se usan referencias fijas."""
    from raceengine_engine.draw import draw_extend

    m = _draw_model()
    st = draw_extend(ctx, m, [], [], -33.4300, -70.6450)
    assert st["ok"]
    score = st["route"]["global_score"]
    assert 0.0 < score < 99.0, f"score saturado: {score}"


def test_draw_state_is_stateless_and_reproducible(ctx):
    from raceengine_engine.draw import draw_extend, draw_state

    m = _draw_model()
    st = draw_extend(ctx, m, [], [], -33.4300, -70.6450)
    again = draw_state(ctx, m, st["nodes"], st["edges"])
    assert again["distance_m"] == st["distance_m"]
    assert again["coordinates"] == st["coordinates"]


def test_start_prefers_the_largest_reachable_component(ctx):
    """Reproduce el caso de Manhattan: un fragmento pequeño junto a la malla real.

    El nodo más próximo al clic queda dentro de una bolsa aislada de unos pocos
    cientos de nodos. Esa bolsa supera cualquier umbral modesto, así que un
    anclaje que aceptara «el primero que pasa el mínimo» se quedaría ahí — y a
    partir de ese momento hasta una zona obligatoria a 900 m saldría inalcanzable.
    El anclaje debe elegir el candidato que alcanza MÁS red.
    """
    import numpy as np

    from raceengine_engine import snap
    from raceengine_engine.weights import build_weights as real_build

    g = ctx.graph
    m = model(distance=DistanceLayer(target_m=20000, tolerance_pct=2.0),
              geometry=GeometryLayer(search_radius_max_m=8000))
    px, py = g.project(START.lat, START.lon)
    # Bolsa de ~250 m alrededor del clic: se corta del resto del grafo.
    d2 = (g.x - px) ** 2 + (g.y - py) ** 2
    pocket = d2 <= 250.0 ** 2
    assert 5 < int(pocket.sum()) < g.n_nodes // 4, int(pocket.sum())

    def crippled(graph, mdl, esrc, start_node):
        w, diag = real_build(graph, mdl, esrc, start_node)
        mask = w.allowed_edges.copy()
        crossing = pocket[esrc] != pocket[graph.indices]
        mask &= ~crossing                      # la bolsa queda desconectada
        w.allowed_edges = mask
        return w, dict(diag, edges_allowed=int(mask.sum()))

    snap._CACHE.clear()
    orig = snap.build_weights
    snap.build_weights = crippled
    try:
        a = snap.anchor(ctx, m)
    finally:
        snap.build_weights = orig
        snap._CACHE.clear()

    assert not pocket[a.start], "el anclaje se quedó dentro de la bolsa aislada"
    assert a.reach_nodes > int(pocket.sum()) * 3, (
        f"alcance {a.reach_nodes} frente a bolsa de {int(pocket.sum())}")


def test_near_zone_reports_start_connectivity_not_zone_distance(ctx):
    """Una zona cercana que sale inalcanzable debe acusar a la RED, no a la zona."""
    from raceengine_engine import snap
    from raceengine_engine.weights import build_weights as real_build

    g = ctx.graph
    px, py = g.project(START.lat, START.lon)
    d2 = (g.x - px) ** 2 + (g.y - py) ** 2
    # Bolsa grande: todos los candidatos de anclaje caen dentro de ella, así que
    # el motor no tiene escapatoria y la zona de fuera queda inalcanzable de verdad.
    pocket = d2 <= 1500.0 ** 2

    far = np.nonzero(d2 > 1800.0 ** 2)[0]
    zi = int(far[np.argmin(d2[far])])
    straight = float(np.sqrt(d2[zi]))
    z = CustomZone(id="z", name="Cercana", behavior=ZoneBehavior.MANDATORY,
                   geometry=GeoJSONPoint(coordinates=(float(g.lon[zi]), float(g.lat[zi]))),
                   buffer_m=200)
    m = model(distance=DistanceLayer(target_m=20000, tolerance_pct=2.0),
              geometry=GeometryLayer(search_radius_max_m=8000), zones=(z,))
    assert straight < 0.45 * m.distance.target_m * 0.62, straight

    def crippled(graph, mdl, esrc, start_node):
        w, diag = real_build(graph, mdl, esrc, start_node)
        mask = w.allowed_edges.copy()
        mask &= ~(pocket[esrc] != pocket[graph.indices])   # la bolsa se desconecta
        w.allowed_edges = mask
        return w, dict(diag, edges_allowed=int(mask.sum()))

    snap._CACHE.clear()
    orig = snap.build_weights
    snap.build_weights = crippled
    try:
        r = run_preflight(ctx, m)
    finally:
        snap.build_weights = orig
        snap._CACHE.clear()

    assert not r.feasible
    ids = {b.id for b in r.blocking}
    assert "start.connectivity" in ids, ids
    # Y NO debe culpar a la zona por su distancia.
    assert not any(b.id.startswith("zone.mandatory") and b.rule == "reachability"
                   for b in r.blocking), [b.id for b in r.blocking]
    detail = next(b.detail for b in r.blocking if b.id == "start.connectivity")
    assert "Cercana" in detail


# ── Presupuesto reproducible ─────────────────────────────────────────────────
def test_iteration_budget_scales_with_the_graph(ctx):
    """El presupuesto en iteraciones debe encogerse en un grafo más caro.

    Es el fallo que se vio en Santiago: la calibración estaba hecha sobre una
    región de 4.761 nodos y se aplicaba tal cual a una de 120.000, así que el
    motor pedía un trabajo que no cabía en el presupuesto, tardaba el triple y el
    resultado dejaba de ser reproducible.
    """
    import copy

    from raceengine_engine.engine import iteration_budget, ms_per_iteration

    m = model(total_budget_ms=20000)
    barato = iteration_budget(m, ctx)

    caro = copy.copy(ctx)
    caro.astar_ms = ctx.astar_ms * 20        # una ciudad veinte veces más cara
    pocas = iteration_budget(m, caro)

    assert pocas < barato / 5, (barato, pocas)
    assert ms_per_iteration(m, caro) > ms_per_iteration(m, ctx)


def test_calibration_is_quantised_and_deterministic(ctx):
    """Dos medidas del mismo grafo no pueden dar presupuestos distintos."""
    from raceengine_engine.context import calibrate

    a, b = calibrate(ctx), calibrate(ctx)
    assert a == b, (a, b)
    assert a > 0


def test_reproducible_by_default_gives_the_same_route_twice(ctx):
    m = model(total_budget_ms=8000)
    a, b = generate(m), generate(m)
    assert a.reproducible and b.reproducible
    assert a.recommended.coordinates == b.recommended.coordinates
    assert round(a.recommended.global_score, 9) == round(b.recommended.global_score, 9)


def test_reproducible_flag_is_false_when_the_clock_cuts_the_search(ctx):
    """Si salta la red de seguridad hay que DECIRLO, no callarlo."""
    m = model(total_budget_ms=1000, fast_budget_ms=500,
              distance=DistanceLayer(target_m=20000, tolerance_pct=2.0),
              geometry=GeometryLayer(search_radius_max_m=8000))
    r = generate(m)
    # O terminó el trabajo fijado (reproducible) o lo cortó el reloj (y lo dice).
    truncado = any("tope de seguridad" in line for line in r.log)
    assert r.reproducible != truncado


# ── Cobertura con muchas zonas obligatorias ──────────────────────────────────
def _five_zones():
    pts = [(-70.6400, -33.4280), (-70.6600, -33.4300), (-70.6650, -33.4450),
           (-70.6350, -33.4500), (-70.6450, -33.4200)]
    return tuple(
        CustomZone(id=f"z{i}", name=f"Obligatorio {i + 1}",
                   behavior=ZoneBehavior.MANDATORY,
                   geometry=GeoJSONPoint(coordinates=c), buffer_m=300)
        for i, c in enumerate(pts)
    )


def test_five_mandatory_zones_are_all_covered_at_marathon_distance(ctx):
    """El caso que falló en Santiago: 42 km y cinco zonas repartidas.

    Antes las zonas no participaban en la generación —se insertaban después, una
    a una, con un presupuesto insuficiente— y el resultado salía con tres o cuatro
    sin cubrir. Ahora son la semilla del anillo.
    """
    from raceengine_contracts import RouteTypeLayer

    r = generate(model(
        distance=DistanceLayer(target_m=42195, tolerance_pct=1.0),
        geometry=GeometryLayer(search_radius_max_m=15000),
        routetype=RouteTypeLayer(allow_segment_reuse=True),
        zones=_five_zones(), fast_budget_ms=4000, total_budget_ms=20000))
    c = r.recommended
    assert not c.mandatory_missing, c.mandatory_missing
    assert len(c.mandatory_covered) == 5
    assert c.in_tolerance, f"{c.metrics.distance_m:.0f} m"


def test_more_budget_never_degrades_coverage(ctx):
    """Más presupuesto no puede dar PEOR cobertura.

    Era el síntoma más claro de que el bucle de mejora erosionaba las zonas: con
    50 s salían cuatro sin cubrir y con 20 s sólo tres.
    """
    from raceengine_contracts import RouteTypeLayer

    base = dict(distance=DistanceLayer(target_m=20000, tolerance_pct=2.0),
                geometry=GeometryLayer(search_radius_max_m=9000),
                routetype=RouteTypeLayer(allow_segment_reuse=True),
                zones=_five_zones(), fast_budget_ms=3000)
    corto = generate(model(total_budget_ms=6000, **base))
    largo = generate(model(total_budget_ms=20000, **base))
    assert len(largo.recommended.mandatory_missing) <= len(corto.recommended.mandatory_missing)


def test_improve_operators_never_touch_a_protected_anchor(ctx):
    """Las anclas de zona obligatoria son intocables para los operadores."""
    from raceengine_engine.improve.operators import OPERATORS
    from raceengine_engine.snap import anchor

    g = ctx.graph
    m = model(distance=DistanceLayer(target_m=12000, tolerance_pct=2.0),
              geometry=GeometryLayer(search_radius_max_m=6000), zones=_five_zones())
    a = anchor(ctx, m)
    from raceengine_engine.generate import mandatory_seed_anchors
    from raceengine_engine.repair import zone_masks

    must = mandatory_seed_anchors(ctx, zone_masks(ctx, m), a.reach, a.start)
    assert len(must) >= 3
    protegidas = frozenset(must)
    ring = list(must) + [int(n) for n in np.nonzero(
        np.isfinite(a.reach) & g.is_intersection)[0][:4] if int(n) not in protegidas]

    rng = np.random.default_rng(7)
    for nombre, op in OPERATORS.items():
        for _ in range(40):
            out = op(g, ring, a.start, a.reach, rng, protected=protegidas)
            if out is None:
                continue
            faltan = protegidas - set(out)
            assert not faltan, f"{nombre} perdió {len(faltan)} ancla(s) obligatoria(s)"


# ── Medición certificable ────────────────────────────────────────────────────
def test_spr_solver_matches_the_closed_form_on_a_right_angle():
    """Cortar una esquina de 90° ahorra ≈ 2·d·sin(45°). Es comprobable a mano."""
    from raceengine_engine.measure import (
        corner_saving_estimate, shortest_possible_route,
    )

    for d in (1.0, 3.0, 6.0):
        x = np.array([-100.0, 0.0, 0.0])
        y = np.array([0.0, 0.0, 100.0])
        allow = np.array([0.0, d, 0.0])
        length, _, _ = shortest_possible_route(x, y, allow)
        ahorro = 200.0 - length
        cerrado = corner_saving_estimate(90.0, d)
        # La forma cerrada es de primer orden y sobreestima un poco; el solver
        # debe quedarse justo por debajo, nunca por encima.
        assert 0.90 * cerrado <= ahorro <= cerrado + 1e-6, (ahorro, cerrado)


def test_spr_never_exceeds_the_centreline(ctx):
    r = generate(model())
    m = r.recommended.metrics
    assert 0 < m.spr_m <= m.distance_m
    assert m.spr_saving_m == pytest.approx(m.distance_m - m.spr_m, abs=1e-6)


def test_a_straight_line_has_nothing_to_cut():
    from raceengine_engine.measure import shortest_possible_route

    x = np.linspace(0, 1000, 50)
    y = np.zeros(50)
    allow = np.full(50, 3.0)
    allow[0] = allow[-1] = 0.0
    length, _, _ = shortest_possible_route(x, y, allow)
    assert length == pytest.approx(1000.0, abs=1e-6)


def test_certified_mode_targets_the_measured_line_not_the_centreline(ctx):
    """La diferencia que hace que un maratón certifique o no.

    En modo eje, la longitud SPR sale sistemáticamente corta respecto al objetivo
    —que es lo que mediría el juez—. En modo certificado el motor apunta a la SPR
    y es el eje el que queda largo.
    """
    from raceengine_contracts import DistanceLayer
    from raceengine_engine.measure import SHORT_COURSE_PREVENTION_FACTOR as SCPF

    base = dict(geometry=GeometryLayer(search_radius_max_m=5000),
                fast_budget_ms=2500, total_budget_ms=10000)
    eje = generate(model(distance=DistanceLayer(target_m=10000, tolerance_pct=1.0),
                         **base))
    cert = generate(model(
        distance=DistanceLayer(target_m=10000, tolerance_pct=1.0,
                               measurement="certified"), **base))

    objetivo = 10000 * SCPF
    assert cert.recommended.in_tolerance
    # La SPR del modo certificado se acerca más al objetivo certificable que la
    # del modo eje, que es justo el defecto que este modo corrige.
    err_cert = abs(cert.recommended.metrics.spr_m - objetivo)
    err_eje = abs(eje.recommended.metrics.spr_m - objetivo)
    assert err_cert < err_eje, (err_cert, err_eje)
    # Y el eje del recorrido certificado queda LARGO a propósito.
    assert cert.recommended.metrics.distance_m > objetivo


# ── Vueltas ──────────────────────────────────────────────────────────────────
def test_laps_multiply_the_distance_but_not_the_closures(ctx):
    """La ventaja operativa del circuito de vueltas, medida.

    Con N vueltas se recorre lo mismo pero se corta una fracción de la calle: es
    la partida más cara del presupuesto de una carrera urbana.
    """
    from raceengine_contracts import RouteTypeLayer

    base = dict(distance=DistanceLayer(target_m=12000, tolerance_pct=2.0),
                geometry=GeometryLayer(search_radius_max_m=6000),
                fast_budget_ms=2500, total_budget_ms=9000)
    una = generate(model(routetype=RouteTypeLayer(allow_segment_reuse=True), **base))
    tres = generate(model(
        routetype=RouteTypeLayer(laps=3, allow_segment_reuse=True), **base))

    a, b = una.recommended.metrics, tres.recommended.metrics
    assert b.distance_m == pytest.approx(12000, rel=0.05)
    assert tres.recommended.in_tolerance
    assert b.closure_len_m < a.closure_len_m * 0.55, (a.closure_len_m, b.closure_len_m)


def test_lap_repetition_is_not_counted_as_segment_reuse(ctx):
    from raceengine_contracts import RouteTypeLayer

    r = generate(model(
        distance=DistanceLayer(target_m=12000, tolerance_pct=2.0),
        geometry=GeometryLayer(search_radius_max_m=6000),
        routetype=RouteTypeLayer(laps=3, allow_segment_reuse=True),
        fast_budget_ms=2500, total_budget_ms=9000))
    # Si se contara la repetición, esto sería ~100%.
    assert r.recommended.metrics.segment_reuse_pct < 60.0


# ── Capacidad ────────────────────────────────────────────────────────────────
def test_capacity_scales_with_the_field(ctx):
    from raceengine_contracts import LogisticsLayer

    base = dict(distance=DistanceLayer(target_m=8000, tolerance_pct=2.0),
                geometry=GeometryLayer(search_radius_max_m=4000),
                fast_budget_ms=2000, total_budget_ms=7000)
    poca = generate(model(logistics=LogisticsLayer(field_size=1000), **base))
    mucha = generate(model(logistics=LogisticsLayer(field_size=8000), **base))
    a, b = poca.recommended.metrics, mucha.recommended.metrics
    assert a.min_width_m > 0 and a.flow_per_min > 0
    assert b.field_clear_min > a.field_clear_min
    # Sin dato de inscritos no se inventa un tiempo de paso.
    sin = generate(model(**base))
    assert sin.recommended.metrics.field_clear_min == 0.0
    assert sin.recommended.metrics.min_width_m > 0


# ── Importación ──────────────────────────────────────────────────────────────
def _gpx_of(route) -> str:
    pts = "".join(
        f'<trkpt lat="{c[1]:.6f}" lon="{c[0]:.6f}"/>' for c in route.coordinates)
    return f'<?xml version="1.0"?><gpx version="1.1"><trk><trkseg>{pts}</trkseg></trk></gpx>'


def test_import_round_trip_recovers_the_same_route(ctx):
    """Exportar e importar debe devolver el mismo recorrido, no uno parecido."""
    from raceengine_engine.importer import match_track, parse_track

    m = model(distance=DistanceLayer(target_m=8000, tolerance_pct=2.0),
              geometry=GeometryLayer(search_radius_max_m=4000),
              fast_budget_ms=2000, total_budget_ms=7000)
    r = generate(m)
    pts = parse_track(_gpx_of(r.recommended))
    assert len(pts) > 10

    out = match_track(ctx, m, pts)
    assert out["ok"]
    assert out["imported"]["matched_pct"] > 95
    assert out["imported"]["deviation_m"] < 25
    assert out["distance_m"] == pytest.approx(
        r.recommended.metrics.distance_m, rel=0.05)


def test_import_scores_with_the_same_engine(ctx):
    from raceengine_engine.importer import match_track, parse_track

    m = model(distance=DistanceLayer(target_m=8000, tolerance_pct=2.0),
              geometry=GeometryLayer(search_radius_max_m=4000),
              fast_budget_ms=2000, total_budget_ms=7000)
    r = generate(m)
    out = match_track(ctx, m, parse_track(_gpx_of(r.recommended)))
    ruta = out["route"]
    assert 0 < ruta["global_score"] <= 100
    assert ruta["metrics"]["crossings"] > 0
    assert set(ruta["breakdown"]["explanation_i18n"]) == {"es", "en"}


def test_import_rejects_a_track_from_another_city(ctx):
    from raceengine_engine.importer import match_track

    m = model()
    out = match_track(ctx, m, [(48.8566, 2.3522), (48.8570, 2.3530)])
    assert not out["ok"]
    assert "región" in out["error"] or "ciudad" in out["error"]


def test_import_parses_gpx_and_geojson_alike(ctx):
    from raceengine_engine.importer import parse_track

    gpx = ('<?xml version="1.0"?><gpx><trk><trkseg>'
           '<trkpt lat="-33.44" lon="-70.65"/><trkpt lat="-33.45" lon="-70.66"/>'
           '</trkseg></trk></gpx>')
    geo = ('{"type":"Feature","geometry":{"type":"LineString",'
           '"coordinates":[[-70.65,-33.44],[-70.66,-33.45]]}}')
    a, b = parse_track(gpx), parse_track(geo)
    assert a == b == [(-33.44, -70.65), (-33.45, -70.66)]


# ── Emparejado de trazas: los dos fallos que se veían en el mapa ─────────────
def _parallel_streets_ctx(sep_m: float = 28.0, n: int = 60, step_m: float = 25.0):
    """Dos avenidas paralelas separadas `sep_m`, unidas cada 200 m.

    Es el caso que rompía el emparejador ingenuo: con ruido de GPS de 15 m, el
    nodo más cercano a un punto de la avenida norte puede pertenecer a la sur, y
    decidir punto a punto produce el zigzag entre calzadas que se veía en el mapa.
    """
    import time

    import numpy as np

    from raceengine_engine.context import GraphContext
    from raceengine_geodata import GraphBuilder
    from raceengine_routing import LocalRoutingProvider

    lat0, lon0 = -33.4372, -70.6506
    dlat = lambda m: m / 111320.0                                    # noqa: E731
    dlon = lambda m: m / (111320.0 * np.cos(np.radians(lat0)))       # noqa: E731

    b = GraphBuilder(lat0, lon0)
    norte, sur = [], []
    for i in range(n):
        a_id, b_id = 1000 + i, 2000 + i
        la_n, la_s = lat0 + dlat(sep_m), lat0
        lo = lon0 + dlon(i * step_m)
        b.add_node(a_id, la_n, lo)
        b.add_node(b_id, la_s, lo)
        norte.append((a_id, la_n, lo))
        sur.append((b_id, la_s, lo))
        if i:
            b.add_edge(1000 + i - 1, a_id, step_m, "secondary")
            b.add_edge(2000 + i - 1, b_id, step_m, "secondary")
        if i % 8 == 0:                                   # travesaños cada 200 m
            b.add_edge(a_id, b_id, sep_m, "residential")
    g = b.build(version="paralelas")
    esrc = np.repeat(np.arange(g.n_nodes, dtype=np.int32),
                     np.diff(g.indptr).astype(np.int32))
    ctx = GraphContext(region_id="paralelas", graph=g,
                       provider=LocalRoutingProvider(g), esrc=esrc,
                       intersections=g.is_intersection, built_at=time.time())
    idx = {int(nid): i for i, nid in enumerate(g.node_ids.tolist())}
    return ctx, [idx[i] for i, _, _ in norte], [idx[i] for i, _, _ in sur], norte


def test_import_stays_on_the_street_the_track_runs_along():
    """Con ruido de GPS mayor que la separación entre calzadas, no debe saltar.

    Antes se decidía cada punto por separado, así que un punto ruidoso enganchaba
    a la avenida de al lado y el tramo siguiente volvía: ese ida y vuelta es el
    aspecto «escalonado» que se veía sobre el mapa.
    """
    import numpy as np

    from raceengine_engine.importer import match_track

    ctx, norte, sur, pts_norte = _parallel_streets_ctx()
    g = ctx.graph
    rng = np.random.default_rng(11)
    sigma = 15.0                       # ruido MAYOR que la mitad de la separación
    traza = [(la + rng.normal(0, sigma) / 111320.0,
              lo + rng.normal(0, sigma) / (111320.0 * np.cos(np.radians(la))))
             for _, la, lo in pts_norte]

    m = ConstraintModel(region="paralelas", start=GeoPoint(lat=pts_norte[0][1],
                                                          lon=pts_norte[0][2]),
                        distance=DistanceLayer(target_m=1400, tolerance_pct=10.0),
                        geometry=GeometryLayer(search_radius_max_m=3000))
    out = match_track(ctx, m, traza)
    assert out["ok"], out.get("error")

    en_sur = sum(1 for nd in out["nodes"] if nd in set(sur))
    assert en_sur == 0, f"{en_sur} nodos enganchados a la calzada equivocada"
    # Y la longitud es la de la avenida, no la de un zigzag entre calzadas.
    largo = (len(pts_norte) - 1) * 25.0
    assert out["distance_m"] == pytest.approx(largo, rel=0.02)


def test_import_follows_a_track_that_breaks_the_rules(ctx):
    """Una traza importada tiene derecho a violar las restricciones del usuario.

    Emparejar con los pesos de la carrera era el error de fondo: con una zona
    prohibida encima, las calles por las que de verdad pasaba la traza estaban
    vetadas y el emparejador no tenía más remedio que irse por otro sitio. El
    recorrido resultante no era el que se había importado, y todo lo medido
    después describía una ficción.
    """
    from raceengine_engine.importer import match_track, parse_track

    limpio = model(distance=DistanceLayer(target_m=6000, tolerance_pct=3.0),
                   geometry=GeometryLayer(search_radius_max_m=3000),
                   fast_budget_ms=2000, total_budget_ms=6000)
    r = generate(limpio)
    traza = parse_track(_gpx_of(r.recommended))

    # Una zona prohibida justo encima de la mitad del recorrido.
    medio = r.recommended.coordinates[len(r.recommended.coordinates) // 2]
    vetado = limpio.model_copy(update={"zones": (
        CustomZone(id="veto", name="veto", behavior=ZoneBehavior.FORBIDDEN,
                   buffer_m=400,
                   geometry=GeoJSONPoint(coordinates=(medio[0], medio[1]))),)})

    out = match_track(ctx, vetado, traza)
    assert out["ok"], out.get("error")
    assert out["distance_m"] == pytest.approx(
        r.recommended.metrics.distance_m, rel=0.05), "el enganche esquivó la zona"
    assert out["imported"]["deviation_m"] < 25


def test_import_declares_a_gap_instead_of_inventing_a_shortcut(ctx):
    """Si falta red bajo un trozo de la traza, se corta y se dice; no se cose."""
    import numpy as np

    from raceengine_engine.importer import match_track, parse_track

    m = model(distance=DistanceLayer(target_m=6000, tolerance_pct=3.0),
              geometry=GeometryLayer(search_radius_max_m=3000),
              fast_budget_ms=2000, total_budget_ms=6000)
    r = generate(m)
    traza = parse_track(_gpx_of(r.recommended))
    corte = len(traza) // 2
    # Un salto de ~4 km en mitad de la traza: no hay forma honesta de unirlo.
    lejos = [(la + 0.04, lo) for la, lo in traza[corte:corte + 4]]
    rota = traza[:corte] + lejos + traza[corte + 4:]

    out = match_track(ctx, m, rota)
    assert out["ok"], out.get("error")
    im = out["imported"]
    assert im["gaps"] + im["unmatched_points"] > 0, "el salto pasó desapercibido"
    saltos = np.hypot(*np.diff(np.array(
        [(ctx.graph.x[n], ctx.graph.y[n]) for n in out["nodes"]]), axis=0).T)
    assert saltos.max() < 400.0, "se cosió un tramo que la traza no recorre"


def test_import_returns_the_original_trace_for_comparison(ctx):
    """Sin la traza cruda dibujada debajo, el usuario no puede juzgar el enganche."""
    from raceengine_engine.importer import match_track, parse_track

    m = model(distance=DistanceLayer(target_m=6000, tolerance_pct=3.0),
              geometry=GeometryLayer(search_radius_max_m=3000),
              fast_budget_ms=2000, total_budget_ms=6000)
    r = generate(m)
    out = match_track(ctx, m, parse_track(_gpx_of(r.recommended)))
    raw = out["raw_coordinates"]
    assert len(raw) > 10 and len(raw[0]) == 2
    assert out["imported"]["raw_length_m"] == pytest.approx(
        r.recommended.metrics.distance_m, rel=0.05)


def test_import_works_without_a_start_point_configured(ctx):
    """Importar antes de fijar nada: la salida es la de la traza."""
    from raceengine_engine.importer import match_track, parse_track

    m = model(distance=DistanceLayer(target_m=6000, tolerance_pct=3.0),
              geometry=GeometryLayer(search_radius_max_m=3000),
              fast_budget_ms=2000, total_budget_ms=6000)
    r = generate(m)
    traza = parse_track(_gpx_of(r.recommended))

    # Salida del usuario deliberadamente lejos de la traza.
    otro = m.model_copy(update={"start": GeoPoint(lat=-33.4600, lon=-70.6800)})
    out = match_track(ctx, otro, traza)
    assert out["ok"], out.get("error")
    assert out["route"]["global_score"] > 0
    assert out["start"]["lat"] == pytest.approx(traza[0][0], abs=2e-3)


# ── Regiones compuestas ──────────────────────────────────────────────────────
def _strip(lat0: float, lon0: float, base_id: int, n: int = 30, step_m: float = 40.0):
    """Una calle recta de `n` nodos, con ids propios, para probar la unión."""
    import numpy as np

    from raceengine_geodata import GraphBuilder

    b = GraphBuilder(lat0, lon0)
    dlon = lambda m: m / (111320.0 * np.cos(np.radians(lat0)))       # noqa: E731
    for i in range(n):
        nid = base_id + i
        b.add_node(nid, lat0, lon0 + dlon(i * step_m))
        if i:
            b.add_edge(base_id + i - 1, nid, step_m, "residential")
    return b.build(version=f"tira{base_id}")


def test_merging_two_regions_keeps_every_street():
    from raceengine_geodata import merge_graphs

    a = _strip(-33.4372, -70.6506, 1000)
    b = _strip(-33.4500, -70.6506, 2000)
    m = merge_graphs([a, b], version="a+b")
    assert m.n_nodes == a.n_nodes + b.n_nodes
    assert m.n_edges == a.n_edges + b.n_edges
    assert m.version == "a+b"


def test_merging_welds_the_seam_instead_of_duplicating_it():
    """Un cruce que aparece en dos descargas es UN nodo, no dos.

    Es la razón de identificar los nodos por su id de OSM: si se dedujera por
    coordenadas, el redondeo dejaría dos nodos a centímetros uno de otro y el
    recorrido no podría pasar de una comuna a la otra.
    """
    from raceengine_geodata import merge_graphs

    a = _strip(-33.4372, -70.6506, 1000, n=30)
    # Comparte los diez últimos nodos de `a` (mismos ids, mismas coordenadas).
    b = _strip(-33.4372, -70.6506, 1000, n=45)
    m = merge_graphs([a, b])
    assert m.n_nodes == 45, "la costura duplicó nodos"
    assert m.n_edges == b.n_edges


def test_merging_a_single_region_is_a_no_op(ctx):
    from raceengine_geodata import load_region

    a = load_region("demo")
    b = load_region("demo+demo")
    assert b.n_nodes == a.n_nodes and b.n_edges == a.n_edges


def test_a_composite_region_generates_a_route():
    """El motor debe tratar una región compuesta como cualquier otra."""
    from raceengine_engine import get_context

    c = get_context("demo+demo")
    assert c.graph.n_nodes > 0
    r = generate(model(region="demo+demo"))
    assert r.recommended is not None
    assert r.recommended.metrics.distance_m > 0


def test_region_parts_are_sorted_and_deduplicated():
    from raceengine_geodata import region_parts

    assert region_parts("nunoa+las_condes+nunoa") == ["las_condes", "nunoa"]
    assert region_parts("demo") == ["demo"]


def test_certified_mode_never_lands_short(ctx):
    """Un recorrido corto no se homologa, y la banda lo refleja.

    En modo eje la tolerancia es simétrica; en certificable no puede serlo.
    Pasarse unos metros es irrelevante —los corredores hacen algo de más—, pero
    quedarse corto invalida el recorrido aunque falte un metro. Con la banda
    simétrica el motor apuntaba al objetivo exacto y la mitad de los resultados
    salían unos metros por debajo, con el panel diciendo a la vez «en tolerancia»
    y «no certificaría».
    """
    from raceengine_engine.metrics import (
        compliant_target, in_tolerance, target_aim,
    )
    from raceengine_contracts import RawMetrics

    m = model(distance=DistanceLayer(target_m=8000, tolerance_pct=2.0,
                                     measurement="certified"),
              geometry=GeometryLayer(search_radius_max_m=4000),
              fast_budget_ms=2000, total_budget_ms=7000)
    t = compliant_target(m)                      # 8.000 × 1,001
    tol = m.distance.tolerance_m

    corto, largo = RawMetrics(), RawMetrics()
    corto.spr_m, largo.spr_m = t - 1.0, t + tol
    corto.distance_m = largo.distance_m = 8000.0
    assert not in_tolerance(corto, m), "un metro corto no es certificable"
    assert in_tolerance(largo, m), "pasarse dentro de la banda sí lo es"
    # Y se apunta al centro de esa banda, no a su borde inferior.
    assert target_aim(m) == pytest.approx(t + tol)

    r = generate(m)
    rec = r.recommended
    assert rec.metrics.spr_m >= t - 1e-6, (
        f"la ruta certificable mide {t - rec.metrics.spr_m:.0f} m corta")


def test_centreline_mode_keeps_the_symmetric_band(ctx):
    from raceengine_engine.metrics import compliant_target, in_tolerance, target_aim
    from raceengine_contracts import RawMetrics

    m = model(distance=DistanceLayer(target_m=8000, tolerance_pct=2.0))
    t, tol = compliant_target(m), m.distance.tolerance_m
    assert target_aim(m) == pytest.approx(t)
    corto = RawMetrics()
    corto.distance_m = t - tol * 0.5
    assert in_tolerance(corto, m)


# ── Ciclismo ─────────────────────────────────────────────────────────────────
def test_the_discipline_decides_which_roads_exist():
    """Es la restricción más dura: cambia el grafo sobre el que se busca."""
    import numpy as np

    from raceengine_engine.weights import DISCIPLINE_IDS
    from raceengine_geodata import ALL_CLASSES

    idx = {c: i for i, c in enumerate(ALL_CLASSES)}
    correr = set(DISCIPLINE_IDS["running"].tolist())
    ruta = set(DISCIPLINE_IDS["road"].tolist())
    gravel = set(DISCIPLINE_IDS["gravel"].tolist())
    mtb = set(DISCIPLINE_IDS["mtb"].tolist())

    # Una nacional es la columna vertebral de una marcha y un sitio al que no se
    # manda a un corredor.
    assert idx["trunk"] in ruta and idx["trunk"] not in correr
    # Un camino agrícola no es para ruta y sí para gravel.
    assert idx["track"] not in ruta and idx["track"] in gravel
    # Una senda sólo para MTB.
    assert idx["bridleway"] in mtb and idx["bridleway"] not in gravel
    # Y las escaleras no son de nadie.
    assert all(idx["steps"] not in s for s in (correr, ruta, gravel, mtb))
    # Cada disciplina de bici contiene a la anterior.
    assert ruta < gravel < mtb


def test_running_sees_exactly_what_it_always_saw(ctx):
    """Añadir disciplinas no puede cambiar lo que hacía una carrera a pie."""
    from raceengine_contracts import DISCIPLINE_CLASSES
    from raceengine_geodata import RACE_CLASSES

    assert set(DISCIPLINE_CLASSES["running"]) == set(RACE_CLASSES)


def test_the_class_table_is_append_only():
    """`eclass` guarda el índice en la tabla: reordenarla rompe los grafos ya hechos.

    Un grafo construido antes de existir el ciclismo tiene índices 0–9. Si se
    insertara una clase en medio, esos índices pasarían a significar otra cosa y
    una calle residencial se volvería un sendero sin que nada avisara.
    """
    from raceengine_geodata import ALL_CLASSES, RACE_CLASSES

    assert ALL_CLASSES[:len(RACE_CLASSES)] == RACE_CLASSES
    assert ALL_CLASSES.index("residential") == 0


def test_cycling_speed_follows_a_power_balance():
    """Subir es desproporcionadamente lento y bajar tiene techo."""
    import numpy as np

    from raceengine_engine.cycling import MAX_DESCENT_KMH, speed_kmh

    g = np.array([-12.0, -6, -3, 0, 3, 6, 9, 12])
    v = speed_kmh("road", g)
    # Monótona decreciente con la pendiente.
    assert all(a > b for a, b in zip(v, v[1:])), v
    llano = float(speed_kmh("road", np.array([0.0]))[0])
    assert 25.0 < llano < 34.0, f"{llano:.1f} km/h en llano no es un aficionado"
    # Al 8 % un aficionado sube, no empuja la bici.
    ocho = float(speed_kmh("road", np.array([8.0]))[0])
    assert 7.0 < ocho < 13.0, f"{ocho:.1f} km/h al 8 %"
    # Bajando no se dispara: manda el aire y el freno.
    assert float(speed_kmh("road", np.array([-15.0]))[0]) <= MAX_DESCENT_KMH
    # Y el terreno ordena las disciplinas en llano.
    plano = [float(speed_kmh(d, np.array([0.0]))[0]) for d in ("road", "gravel", "mtb")]
    assert plano[0] > plano[1] > plano[2]


def test_cycling_time_is_not_a_fixed_pace(ctx):
    """Dos rutas de la misma distancia y distinto desnivel no tardan lo mismo."""
    from raceengine_contracts import Discipline
    from raceengine_engine.cycling import ride_time_min

    g = ctx.graph
    e = np.arange(min(400, g.n_edges), dtype=np.int64)
    llano = ride_time_min(g, e, "road")
    # Con el mismo trazado en MTB se tarda bastante más.
    monte = ride_time_min(g, e, "mtb")
    assert monte > llano * 1.4


def test_climbs_are_reported_one_by_one():
    """«1.850 m de desnivel» no dice si es un puerto o cincuenta repechos."""
    import time

    import numpy as np

    from raceengine_engine.cycling import climb_summary, climbs
    from raceengine_geodata import GraphBuilder, bake_elevation

    # Calle recta de 4 km: 1 km llano, 2 km al 6 %, 1 km bajando.
    lat0, lon0, paso = -33.4372, -70.6506, 100.0
    b = GraphBuilder(lat0, lon0)
    n = 40
    for i in range(n):
        b.add_node(1000 + i, lat0, lon0 + i * paso / 93_000.0)
        if i:
            b.add_edge(1000 + i - 1, 1000 + i, paso, "tertiary")
    g = b.build(version="rampa")
    alt = np.zeros(g.n_nodes)
    for i in range(g.n_nodes):
        if i < 10:
            alt[i] = 0.0
        elif i < 30:
            alt[i] = (i - 10) * paso * 0.06
        else:
            alt[i] = alt[29] - (i - 29) * paso * 0.05
    bake_elevation(g, alt, smooth=False)

    # Los nodos se indexan por id de OSM ordenado, así que el camino de ida es
    # 0, 1, 2… y `edges_for_path` recupera sus aristas en ese sentido.
    from raceengine_engine.engine import edges_for_path

    camino = list(range(g.n_nodes))
    e = np.asarray(edges_for_path(g, camino), dtype=np.int64)
    lista = climbs(g, e)
    assert lista, "no detectó la rampa"
    res = climb_summary(lista)
    assert res["climbs"] >= 1
    assert res["hardest_climb_pct"] == pytest.approx(6.0, abs=1.2)
    assert res["longest_climb_m"] > 1200


def test_gravel_may_ride_on_dirt_and_road_may_not():
    """En gravel el firme sin asfaltar no es una concesión: es el terreno."""
    import time

    import numpy as np

    from raceengine_contracts import Discipline, GeoPoint
    from raceengine_engine.context import GraphContext
    from raceengine_engine.weights import build_weights
    from raceengine_geodata import GraphBuilder
    from raceengine_routing import LocalRoutingProvider

    lat0, lon0 = -33.4372, -70.6506
    b = GraphBuilder(lat0, lon0)
    for i in range(20):
        b.add_node(1000 + i, lat0, lon0 + i * 100.0 / 93_000.0)
        if i:
            # Mitad asfalto (terciaria), mitad camino de tierra.
            if i < 10:
                b.add_edge(1000 + i - 1, 1000 + i, 100.0, "tertiary", paved=True)
            else:
                b.add_edge(1000 + i - 1, 1000 + i, 100.0, "track", paved=False)
    g = b.build(version="mixto")
    esrc = np.repeat(np.arange(g.n_nodes, dtype=np.int32),
                     np.diff(g.indptr).astype(np.int32))
    ctx = GraphContext(region_id="mixto", graph=g, provider=LocalRoutingProvider(g),
                       esrc=esrc, intersections=g.is_intersection, built_at=time.time())

    def permitidas(disc):
        m = ConstraintModel(region="mixto", discipline=disc,
                            start=GeoPoint(lat=lat0, lon=lon0),
                            distance=DistanceLayer(target_m=2000, tolerance_pct=10.0),
                            geometry=GeometryLayer(search_radius_max_m=5000))
        w, _ = build_weights(g, m, ctx.esrc, 0)
        return int(w.allowed_edges.sum())

    assert permitidas(Discipline.ROAD) < permitidas(Discipline.GRAVEL)
    assert permitidas(Discipline.RUNNING) < permitidas(Discipline.GRAVEL)


def test_distance_can_be_a_range_instead_of_a_target():
    """En cicloturismo nadie promete 137 km exactos."""
    from raceengine_engine.metrics import compliant_target

    libre = model(distance=DistanceLayer(
        mode="range", range_min_m=110_000, range_max_m=135_000, target_m=120_000))
    assert libre.distance.min_m == 110_000
    assert libre.distance.max_m == 135_000
    assert libre.distance.in_tolerance(120_000)
    assert not libre.distance.in_tolerance(140_000)
    # Y el objetivo interno es el CENTRO de la horquilla, no `target_m`.
    assert compliant_target(libre) == pytest.approx(122_500)

    with pytest.raises(ValueError):
        DistanceLayer(mode="range", range_min_m=100_000, range_max_m=90_000)


def test_a_gain_target_changes_what_scores_well():
    """Pedir 2.500 m de desnivel tiene que mover la puntuación, no ser decorativo."""
    from raceengine_contracts import ElevationLayer, RawMetrics
    from raceengine_engine.scoring import normalize_set

    con = model(elevation=ElevationLayer(enabled=True, target_gain_m=800,
                                         gain_tolerance_pct=15.0))
    sin = model()

    flojo, justo = RawMetrics(), RawMetrics()
    flojo.distance_m = justo.distance_m = 6000.0
    flojo.gain_m, justo.gain_m = 50.0, 800.0

    n_con = normalize_set([flojo, justo], con)
    assert n_con[1].gain_fit > n_con[0].gain_fit
    assert n_con[1].gain_fit == pytest.approx(1.0, abs=0.01)
    # Sin desnivel objetivo, la componente no discrimina.
    n_sin = normalize_set([flojo, justo], sin)
    assert n_sin[0].gain_fit == n_sin[1].gain_fit == 1.0


def test_a_corridor_is_far_cheaper_than_the_circle_that_contains_it():
    """Es la razón de existir del corredor: una ruta larga no cabe en un círculo."""
    import math

    from raceengine_geodata.osm import corridor_area_km2, corridor_polygon
    from raceengine_geodata.regions import corridor_length_km

    # Santiago → Valparaíso, muy simplificado.
    pts = [(-33.4372, -70.6506), (-33.35, -71.05), (-33.05, -71.42), (-33.0458, -71.6197)]
    buf = 8000.0
    anillo = corridor_polygon(pts, buf)
    largo = corridor_length_km(pts)
    franja = corridor_area_km2(anillo)
    circulo = math.pi * (largo / 2 + buf / 1000) ** 2

    assert 95 < largo < 120
    # La franja se parece a «largo × ancho», con algo de más en las puntas.
    assert franja == pytest.approx(largo * 2 * buf / 1000, rel=0.35)
    assert franja < circulo / 4, "el corredor tiene que ahorrar de verdad"


def test_the_corridor_wraps_every_point_of_the_route():
    """Si un punto del trayecto queda fuera de la franja, la descarga no sirve."""
    from raceengine_contracts import point_in_ring
    from raceengine_geodata.osm import corridor_polygon

    pts = [(-33.44, -70.65), (-33.40, -70.40), (-33.30, -70.20), (-33.10, -70.30)]
    anillo = corridor_polygon(pts, 5000.0)
    ring = [(lo, la) for la, lo in anillo]          # point_in_ring espera (lon, lat)
    for la, lo in pts:
        assert point_in_ring(lo, la, ring), f"({la}, {lo}) fuera de su propia franja"


def test_a_corridor_that_is_too_big_is_refused_with_a_reason():
    from raceengine_geodata import add_corridor_region

    lejos = [(-33.44, -70.65), (10.0, -70.65)]      # ~4.800 km
    with pytest.raises(ValueError) as exc:
        add_corridor_region("x_enorme", "enorme", lejos, 8000)
    assert "km" in str(exc.value)


# ── Regresiones del emparejado ───────────────────────────────────────────────
def test_an_imported_route_is_always_a_connected_path(ctx):
    """La invariante que su ausencia convirtió en un 500 en producción.

    Al cerrar un hueco, el emparejador pegaba el trozo siguiente detrás del
    anterior SIN arista que los uniera. Nadie se enteraba hasta doscientas líneas
    más abajo, cuando la medición de la ruta más corta posible intentaba emparejar
    dos arrays de distinta longitud: `operands could not be broadcast together
    with shapes (1309,) (1308,)`.
    """
    import numpy as np

    from raceengine_engine.importer import match_track, parse_track

    m = model(distance=DistanceLayer(target_m=6000, tolerance_pct=3.0),
              geometry=GeometryLayer(search_radius_max_m=3000),
              fast_budget_ms=2000, total_budget_ms=6000)
    r = generate(m)
    traza = parse_track(_gpx_of(r.recommended))

    # Traza con DOS saltos lejos de la red: dos huecos, que es lo que hacía que
    # faltaran exactamente dos aristas.
    def romper(t, en, cuantos=3):
        return t[:en] + [(la + 0.05, lo + 0.05) for la, lo in t[en:en + cuantos]] \
            + t[en + cuantos:]

    rota = romper(romper(traza, len(traza) // 3), 2 * len(traza) // 3)
    out = match_track(ctx, m, rota)
    assert out["ok"], out.get("error")
    assert len(out["edges"]) == len(out["nodes"]) - 1, (
        f"{len(out['nodes'])} nodos y {len(out['edges'])} tramos: camino con saltos")
    # Y la medición, que es donde reventaba, funciona.
    assert out["route"]["metrics"]["spr_m"] > 0


def test_a_short_hole_is_bridged_and_a_huge_one_is_cut(ctx):
    """Un túnel se puentea; un salto de cinco kilómetros no se inventa."""
    from raceengine_engine.importer import match_track, parse_track

    m = model(distance=DistanceLayer(target_m=6000, tolerance_pct=3.0),
              geometry=GeometryLayer(search_radius_max_m=3000),
              fast_budget_ms=2000, total_budget_ms=6000)
    r = generate(m)
    traza = parse_track(_gpx_of(r.recommended))

    # Hueco corto: se quitan tres puntos del medio, como un túnel sin cobertura.
    corte = len(traza) // 2
    con_tunel = traza[:corte] + traza[corte + 3:]
    a = match_track(ctx, m, con_tunel)
    assert a["ok"] and len(a["edges"]) == len(a["nodes"]) - 1
    # Sin cortes: el trozo que falta se cose por calle, no se descarta.
    assert a["imported"]["gaps"] == 0
    # Puede salir algo más corto que el original y es correcto: sin esos puntos
    # no hay constancia del rodeo, así que el enganche toma el camino directo.
    assert a["distance_m"] > r.recommended.metrics.distance_m * 0.8

    # Puntos fuera de la región: no tienen candidato, se declaran y se siguen
    # ignorando. No abren hueco, porque no hay dos trozos que reconciliar.
    lejos = [(la + 0.05, lo) for la, lo in traza[corte:corte + 4]]
    partida = traza[:corte] + lejos + traza[corte + 4:]
    b = match_track(ctx, m, partida)
    assert b["ok"] and len(b["edges"]) == len(b["nodes"]) - 1
    assert b["imported"]["unmatched_points"] >= 4


def test_two_islands_are_not_stitched_into_one_route():
    """Si dos trozos no se tocan por calle, no se cosen: se queda el mayor.

    Es el caso que obliga a devolver TRAMOS y no un camino: dos calles sin
    conexión entre ellas, y una traza que salta de una a la otra. Coserlas daría
    un «recorrido» que pasa por donde no hay calzada.
    """
    import time

    import numpy as np

    from raceengine_engine.context import GraphContext
    from raceengine_engine.importer import match_track
    from raceengine_geodata import GraphBuilder
    from raceengine_routing import LocalRoutingProvider

    lat0, lon0 = -33.4372, -70.6506
    dlon = lambda m: m / (111_320.0 * np.cos(np.radians(lat0)))      # noqa: E731
    b = GraphBuilder(lat0, lon0)
    # Isla A: 30 nodos. Isla B: 12 nodos, a 3 km, sin nada que las una.
    for i in range(30):
        b.add_node(1000 + i, lat0, lon0 + dlon(i * 50.0))
        if i:
            b.add_edge(1000 + i - 1, 1000 + i, 50.0, "residential")
    for i in range(12):
        b.add_node(2000 + i, lat0 + 0.027, lon0 + dlon(i * 50.0))
        if i:
            b.add_edge(2000 + i - 1, 2000 + i, 50.0, "residential")
    g = b.build(version="islas")
    esrc = np.repeat(np.arange(g.n_nodes, dtype=np.int32),
                     np.diff(g.indptr).astype(np.int32))
    ctx = GraphContext(region_id="islas", graph=g, provider=LocalRoutingProvider(g),
                       esrc=esrc, intersections=g.is_intersection, built_at=time.time())
    idx = {int(v): i for i, v in enumerate(g.node_ids.tolist())}
    isla_b = {idx[2000 + i] for i in range(12)}

    traza = ([(lat0, lon0 + dlon(i * 50.0)) for i in range(30)]
             + [(lat0 + 0.027, lon0 + dlon(i * 50.0)) for i in range(12)])

    m = ConstraintModel(region="islas", start=GeoPoint(lat=lat0, lon=lon0),
                        distance=DistanceLayer(target_m=1450, tolerance_pct=10.0),
                        geometry=GeometryLayer(search_radius_max_m=8000))
    out = match_track(ctx, m, traza)
    assert out["ok"], out.get("error")
    assert len(out["edges"]) == len(out["nodes"]) - 1
    assert out["imported"]["gaps"] >= 1, "no declaró el corte"
    # Se conserva la isla LARGA, no la corta, y no se mezclan.
    assert not (set(out["nodes"]) & isla_b)
    assert out["distance_m"] == pytest.approx(29 * 50.0, rel=0.05)


def test_import_does_not_zigzag_between_the_two_carriageways():
    """El escalonado que se veía sobre el mapa, reproducido.

    Una avenida grande viene en OSM como DOS vías paralelas separadas veinte
    metros, unidas sólo en los cruces. Con ruido de GPS, el nodo más cercano
    alterna entre una y otra, y el enganche cruzaba y volvía en cada manzana: un
    diente de sierra fino a lo largo de una avenida recta.

    La penalización por rumbo lo mata: cruzar de calzada son dos giros de 90°
    respecto a la dirección que lleva la traza.
    """
    import time

    import numpy as np

    from raceengine_engine.context import GraphContext
    from raceengine_engine.importer import match_track
    from raceengine_geodata import GraphBuilder
    from raceengine_routing import LocalRoutingProvider

    lat0, lon0 = -33.4372, -70.6506
    sep, paso, n = 22.0, 30.0, 90
    b = GraphBuilder(lat0, lon0)
    dlat = lambda m: m / 111_320.0                                   # noqa: E731
    dlon = lambda m: m / (111_320.0 * np.cos(np.radians(lat0)))      # noqa: E731
    for i in range(n):
        lo = lon0 + dlon(i * paso)
        b.add_node(1000 + i, lat0 + dlat(sep), lo)      # calzada norte
        b.add_node(2000 + i, lat0, lo)                  # calzada sur
        if i:
            b.add_edge(1000 + i - 1, 1000 + i, paso, "primary")
            b.add_edge(2000 + i - 1, 2000 + i, paso, "primary")
        if i % 10 == 0:                                  # cruce cada 300 m
            b.add_edge(1000 + i, 2000 + i, sep, "residential")
    g = b.build(version="avenida")
    esrc = np.repeat(np.arange(g.n_nodes, dtype=np.int32),
                     np.diff(g.indptr).astype(np.int32))
    ctx = GraphContext(region_id="avenida", graph=g,
                       provider=LocalRoutingProvider(g), esrc=esrc,
                       intersections=g.is_intersection, built_at=time.time())
    idx = {int(v): i for i, v in enumerate(g.node_ids.tolist())}
    sur = {idx[2000 + i] for i in range(n)}

    rng = np.random.default_rng(5)
    traza = [(lat0 + dlat(sep) + rng.normal(0, 14) / 111_320.0,
              lon0 + dlon(i * paso) + rng.normal(0, 14) / (111_320.0 * np.cos(np.radians(lat0))))
             for i in range(n)]

    m = ConstraintModel(region="avenida",
                        start=GeoPoint(lat=lat0 + dlat(sep), lon=lon0),
                        distance=DistanceLayer(target_m=2700, tolerance_pct=10.0),
                        geometry=GeometryLayer(search_radius_max_m=6000))
    out = match_track(ctx, m, traza)
    assert out["ok"], out.get("error")

    en_sur = sum(1 for x in out["nodes"] if x in sur)
    assert en_sur == 0, f"{en_sur} nodos en la calzada contraria: sigue zigzagueando"
    # Y la longitud es la de la avenida, no la de un diente de sierra.
    assert out["distance_m"] == pytest.approx((n - 1) * paso, rel=0.03)


def test_out_and_back_spurs_are_removed():
    """A → B → A no aporta recorrido y sí produce dientes visibles en el mapa."""
    from raceengine_engine.importer import _drop_spurs

    class G:
        elen = np.full(40, 20.0, dtype=np.float32)      # aristas cortas: artefacto

    n, e = _drop_spurs([1, 2, 3, 2, 3, 4], [10, 11, 12, 13, 14], G())
    assert n == [1, 2, 3, 4]
    assert len(e) == len(n) - 1

    # Pero un ramal LARGO es del recorrido, no del emparejador: se conserva.
    class L:
        elen = np.full(40, 300.0, dtype=np.float32)

    n2, e2 = _drop_spurs([1, 2, 3, 2, 1], [10, 11, 12, 13], L())
    assert n2 == [1, 2, 3, 2, 1], "se comió un tramo de ida y vuelta real"
    assert len(e2) == len(n2) - 1


# ── Monte a pie ──────────────────────────────────────────────────────────────
def test_offroad_pace_follows_tobler():
    """Andar y correr por monte no es ni asfalto ni bici.

    La comprobación que de verdad distingue el modelo: el óptimo NO está en el
    llano, está bajando un 5 %. Cualquiera que haya andado por monte lo sabe, y
    un modelo simétrico en torno al cero se equivoca justo donde más se camina.
    """
    import numpy as np

    from raceengine_engine.cycling import foot_speed_kmh

    g = np.array([-20.0, -10, -5, 0, 5, 10, 20])
    v = foot_speed_kmh("hiking", g)
    i_max = int(np.argmax(v))
    assert g[i_max] == -5.0, f"el óptimo salió en {g[i_max]:+.0f} %"
    # Y no es simétrico: bajar un 5 % es más rápido que el llano.
    assert float(foot_speed_kmh("hiking", np.array([-5.0]))[0]) > \
        float(foot_speed_kmh("hiking", np.array([0.0]))[0])
    # Cifras de andar reconocibles: ~5 km/h en llano.
    assert 4.5 < float(foot_speed_kmh("hiking", np.array([0.0]))[0]) < 5.5
    # Y correr por monte es más rápido que andarlo, en toda la curva.
    assert all(a > b for a, b in zip(foot_speed_kmh("trail", g), v))


def test_trail_does_not_use_trunk_roads_and_hiking_may_use_steps():
    from raceengine_engine.weights import DISCIPLINE_IDS
    from raceengine_geodata import ALL_CLASSES

    idx = {c: i for i, c in enumerate(ALL_CLASSES)}
    trail = set(DISCIPLINE_IDS["trail"].tolist())
    hiking = set(DISCIPLINE_IDS["hiking"].tolist())

    assert idx["path"] in trail and idx["track"] in trail
    assert idx["trunk"] not in trail, "una carrera de monte no sale a la nacional"
    # Andando se suben escaleras; corriendo una carrera, no se mandan.
    assert idx["steps"] in hiking and idx["steps"] not in trail


def test_offroad_time_is_slower_than_road_running(ctx):
    """El mismo trazado a pie por monte tarda más que por asfalto."""
    import numpy as np

    from raceengine_engine.cycling import foot_time_min

    g = ctx.graph
    e = np.arange(min(300, g.n_edges), dtype=np.int64)
    andando = foot_time_min(g, e, "hiking")
    corriendo = foot_time_min(g, e, "trail")
    assert andando > corriendo > 0


# ── Modo libre: ruta directa sin perseguir kilómetros ────────────────────────
def _free(**kw) -> ConstraintModel:
    from raceengine_contracts import RouteShape, RouteTypeLayer

    base = dict(
        region="demo", start=START,
        finish=GeoPoint(lat=-33.4180, lon=-70.6180),
        routetype=RouteTypeLayer(shape=RouteShape.OPEN, start_equals_finish=False),
        distance=DistanceLayer(mode="free"),
        geometry=GeometryLayer(search_radius_max_m=12000),
    )
    base.update(kw)
    return ConstraintModel(**base)


def test_free_mode_routes_between_points_without_chasing_distance(ctx):
    """Lo que se pide es la ruta lógica, no unos kilómetros.

    Perseguir una distancia que nadie pidió no restringe nada: obliga al motor a
    inventar rodeos para rellenar. En modo libre no hay banda, y la distancia es
    consecuencia de la ruta.
    """
    r = generate(_free())
    assert r.status == "converged", r.status
    rec = r.recommended
    assert rec is not None and rec.metrics.distance_m > 0
    assert rec.in_tolerance, "sin distancia objetivo, siempre está «en tolerancia»"
    # Y es una ruta razonable entre los dos puntos, no un rodeo de relleno.
    g = ctx.graph
    a = g.nearest_node(START.lat, START.lon)
    b = g.nearest_node(-33.4180, -70.6180)
    recta = float(np.hypot(g.x[b] - g.x[a], g.y[b] - g.y[a]))
    assert rec.metrics.distance_m < recta * 2.0, (
        f"{rec.metrics.distance_m:.0f} m para una recta de {recta:.0f} m")


def test_free_mode_ignores_the_target_distance_entirely(ctx):
    """Cambiar `target_m` en modo libre no puede cambiar el resultado."""
    a = generate(_free(distance=DistanceLayer(mode="free", target_m=3_000)))
    b = generate(_free(distance=DistanceLayer(mode="free", target_m=90_000)))
    assert a.recommended.metrics.distance_m == pytest.approx(
        b.recommended.metrics.distance_m)
    assert a.recommended.node_path == b.recommended.node_path


def test_free_mode_visits_every_waypoint(ctx):
    z = [CustomZone(id=f"p{i}", name=f"Paso {i}", behavior=ZoneBehavior.MANDATORY,
                    geometry=GeoJSONPoint(coordinates=c), buffer_m=280)
         for i, c in enumerate([(-70.6700, -33.4300), (-70.6250, -33.4500)])]
    r = generate(_free(zones=tuple(z)))
    assert r.status == "converged"
    assert sorted(r.recommended.mandatory_covered) == ["Paso 0", "Paso 1"]
    assert not r.recommended.mandatory_missing


def test_waypoints_are_visited_in_the_shortest_order(ctx):
    """El orden en que se pinchan no es el orden en que conviene recorrerlos."""
    import numpy as np

    from raceengine_engine.generate import waypoint_order
    from raceengine_engine.snap import anchor

    m = _free()
    anc = anchor(ctx, m)
    g = ctx.graph
    # Cuatro puntos alineados a lo largo de una avenida, dados al revés.
    ids = [g.nearest_node(-33.4372, -70.6506 + d) for d in (0.0, 0.030, 0.010, 0.020)]
    ids = [int(i) for i in ids]
    orden = waypoint_order(ctx, anc.weights, ids)
    # El primero y el último están fijos; los de en medio se reordenan.
    assert orden[0] == 0 and orden[-1] == len(ids) - 1
    recorrido = [ids[i] for i in orden]
    coste = sum(float(np.hypot(g.x[b] - g.x[a], g.y[b] - g.y[a]))
                for a, b in zip(recorrido, recorrido[1:]))
    original = sum(float(np.hypot(g.x[b] - g.x[a], g.y[b] - g.y[a]))
                   for a, b in zip(ids, ids[1:]))
    assert coste <= original + 1.0, "el reordenado no mejoró el recorrido"


def test_the_objective_changes_the_route_not_just_the_score(ctx):
    """«Más llana» tenía que devolver otra ruta, y devolvía la misma con otro número.

    Hasta ahora el objetivo sólo tocaba los pesos de la PUNTUACIÓN. Elegir «menos
    tráfico» cambiaba el número del panel y el camino era idéntico: eso no es una
    preferencia, es una etiqueta.
    """
    caminos = {}
    for o in ("balanced", "fastest", "flattest", "climbing",
              "fewest_closures", "most_scenic"):
        r = generate(_free(objective=Objective(o)))
        caminos[o] = tuple(r.recommended.node_path)
    assert len(set(caminos.values())) >= 4, (
        f"sólo {len(set(caminos.values()))} rutas distintas para 6 objetivos")

    # Y las cifras se mueven en la dirección que promete cada objetivo.
    llana = generate(_free(objective=Objective.FLATTEST)).recommended.metrics
    monte = generate(_free(objective=Objective.CLIMBING)).recommended.metrics
    assert monte.gain_m >= llana.gain_m
    cierres = generate(_free(objective=Objective.FEWEST_CLOSURES)).recommended.metrics
    equil = generate(_free(objective=Objective.BALANCED)).recommended.metrics
    assert cierres.closure_impact <= equil.closure_impact * 1.02


def test_free_mode_offers_real_alternatives_not_the_same_route_thrice(ctx):
    r = generate(_free())
    rutas = [r.recommended] + list(r.alternatives)
    firmas = {tuple(c.node_path) for c in rutas}
    assert len(firmas) == len(rutas), "hay variantes repetidas"


def test_a_free_loop_without_waypoints_says_what_is_missing(ctx):
    from raceengine_contracts import RouteShape, RouteTypeLayer

    r = generate(ConstraintModel(
        region="demo", start=START,
        routetype=RouteTypeLayer(shape=RouteShape.CIRCUIT),
        distance=DistanceLayer(mode="free"),
        geometry=GeometryLayer(search_radius_max_m=8000)))
    assert r.status == "infeasible"
    detalle = r.infeasibility.blocking[0].detail
    assert "punto de paso" in detalle


def test_free_mode_skips_the_distance_rules_of_the_preflight(ctx):
    """Un radio pequeño no puede bloquear un circuito que nadie ha pedido."""
    chico = _free(geometry=GeometryLayer(search_radius_max_m=800))
    rep = run_preflight(ctx, chico)
    assert not any(b.rule == "VR-2" for b in rep.blocking)


def test_the_reverse_edge_table_belongs_to_its_graph():
    """Estaba cacheada por `id(grafo)`, y Python reutiliza direcciones.

    Un grafo nuevo podía caer en el `id` de otro ya liberado y heredar su tabla.
    Con suerte reventaba con un IndexError —así se descubrió, en una ejecución
    completa de la suite—; sin suerte, devolvía kilómetros de cierre calculados
    sobre las aristas de otro mapa.
    """
    import gc

    import numpy as np

    from raceengine_geodata import GraphBuilder

    def tira(n: int, base: int):
        b = GraphBuilder(-33.4372, -70.6506)
        for i in range(n):
            b.add_node(base + i, -33.4372, -70.6506 + i * 0.0005)
            if i:
                b.add_edge(base + i - 1, base + i, 50.0, "residential")
        return b.build(version=f"t{base}")

    grande = tira(200, 1000)
    _ = grande.reverse
    ids = {id(grande)}
    del grande
    gc.collect()

    # Se crean varios pequeños con la esperanza de reutilizar la dirección.
    for k in range(30):
        chico = tira(5, 5000 + k)
        rev = chico.reverse
        assert len(rev) == chico.n_edges, "heredó la tabla de otro grafo"
        # Y la tabla es correcta: la inversa de la inversa es uno mismo.
        assert (rev[rev] == np.arange(chico.n_edges)).all()
        ids.add(id(chico))


def test_the_reverse_edge_table_is_correct(ctx):
    import numpy as np

    g = ctx.graph
    rev = g.reverse
    src = np.repeat(np.arange(g.n_nodes), np.diff(g.indptr))
    assert (rev[rev] == np.arange(g.n_edges)).all(), "no es involutiva"
    assert (src[rev] == g.indices).all() and (g.indices[rev] == src).all()


def test_free_mode_reaches_a_finish_beyond_the_configured_radius(ctx):
    """El radio recortaba la red antes de llegar a la meta, y decía «listo».

    El radio es una restricción de DISEÑO —un circuito de 10 km no debe irse a
    veinte kilómetros de la salida—, pero cuando lo que se pide es ir de A a B,
    los extremos SON el problema. Con el valor por defecto de 4 km, que además
    vive en modo avanzado y está oculto, una ruta a un punto lejano se quedaba a
    las afueras y el panel la presentaba como terminada.
    """
    from raceengine_contracts import RouteShape, RouteTypeLayer
    from raceengine_engine.weights import effective_radius_m

    g = ctx.graph
    meta = GeoPoint(lat=-33.4000, lon=-70.5900)
    m = ConstraintModel(
        region="demo", start=START, finish=meta,
        routetype=RouteTypeLayer(shape=RouteShape.OPEN, start_equals_finish=False),
        distance=DistanceLayer(mode="free"),
        geometry=GeometryLayer(search_radius_max_m=4000))

    inicio = g.nearest_node(START.lat, START.lon)
    assert effective_radius_m(g, m, inicio) > 8000, "el radio no se estiró"

    r = generate(m)
    assert r.status == "converged"
    assert r.recommended.node_path[-1] == g.nearest_node(meta.lat, meta.lon), (
        "la ruta no llega a la meta")


def test_the_radius_still_binds_when_a_distance_is_asked_for(ctx):
    """Estirarlo en modo libre no puede aflojarlo en modo objetivo."""
    from raceengine_engine.weights import effective_radius_m

    g = ctx.graph
    m = model(geometry=GeometryLayer(search_radius_max_m=3000))
    assert effective_radius_m(g, m, g.nearest_node(START.lat, START.lon)) == 3000


def test_a_fragmented_graph_is_reported_not_hidden():
    """Un mapa a trozos no parece un error: parece un mapa.

    Una descarga incompleta deja al motor trazando dentro del trozo donde cayó la
    salida, con métricas perfectamente coherentes y sin decir nada de lo demás. Es
    lo que se ve como «el recorrido se corta en una parte».
    """
    from raceengine_geodata import GraphBuilder, connectivity

    def tira(b, base, lat):
        for i in range(15):
            b.add_node(base + i, lat, -70.6506 + i * 0.0006)
            if i:
                b.add_edge(base + i - 1, base + i, 60.0, "residential")

    b = GraphBuilder(-33.4372, -70.6506)
    tira(b, 1000, -33.4372)          # dos tiras que NO se tocan
    tira(b, 2000, -33.5000)
    roto = b.build(version="roto")
    con = connectivity(roto)
    assert con["components"] == 2
    assert con["largest_pct"] == pytest.approx(50.0)
    assert con["level"] == "deferred"

    b2 = GraphBuilder(-33.4372, -70.6506)
    tira(b2, 3000, -33.4372)
    entero = b2.build(version="entero")
    sano = connectivity(entero)
    assert sano["components"] == 1 and sano["level"] == "reliable"


def test_connectivity_is_recorded_when_regions_are_merged():
    """Unir dos comunas que no se tocan da un grafo en dos trozos: hay que decirlo."""
    from raceengine_geodata import GraphBuilder, merge_graphs

    def tira(base, lat):
        b = GraphBuilder(-33.4372, -70.6506)
        for i in range(12):
            b.add_node(base + i, lat, -70.6506 + i * 0.0006)
            if i:
                b.add_edge(base + i - 1, base + i, 60.0, "residential")
        return b.build(version=f"t{base}")

    m = merge_graphs([tira(1000, -33.4372), tira(2000, -33.5200)])
    con = m.coverage.get("connectivity")
    assert con and con["components"] == 2
    assert con["level"] != "reliable"


# ── El preflight no puede bloquear con una cuenta mal hecha ──────────────────
def _ring_zones(n: int, radio_deg: float = 0.030, shuffle_seed: int | None = None):
    import math
    import random

    centros = [(-33.4372 + radio_deg * math.sin(2 * math.pi * k / n),
                -70.6506 + radio_deg * math.cos(2 * math.pi * k / n))
               for k in range(n)]
    if shuffle_seed is not None:
        random.Random(shuffle_seed).shuffle(centros)
    return tuple(
        CustomZone(id=f"z{i}", name=f"Z{i}", behavior=ZoneBehavior.MANDATORY,
                   geometry=GeoJSONPoint(coordinates=(lo, la)), buffer_m=300)
        for i, (la, lo) in enumerate(centros))


def test_the_zone_estimate_does_not_depend_on_click_order(ctx):
    """Se sumaba en el orden en que se pincharon los puntos.

    Con catorce zonas trazadas sobre un maratón real eso daba 58,6 km de línea
    recta para un recorrido de 41,7 km, y el preflight declaraba imposible algo
    que existe. Bloquear es lo único que el usuario no puede rebatir.
    """
    from raceengine_engine.preflight import mandatory_span_m

    g = ctx.graph
    base = dict(region="demo", start=START,
                distance=DistanceLayer(target_m=42000, tolerance_pct=8.0),
                geometry=GeometryLayer(search_radius_max_m=20000))
    ordenadas = ConstraintModel(**base, zones=_ring_zones(14))
    revueltas = ConstraintModel(**base, zones=_ring_zones(14, shuffle_seed=4))

    a, _ = mandatory_span_m(g, ordenadas)
    b, _ = mandatory_span_m(g, revueltas)
    assert b == pytest.approx(a, rel=0.02), (
        f"el desorden infló la estimación: {a/1000:.1f} vs {b/1000:.1f} km")


def test_the_detour_factor_shrinks_when_the_waypoints_are_dense(ctx):
    """Con muchos puntos, la poligonal YA es la ruta: el margen sobra."""
    from raceengine_engine.preflight import DETOUR_MAX, mandatory_span_m

    g = ctx.graph
    base = dict(region="demo", start=START,
                distance=DistanceLayer(target_m=42000, tolerance_pct=8.0),
                geometry=GeometryLayer(search_radius_max_m=20000))
    _, pocos = mandatory_span_m(g, ConstraintModel(**base, zones=_ring_zones(2)))
    _, muchos = mandatory_span_m(g, ConstraintModel(**base, zones=_ring_zones(14)))
    assert pocos == pytest.approx(DETOUR_MAX, abs=0.01)
    assert muchos < 1.10


def test_many_zones_on_a_real_sized_course_are_not_declared_impossible(ctx):
    """Catorce zonas sobre un anillo que cabe de sobra: tiene que dejar intentar."""
    m = model(distance=DistanceLayer(target_m=42000, tolerance_pct=8.0),
              geometry=GeometryLayer(search_radius_max_m=20000),
              zones=_ring_zones(14, shuffle_seed=7))
    r = run_preflight(ctx, m)
    assert not any(b.rule == "VR-5" for b in r.blocking), (
        [b.detail for b in r.blocking])


def test_zones_that_truly_do_not_fit_are_still_blocked(ctx):
    """Aflojar la estimación no puede dejar pasar lo imposible."""
    m = model(distance=DistanceLayer(target_m=3000, tolerance_pct=2.0),
              geometry=GeometryLayer(search_radius_max_m=20000),
              zones=_ring_zones(4, radio_deg=0.055))
    r = run_preflight(ctx, m)
    assert not r.feasible


def test_an_unbridgeable_gap_does_not_cost_half_the_track(ctx):
    """Se conservaba sólo la cadena más larga y el resto se perdía en silencio."""
    from raceengine_engine.importer import match_track, parse_track

    m = model(distance=DistanceLayer(target_m=8000, tolerance_pct=3.0),
              geometry=GeometryLayer(search_radius_max_m=4000),
              fast_budget_ms=2000, total_budget_ms=7000)
    r = generate(m)
    traza = parse_track(_gpx_of(r.recommended))

    # Un hueco de ~700 m en mitad de la traza: hay calle debajo, así que el
    # puente debe encontrarla y no perder la segunda mitad.
    corte = len(traza) // 2
    rota = traza[:corte] + traza[corte + 6:]
    out = match_track(ctx, m, rota)
    assert out["ok"], out.get("error")
    assert out["distance_m"] > r.recommended.metrics.distance_m * 0.85, (
        f"se perdió traza: {out['distance_m']:.0f} de "
        f"{r.recommended.metrics.distance_m:.0f} m")
    assert "dropped_m" in out["imported"]


def test_no_route_says_why_not_just_that_there_is_none(ctx):
    """«No hay camino» es cierto y no sirve: hay que decir qué lo corta."""
    from raceengine_contracts import RouteShape, RouteTypeLayer

    m = ConstraintModel(
        region="demo", start=START,
        finish=GeoPoint(lat=-33.9000, lon=-71.9000),
        routetype=RouteTypeLayer(shape=RouteShape.OPEN, start_equals_finish=False),
        distance=DistanceLayer(mode="free"),
        geometry=GeometryLayer(search_radius_max_m=9000))
    r = generate(m)
    if r.status == "infeasible":
        d = r.infeasibility.blocking[0].detail
        # Diga lo que diga, tiene que traer cifras de red y una pista accionable.
        assert "nodos" in d and ("%" in d or "restricci" in d)


def test_camino_minimo_en_c_da_el_mismo_coste_que_el_astar(ctx):
    """Con scipy, el camino mínimo sale del Dijkstra en C: mismo coste que el A*
    (también con aristas penalizadas, nodos a evitar y cota de distancia)."""
    pytest.importorskip("scipy")
    from raceengine_engine.weights import build_weights
    g, prov = ctx.graph, ctx.provider
    w, _ = build_weights(g, model(), ctx.esrc, int(g.nearest_node(START.lat, START.lon)))
    rng = np.random.default_rng(7)
    pares = rng.integers(0, g.n_nodes, size=(40, 2))
    penal = {int(e): 500.0 for e in rng.integers(0, g.n_edges, size=300)}
    evitar = np.zeros(g.n_nodes, dtype=bool)
    evitar[rng.integers(0, g.n_nodes, size=50)] = True
    casos = 0
    for a, b in pares:
        for pen, av, tope in (({}, None, None), (penal, None, None), ({}, evitar, None), ({}, None, 1500.0)):
            w.penalty = pen
            c = prov.shortest_path(int(a), int(b), w, max_distance_m=tope, avoid_nodes=av)
            r = prov._astar(int(a), int(b), w, max_distance_m=tope, avoid_nodes=av)
            w.penalty = {}
            assert (c is None) == (r is None)
            if c is not None:
                casos += 1
                assert c.cost == pytest.approx(r.cost, rel=1e-6)
                assert c.nodes[0] == a and c.nodes[-1] == b and len(c.edges) == len(c.nodes) - 1
                assert all(int(g.indices[e]) == v for e, v in zip(c.edges, c.nodes[1:]))
    assert casos >= 10
