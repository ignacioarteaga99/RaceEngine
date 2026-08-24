"""Engine V2 — orquestador del pipeline.

    preflight → LoopGenerator → MandatoryRepair → DistanceAdjuster
              → Metrics → Scoring → Ranker → [INCUMBENTE]
              → AnytimeImprove (bucle monótono)

Propiedades del contrato anytime (doc 11 §3.7):
  1. Monotonía: el incumbente solo se sustituye si el score global mejora.
  2. Publicación incremental: cada mejora se emite por callback (→ SSE).
  3. Cancelación cooperativa: se comprueba el token en cada iteración.
  4. Ninguna etapa devuelve "nada": todas tienen salida degradada válida.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from raceengine_contracts import (
    Attribution, BlockingConstraint, ConstraintModel, GenerationResult, Incumbent,
    InfeasibilityReport, Provenance, RouteCandidate, RouteShape, Stage,
)

from .context import GraphContext, get_context
from .determinism import ENGINE_VERSION, candidate_set_hash, make_seed, seeded_rng
from .generate import (
    RawLoop, generate_loops, generate_open_routes, mandatory_seed_anchors,
    route_chain,
)
from .improve.archive import ParetoArchive
from .improve.operators import OPERATORS, OperatorCredit
from .messages import msg
from .metrics import (
    compliant_distance, compliant_target, compute_metrics, hard_violations,
    in_tolerance, ops_flags_for, target_aim,
)
from .preflight import run_preflight
from .repair import adjust_distance, evaluate_all, repair_mandatory, zone_masks
from .scoring import compute_indices, explain, explain_all, global_score, normalize_set
from .snap import anchor

UpdateFn = Callable[[str, GenerationResult], None]


@dataclass
class GenerationHandle:
    generation_id: str
    cancel: threading.Event = field(default_factory=threading.Event)
    result: GenerationResult | None = None


def _coords_for(ctx: GraphContext, nodes: list[int]) -> list[tuple[float, float, float]]:
    g = ctx.graph
    n = np.asarray(nodes, dtype=np.int64)
    return [
        (round(float(lo), 6), round(float(la), 6), round(float(el), 1))
        for lo, la, el in zip(g.lon[n], g.lat[n], g.elev[n])
    ]


def _candidate_from_loop(ctx: GraphContext, model: ConstraintModel, loop: RawLoop,
                         masks: dict, seed: str, generator: str) -> RouteCandidate:
    g = ctx.graph
    m = compute_metrics(g, loop.nodes, loop.edges, model, g.coverage)
    rc = RouteCandidate(
        route_id=uuid.uuid4().hex[:12],
        coordinates=_coords_for(ctx, loop.nodes),
        node_path=loop.nodes,
        metrics=m,
    )
    rc.in_tolerance = in_tolerance(m, model)
    rc.hard_violations = hard_violations(m, model, ctx.elevation_available())
    rc.ops_flags = ops_flags_for(m, model)

    touches = evaluate_all(g, loop.nodes, loop.edges, model.mandatory_zones,
                           masks, model.touch_policy, model.lang)
    rc.mandatory_covered = [t.name for t in touches if t.touched]
    rc.mandatory_missing = [t.name for t in touches if not t.touched]
    for t in touches:
        if t.touched:
            # Las violaciones de "no se pasa" ya se reflejan en mandatory_missing;
            # aquí solo interesan las de visitas repetidas o permanencia excesiva.
            rc.hard_violations.extend(t.violations)

    rc.provenance = Provenance(
        seed=seed, graph_version=ctx.version, engine_version=ENGINE_VERSION,
        generator=generator or loop.generator,
    )
    return rc


def _score_all(candidates: list[RouteCandidate], model: ConstraintModel) -> None:
    """Normaliza el conjunto y calcula índices y score global para cada candidato."""
    if not candidates:
        return
    norms = normalize_set([c.metrics for c in candidates], model)
    for c, nm in zip(candidates, norms):
        c.normalized = nm
        c.indices = compute_indices(c.metrics, nm)
        c.breakdown = global_score(nm, c.indices, model)
        # Penalización, no descarte, por violar restricciones duras: así el motor
        # siempre tiene algo que devolver y el usuario ve POR QUÉ falla (FR-3.5).
        penalty = 12.0 * len(c.hard_violations) + 8.0 * len(c.mandatory_missing)
        c.global_score = max(0.0, c.breakdown.global_score - penalty)
        c.breakdown.global_score = c.global_score
        c.breakdown.explanation_i18n = explain_all(c.metrics, c.indices, c.breakdown, model)
        if c.hard_violations:
            for lg, lines in c.breakdown.explanation_i18n.items():
                lines.append(msg("expl_hard", lg, list="; ".join(c.hard_violations)))
        c.breakdown.explanation = list(
            c.breakdown.explanation_i18n.get(model.lang, []))


# Una ruta con un error de distancia por encima de este múltiplo de la banda de
# tolerancia no es "una ruta un poco larga": es otra ruta. Nunca debe ganar por
# cubrir mejor las zonas obligatorias — ambas restricciones son duras.
EGREGIOUS_MULTIPLE = 3.0
EGREGIOUS_FLOOR_PCT = 0.08


def _distance_gate(target_m: float, tol_m: float) -> float:
    return max(EGREGIOUS_MULTIPLE * tol_m, EGREGIOUS_FLOOR_PCT * target_m)


# ── Presupuesto en iteraciones (modo reproducible) ──────────────────────────
#
# El coste de una iteración de mejora se modela como
#
#     ms_iter = C · astar_ms · km^0,4
#
# donde `astar_ms` lo mide cada región al cargarse (ver context.calibrate) y el
# exponente y la constante salen de medir el coste MARGINAL por iteración sobre
# la región de referencia (300 vs 1.500 iteraciones, para aislar la fase rápida):
# 11,5 ms/iteración a 5 km, 24,9 a 10 km y 31,3 a 21 km.
#
# La primera versión de esto usaba una constante fija en vez de `astar_ms`, y por
# eso fallaba justo donde importa: calibrada sobre 4.761 nodos, pedía el mismo
# trabajo sobre una ciudad de 120.000, tardaba tres veces el presupuesto, saltaba
# la red de seguridad y el resultado dejaba de ser reproducible. Escalar por el
# coste real de una consulta en ESE grafo es lo que arregla el caso grande.
ITER_C = 17.0
ITER_KM_EXP = 0.40
# Red de seguridad: si aun así la máquina va mucho más lenta, se corta. El
# resultado deja de ser reproducible y se dice en el panel.
REPRO_SAFETY_FACTOR = 2.5
MIN_REPRO_ITERS = 40
MAX_REPRO_ITERS = 60_000


def ms_per_iteration(model: ConstraintModel, ctx: GraphContext) -> float:
    km = max(model.distance.target_m / 1000.0, 0.5)
    return max(0.5, ITER_C * max(ctx.astar_ms, 0.05) * km ** ITER_KM_EXP)


def iteration_budget(model: ConstraintModel, ctx: GraphContext) -> int:
    """Cuántas iteraciones de mejora caben en el presupuesto pedido."""
    n = int(round(model.total_budget_ms / ms_per_iteration(model, ctx)))
    return max(MIN_REPRO_ITERS, min(MAX_REPRO_ITERS, n))


def _feasible_first(c: RouteCandidate, model: ConstraintModel | None = None) -> tuple:
    """Orden interno. Distancia y cobertura son AMBAS duras.

    El error de distancia desmedido se evalúa antes que la cobertura: si una ruta
    se pasa un 50% del objetivo, no sirve por muchos checkpoints que toque. El
    score global decide dentro de cada clase de factibilidad.
    """
    egregious = 0
    if model is not None:
        ref = compliant_target(model)
        gate = _distance_gate(ref, model.distance.tolerance_m)
        egregious = 1 if abs(compliant_distance(c.metrics, model) - ref) > gate else 0
    return (
        egregious,
        0 if not c.mandatory_missing else 1,
        0 if not c.hard_violations else 1,
        0 if c.in_tolerance else 1,
        -c.global_score,
    )


def _lap_model(model: ConstraintModel) -> ConstraintModel:
    """El modelo de UNA vuelta: mismo todo, distancia dividida.

    La tolerancia se expresa en porcentaje, así que se conserva tal cual y al
    multiplicar por el número de vueltas vuelve a dar la banda pedida.
    """
    from raceengine_contracts import DistanceLayer

    n = model.routetype.laps
    d = model.distance
    return model.model_copy(update={
        "distance": DistanceLayer(
            target_m=d.target_m / n, tolerance_pct=d.tolerance_pct,
            unit=d.unit, measurement=d.measurement),
    })


def expand_laps(ctx: GraphContext, model: ConstraintModel, cand: RouteCandidate,
                nodes: list[int], edges: list[int], masks: dict) -> RouteCandidate:
    """Repite la vuelta N veces y recalcula todo contra el modelo REAL.

    Los cruces se multiplican —se pasa N veces por cada uno, y eso es trabajo de
    personal real— pero los cierres no: la calle se corta una vez. Esa asimetría
    es exactamente la ventaja operativa de un circuito de vueltas, y el índice de
    logística tiene que reflejarla.
    """
    n = model.routetype.laps
    if n <= 1:
        return cand
    full_nodes: list[int] = list(nodes)
    full_edges: list[int] = list(edges)
    for _ in range(n - 1):
        full_nodes.extend(nodes[1:])
        full_edges.extend(edges)

    g = ctx.graph
    m = compute_metrics(g, full_nodes, full_edges, model, g.coverage)
    # La reutilización de tramos se mide DENTRO de una vuelta: repetir la vuelta
    # es la definición del formato, no una violación.
    m.segment_reuse_pct = cand.metrics.segment_reuse_pct

    out = RouteCandidate(
        route_id=cand.route_id, coordinates=_coords_for(ctx, full_nodes),
        node_path=full_nodes, metrics=m,
    )
    out.in_tolerance = in_tolerance(m, model)
    out.hard_violations = hard_violations(m, model, ctx.elevation_available())
    out.ops_flags = ops_flags_for(m, model)
    touches = evaluate_all(g, full_nodes, full_edges, model.mandatory_zones,
                           masks, model.touch_policy, model.lang)
    out.mandatory_covered = [t.name for t in touches if t.touched]
    out.mandatory_missing = [t.name for t in touches if not t.touched]
    out.provenance = cand.provenance
    return out


def edges_for_path(g, nodes: list[int]) -> list[int]:
    """Índices de arista de un camino dado por sus nodos."""
    out: list[int] = []
    for a, b in zip(nodes, nodes[1:]):
        lo, hi = int(g.indptr[a]), int(g.indptr[a + 1])
        seg = g.indices[lo:hi]
        w = np.nonzero(seg == b)[0]
        if not len(w):
            return []
        out.append(lo + int(w[0]))
    return out


def generate(
    model: ConstraintModel,
    handle: GenerationHandle | None = None,
    on_update: UpdateFn | None = None,
) -> GenerationResult:
    """Punto de entrada. Envuelve el núcleo con la lógica de vueltas.

    Un recorrido de N vueltas se resuelve generando UNA vuelta con la distancia
    dividida y repitiéndola. Es correcto y es barato: el espacio de búsqueda de
    una vuelta de 14 km es enormemente menor que el de un circuito de 42, y el
    resultado es el que el organizador quería en primer lugar.

    Durante el streaming se emite la vuelta; al terminar se expande. La geometría
    dibujada es idéntica —repetir una vuelta no cambia el trazado— así que el
    usuario no ve ningún salto: sólo se actualizan las métricas.
    """
    laps = model.routetype.laps
    if laps <= 1:
        return _generate_core(model, handle, on_update)

    ctx = get_context(model.region)
    work = _lap_model(model)

    # El «done» del núcleo se traga: describe UNA vuelta. El cliente cierra el
    # stream en cuanto lo recibe, así que si se dejara pasar nunca vería el
    # recorrido completo. El final lo emite este envoltorio, ya expandido.
    def relay(event: str, res: GenerationResult) -> None:
        if event == "done" or not on_update:
            return
        on_update(event, res)

    result = _generate_core(work, handle, relay)
    masks = zone_masks(ctx, model)

    def expand(c: RouteCandidate | None) -> RouteCandidate | None:
        if c is None:
            return None
        ed = edges_for_path(ctx.graph, c.node_path)
        if not ed:
            return c
        return expand_laps(ctx, model, c, c.node_path, ed, masks)

    result.recommended = expand(result.recommended)
    result.alternatives = [x for x in (expand(a) for a in result.alternatives) if x]
    _score_all([result.recommended] + result.alternatives if result.recommended
               else result.alternatives, model)
    if result.recommended:
        result.log.append(
            f"[expansión] {laps} vueltas de "
            f"{work.distance.target_m / 1000:.3f} km → "
            f"{result.recommended.metrics.distance_m / 1000:.3f} km · "
            f"cierres {result.recommended.metrics.closure_len_m / 1000:.2f} km "
            f"(se cortan una vez, se pasa {laps})")
    # El núcleo ya emitió su «done» con la VUELTA. Hay que volver a emitir con el
    # recorrido completo o el cliente se queda con la métrica de una vuelta.
    if on_update:
        on_update("done", result)
    return result


def _why_no_route(ctx, model, anc) -> BlockingConstraint:
    """Explica POR QUÉ no hay camino, en vez de decir que no lo hay.

    «No hay camino por calle entre los puntos marcados» es cierto y no sirve de
    nada: no dice si falta media descarga, si la meta cayó en una isla o si es una
    restricción del usuario la que corta. El árbol de alcance desde la salida ya
    está calculado, así que el diagnóstico es gratis: se mira cuánta red alcanza
    la salida y si la meta está dentro o fuera.
    """
    g = ctx.graph
    alcanza = int(np.isfinite(anc.reach).sum())
    total = int(g.n_nodes)
    fin = anc.finish if model.routetype.shape == RouteShape.OPEN else anc.start
    aparte = []
    if fin is not None and fin >= 0 and not np.isfinite(anc.reach[fin]):
        aparte.append("meta")
    masks = zone_masks(ctx, model)
    for z in model.mandatory_zones:
        m = masks.get(z.id or z.name)
        if m is not None and m.any() and not np.isfinite(anc.reach[m]).any():
            aparte.append(z.name or z.id)

    clave = "pf_no_route_why" if aparte else "pf_no_route_constraints"
    return BlockingConstraint(
        id="route", rule="reachability",
        detail=msg(clave, model.lang,
                   n=alcanza, t=total, p=100.0 * alcanza / max(total, 1),
                   list=", ".join(aparte) if aparte else "—"),
        suggested_relaxation={
            "geometry.search_radius_max_m": int(
                min(150_000, max(model.geometry.search_radius_max_m * 2, 20_000))),
        },
    )


def _generate_direct(ctx, model, result, emit, log, elapsed, handle):
    """Pipeline de ruta directa: salida → puntos de paso → meta.

    Sin generación de circuitos, sin ajuste de distancia y sin banda de
    tolerancia, porque no hay distancia que perseguir. Lo que decide el camino es
    el OBJETIVO —más llana, más corta, menos tráfico— traducido a pesos de arista.

    Se devuelven varias variantes penalizando lo ya recorrido: entre dos puntos
    casi siempre hay más de una respuesta razonable, y enseñar sólo una obliga al
    usuario a fiarse.
    """
    from .generate import direct_routes

    emit("stage", "generating", 0.15)
    anc = anchor(ctx, model)
    masks = zone_masks(ctx, model)
    loops = direct_routes(ctx, model, anc, masks)
    if not loops:
        result.status = "infeasible"
        result.infeasibility = InfeasibilityReport(
            feasible=False,
            blocking=[_why_no_route(ctx, model, anc)],
        )
        emit("done", "infeasible", 1.0)
        return result

    seed = result.seed
    cands = [_candidate_from_loop(ctx, model, lp, masks, seed, lp.generator)
             for lp in loops]
    _score_all(cands, model)
    cands.sort(key=lambda c: _feasible_first(c, model))

    # Se renumera DESPUÉS de ordenar. La variante que sale mejor no es siempre la
    # primera que se calculó, y llamar «direct_alt2» a la recomendada es cierto
    # como historia y confuso como etiqueta.
    for i, c in enumerate(cands):
        c.provenance.generator = "direct" if i == 0 else f"direct_alt{i}"

    result.recommended = cands[0]
    result.alternatives = cands[1:]
    result.status = "converged"
    result.reproducible = True
    log(f"ruta directa · {len(cands)} variantes · "
        f"{result.recommended.metrics.distance_m / 1000:.2f} km · "
        f"conjunto {candidate_set_hash(cands)}")
    emit("done", "converged", 1.0)
    return result


def _generate_core(
    model: ConstraintModel,
    handle: GenerationHandle | None = None,
    on_update: UpdateFn | None = None,
) -> GenerationResult:
    """Ejecuta el pipeline completo. Bloqueante; el worker la corre en un hilo."""
    t0 = time.perf_counter()
    handle = handle or GenerationHandle(generation_id=uuid.uuid4().hex)
    ctx = get_context(model.region)
    g = ctx.graph
    seed = make_seed(model, ctx.version)
    rng = seeded_rng(seed)

    result = GenerationResult(
        generation_id=handle.generation_id,
        seed=seed,
        graph_version=ctx.version,
        engine_version=ENGINE_VERSION,
        attribution=Attribution(
            text="© OpenStreetMap contributors (ODbL)",
            elevation=g.coverage.get("elevation", {}).get("source", ""),
        ),
    )

    def elapsed() -> int:
        return int((time.perf_counter() - t0) * 1000)

    def emit(event: str, stage: str, progress: float) -> None:
        result.elapsed_ms = elapsed()
        result.stage = stage
        result.progress = progress
        if on_update:
            on_update(event, result)

    def log(msg: str) -> None:
        result.log.append(f"[{elapsed():>6} ms] {msg}")

    # ── 0 · Preflight ────────────────────────────────────────────────────────
    emit("stage", "preflight", 0.02)
    report = run_preflight(ctx, model)
    result.infeasibility = report
    log(f"preflight {report.preflight_ms} ms · "
        f"{len(report.blocking)} bloqueos · {len(report.warnings)} avisos")
    if not report.feasible:
        result.status = "infeasible"
        emit("done", "infeasible", 1.0)
        return result

    # ── 0b · Modo LIBRE: ruta directa, sin perseguir kilómetros ──────────────
    # Aquí el pipeline no aplica. No hay circuito que generar ni distancia que
    # ajustar: hay que encadenar caminos mínimos entre los puntos que el usuario
    # ha marcado, bajo los pesos que dicte el objetivo. Es una petición distinta,
    # no una variante de la misma.
    if model.distance.is_free:
        return _generate_direct(ctx, model, result, emit, log, elapsed, handle)

    # El presupuesto de la fase rápida debe escalar con la distancia: un maratón
    # tiene ~12 anclas y cada tramo es kilométrico. Con 3 s fijos, Mandatory Repair
    # se quedaba sin tiempo y dejaba zonas sin cubrir.
    # Cada zona obligatoria añade trabajo a la fase rápida: hay que insertarla y
    # revalidar. Con cinco zonas y el reparto anterior, a Mandatory Repair le
    # tocaban 680 ms sobre un grafo donde una consulta cuesta 13 ms.
    fast_ms = max(model.fast_budget_ms,
                  int(model.distance.target_m / 1000.0 * 190))
    fast_ms = int(fast_ms * (1.0 + 0.22 * len(model.mandatory_zones)))
    fast_ms = min(fast_ms, int(model.total_budget_ms * 0.70))

    # Anclaje: salida y meta sobre la red REALMENTE utilizable bajo estas
    # restricciones, no sobre el nodo más próximo sin más (ver snap.py).
    anc = anchor(ctx, model)
    start_node, end_node = anc.start, anc.finish
    is_open = model.routetype.shape == RouteShape.OPEN
    w, diag = anc.weights, anc.diag
    log(f"grafo admisible: {diag['edges_allowed']:,}/{diag['edges_total']:,} aristas")
    if anc.start_relocated:
        log(f"salida reubicada {anc.start_moved_m:,.0f} m: el nodo más próximo al "
            f"clic no conecta con red suficiente")

    reach = anc.reach
    log(f"alcanzables por calle: {anc.reach_nodes:,} nodos")
    masks = zone_masks(ctx, model)

    # Anclas obligatorias: una por zona. Entran en la GENERACIÓN, no se parchean
    # después. Y quedan protegidas frente a los operadores de mejora, que si no
    # las mueven fuera de su zona y erosionan la cobertura iteración a iteración.
    must = mandatory_seed_anchors(ctx, masks, reach, start_node) if masks else []
    protected = frozenset(must)
    if masks:
        log(f"anclas obligatorias sembradas: {len(must)}/{len(masks)} zonas")

    # ── 1 · Loop Generator ───────────────────────────────────────────────────
    emit("stage", "generating", 0.10)
    if is_open:
        loops = generate_open_routes(ctx, model, w, start_node, end_node, reach, rng,
                                     k=8, budget_ms=int(fast_ms * 0.40))
    else:
        loops = generate_loops(ctx, model, w, start_node, reach, rng,
                               k=10, budget_ms=int(fast_ms * 0.40), must=must)
    log(f"loop generator: {len(loops)} circuitos crudos")
    if not loops:
        result.status = "infeasible"
        result.infeasibility = report.model_copy(update={
            "blocking": report.blocking + (
                __import__("raceengine_contracts", fromlist=["BlockingConstraint"])
                .BlockingConstraint(
                    id="generation", rule="VR-5",
                    detail=msg("pf_no_loop", model.lang),
                    suggested_relaxation={
                        "geometry.search_radius_max_m":
                            int(model.geometry.search_radius_max_m * 1.6),
                        "distance.tolerance_pct":
                            min(15.0, model.distance.tolerance_pct * 2),
                    },
                ),
            )
        })
        emit("done", "infeasible", 1.0)
        return result

    archive = ParetoArchive(max_overlap=0.70, capacity=24)
    incumbent: Incumbent | None = None
    rings: dict[str, list[int]] = {}
    # Reserva de rutas VÁLIDAS para ofrecer alternativas. El frente de Pareto se
    # colapsa cuando los candidatos se parecen (una ruta domina a las demás en los
    # 5 objetivos), y entonces no queda nada que enseñar. Esto es independiente de
    # la dominancia: guarda todo lo utilizable y al final se eligen las más
    # distintas entre sí.
    usable_pool: list[RouteCandidate] = []
    seen_pool: set[str] = set()

    def remember(cands: list[RouteCandidate]) -> None:
        gate = _distance_gate(model.distance.target_m, model.distance.tolerance_m)
        for c in cands:
            if c.route_id in seen_pool:
                continue
            if c.mandatory_missing or c.hard_violations:
                continue
            if abs(c.metrics.distance_m - model.distance.target_m) > gate:
                continue
            seen_pool.add(c.route_id)
            usable_pool.append(c)

    def consider(cands: list[RouteCandidate], stage: Stage,
                 ring_by_id: dict[str, list[int]]) -> None:
        nonlocal incumbent
        pool = archive.routes + usable_pool + cands
        _score_all(pool, model)
        remember(cands)
        for c in cands:
            archive.offer(c, ring_by_id.get(c.route_id, []))
        for c in archive.routes:
            rings.setdefault(c.route_id, ring_by_id.get(c.route_id, []))
        ranked = sorted(archive.routes, key=lambda c: _feasible_first(c, model))
        if not ranked:
            return
        best = ranked[0]
        if incumbent is None or _feasible_first(best, model) < _feasible_first(incumbent.route, model):
            prev = incumbent.route.global_score if incumbent else None
            incumbent = Incumbent(
                route=best, stage=stage, found_at_ms=elapsed(),
                improvement=None if prev is None else round(best.global_score - prev, 2),
            )
            result.recommended = best

    # Etapa SEED: circuitos crudos.
    seed_cands: list[RouteCandidate] = []
    ring_map: dict[str, list[int]] = {}
    for lp in loops:
        c = _candidate_from_loop(ctx, model, lp, masks, seed, lp.generator)
        ring_map[c.route_id] = lp.anchors
        seed_cands.append(c)
    consider(seed_cands, Stage.SEED, ring_map)

    # ── Puntería del ajustador en modo certificado ───────────────────────────
    # El ajustador de distancia trabaja sobre la longitud del EJE, que es lo que
    # devuelve el ruteo. Pero en modo certificado el objetivo está expresado
    # sobre la ruta más corta posible. Se le da un objetivo desplazado por el
    # ahorro típico de cortar tangentes en ESTE recorrido, medido sobre los
    # candidatos semilla. El ahorro depende del número de curvas, que apenas
    # cambia durante la búsqueda, así que se fija una vez y no oscila.
    aim = model
    if model.distance.measurement == "certified" and seed_cands:
        from raceengine_contracts import DistanceLayer
        offset = float(np.median([c.metrics.spr_saving_m for c in seed_cands]))
        # Se apunta al CENTRO de la banda certificable, que es asimétrica: hacia
        # arriba sobra margen, hacia abajo no hay ninguno. Apuntar al objetivo
        # exacto dejaba la mitad de los resultados unos metros cortos —23 m en la
        # prueba— y un recorrido corto no se homologa por poco que le falte.
        aim = model.model_copy(update={"distance": DistanceLayer(
            target_m=target_aim(model) + offset,
            tolerance_pct=model.distance.tolerance_pct,
            unit=model.distance.unit, measurement="centerline")})
        log(f"medición certificada: objetivo SPR "
            f"{compliant_target(model) / 1000:.3f} km (nunca por debajo) → "
            f"objetivo por el eje {aim.distance.target_m / 1000:.3f} km "
            f"(tangentes ahorran {offset:.0f} m)")

    emit("recommended", "seed", 0.30)
    log(f"seed: mejor score {result.recommended.global_score:.1f}"
        if result.recommended else "seed: sin candidato")

    if handle.cancel.is_set():
        result.status = "cancelled"
        emit("done", "cancelled", 1.0)
        return result

    # ── 2 · Mandatory Repair ─────────────────────────────────────────────────
    repaired: list[RouteCandidate] = []
    ring_map = {}
    if model.mandatory_zones:
        emit("stage", "repairing", 0.38)
        # Menos circuitos, más tiempo cada uno: reparar 4 loops bien es mucho
        # mejor que reparar 6 a medias.
        n_rep = 4
        per = max(500, int(fast_ms * 0.34) // n_rep)
        for lp in loops[:n_rep]:
            fixed, missing = repair_mandatory(ctx, model, w, lp, start_node,
                                              reach, masks, budget_ms=per,
                                              end_node=end_node)
            c = _candidate_from_loop(ctx, model, fixed, masks, seed, fixed.generator)
            ring_map[c.route_id] = fixed.anchors
            repaired.append(c)
        consider(repaired, Stage.REPAIRED, ring_map)
        emit("recommended", "repaired", 0.52)
        cov = len(result.recommended.mandatory_covered) if result.recommended else 0
        log(f"mandatory repair: {cov}/{len(model.mandatory_zones)} zonas cubiertas")

    if handle.cancel.is_set():
        result.status = "cancelled"
        emit("done", "cancelled", 1.0)
        return result

    # ── 3 · Distance Adjuster ────────────────────────────────────────────────
    emit("stage", "adjusting", 0.58)
    base_pool = (repaired or seed_cands)
    base_pool = sorted(base_pool, key=lambda c: _feasible_first(c, model))[:5]
    adjusted: list[RouteCandidate] = []
    ring_map = {}
    for c in base_pool:
        ring = rings.get(c.route_id) or []
        if not ring:
            continue
        lp = RawLoop(nodes=c.node_path, edges=[], anchors=ring,
                     distance_m=c.metrics.distance_m)
        relooped = route_chain(ctx, w, [start_node] + ring + [end_node],
                               max_leg_cost=model.distance.target_m * 0.9)
        if relooped is None:
            continue
        relooped.anchors = ring
        relooped.generator = c.provenance.generator
        fixed, ops = adjust_distance(ctx, aim, w, relooped, start_node, reach,
                                     masks, rng,
                                     budget_ms=max(400, int(fast_ms * 0.26) // 3),
                                     end_node=end_node)
        nc = _candidate_from_loop(ctx, model, fixed, masks, seed, fixed.generator)
        nc.provenance.operators = ops
        ring_map[nc.route_id] = fixed.anchors
        adjusted.append(nc)
    if adjusted:
        consider(adjusted, Stage.ADJUSTED, ring_map)
    emit("recommended", "adjusted", 0.72)
    if result.recommended:
        log(f"distance adjuster: {result.recommended.metrics.distance_m / 1000:.3f} km "
            f"(objetivo {model.distance.target_m / 1000:.3f} km · "
            f"{'en' if result.recommended.in_tolerance else 'FUERA de'} tolerancia)")

    # ── 4 · Anytime Improve ──────────────────────────────────────────────────
    emit("stage", "improving", 0.75)
    credit = OperatorCredit(names=list(OPERATORS))
    deadline = t0 + model.total_budget_ms / 1000.0
    iteration = 0
    accepted = 0
    since_improvement = 0
    # Convergencia: si el vecindario deja de producir mejoras, no tiene sentido
    # quemar el presupuesto entero. El usuario recibe su ruta antes.
    STALL_LIMIT = 600

    # En modo reproducible el criterio de parada es el TRABAJO hecho, no el reloj.
    # El reloj queda como red de seguridad; si salta, el resultado ya no es
    # reproducible y hay que decirlo en vez de callarlo.
    fixed_iters = model.deterministic_iterations
    if fixed_iters is None and model.reproducible:
        fixed_iters = iteration_budget(model, ctx)
        deadline = t0 + (model.total_budget_ms / 1000.0) * REPRO_SAFETY_FACTOR
        log(f"modo reproducible: {fixed_iters} iteraciones · "
            f"{ms_per_iteration(model, ctx):.1f} ms/iteración estimados "
            f"(coste de ruta {ctx.astar_ms:.2f} ms en esta región)")
    truncated = False

    while not handle.cancel.is_set():
        if fixed_iters is not None and iteration >= fixed_iters:
            break
        if time.perf_counter() >= deadline:
            if fixed_iters is not None:
                truncated = True
                log("AVISO: se agotó el tope de seguridad antes de completar las "
                    "iteraciones; este resultado NO es reproducible")
            break
        # La convergencia por estancamiento sí es determinista: depende de la
        # búsqueda, no del reloj. Se aplica también en modo reproducible.
        if since_improvement > STALL_LIMIT:
            log(f"convergencia: {STALL_LIMIT} iteraciones sin mejora")
            break
        iteration += 1
        since_improvement += 1
        if not archive.entries:
            break
        pick = archive.entries[int(rng.integers(0, min(len(archive.entries), 6)))]
        ring = pick.ring or rings.get(pick.route.route_id) or []
        if len(ring) < 3:
            break

        # Cada 25 iteraciones, si al incumbente le faltan zonas obligatorias, se
        # vuelve a invocar Mandatory Repair sobre él. Cubrir un checkpoint es más
        # importante que cualquier mejora de forma.
        if (iteration % 25 == 0 and incumbent
                and incumbent.route.mandatory_missing and model.mandatory_zones):
            lpm = route_chain(ctx, w, [start_node] + ring + [end_node],
                              max_leg_cost=model.distance.target_m * 0.9)
            if lpm is not None and lpm.ok:
                lpm.anchors = ring
                lpm.generator = pick.route.provenance.generator
                fixed_m, still_missing = repair_mandatory(
                    ctx, model, w, lpm, start_node, reach, masks, budget_ms=700,
                    end_node=end_node)
                if len(still_missing) < len(incumbent.route.mandatory_missing):
                    mc = _candidate_from_loop(ctx, model, fixed_m, masks, seed,
                                              fixed_m.generator)
                    mc.provenance.iteration = iteration
                    prev_m = incumbent.route.route_id
                    consider([mc], Stage.IMPROVED, {mc.route_id: fixed_m.anchors})
                    if incumbent.route.route_id != prev_m:
                        accepted += 1
                        since_improvement = 0
                        emit("recommended", "improved", 0.88)
            continue

        # Cada 15 iteraciones, si el incumbente sigue fuera de tolerancia, se
        # vuelve a invocar el Distance Adjuster sobre él. La fase de mejora puede
        # usar los operadores de reparación, no solo los de forma: sin esto, una
        # configuración muy restringida (p. ej. dos zonas obligatorias que fijan
        # la forma) se queda estancada cerca de la banda sin llegar a entrar.
        if iteration % 15 == 0 and incumbent and not incumbent.route.in_tolerance:
            lp0 = route_chain(ctx, w, [start_node] + ring + [end_node],
                              max_leg_cost=model.distance.target_m * 0.9)
            if lp0 is not None and lp0.ok:
                lp0.anchors = ring
                lp0.generator = pick.route.provenance.generator
                tuned, tops = adjust_distance(ctx, aim, w, lp0, start_node, reach,
                                              masks, rng, budget_ms=300, max_iter=6,
                                              end_node=end_node)
                if tops:
                    tc = _candidate_from_loop(ctx, model, tuned, masks, seed,
                                              tuned.generator)
                    tc.provenance.operators = list(pick.route.provenance.operators) + tops
                    tc.provenance.iteration = iteration
                    prev_id = incumbent.route.route_id
                    consider([tc], Stage.IMPROVED, {tc.route_id: tuned.anchors})
                    if incumbent.route.route_id != prev_id:
                        accepted += 1
                        since_improvement = 0
                        emit("recommended", "improved", 0.9)
            continue

        op_name = credit.pick(rng)
        new_ring = OPERATORS[op_name](g, ring, start_node, reach, rng,
                                      protected=protected)
        if not new_ring or len(new_ring) < 3:
            credit.penalize(op_name)
            continue

        lp = route_chain(ctx, w, [start_node] + new_ring + [end_node],
                         max_leg_cost=model.distance.target_m * 0.9)
        if lp is None or not lp.ok:
            credit.penalize(op_name)
            continue
        if abs(lp.distance_m - model.distance.target_m) > model.distance.target_m * 0.40:
            credit.penalize(op_name)
            continue

        lp.anchors = new_ring
        lp.generator = f"{pick.route.provenance.generator}+{op_name}"
        cand = _candidate_from_loop(ctx, model, lp, masks, seed, lp.generator)
        cand.provenance.operators = list(pick.route.provenance.operators) + [op_name]
        cand.provenance.iteration = iteration

        before = incumbent.route.route_id if incumbent else None
        consider([cand], Stage.IMPROVED, {cand.route_id: new_ring})
        if incumbent and incumbent.route.route_id != before:
            accepted += 1
            since_improvement = 0
            credit.reward(op_name)
            emit("recommended", "improved",
                 min(0.98, 0.75 + 0.23 * (time.perf_counter() - t0)
                     / max(model.total_budget_ms / 1000.0, 1e-6)))
        else:
            credit.penalize(op_name)

    if result.recommended and result.recommended.route_id not in seen_pool:
        remember([result.recommended])

    result.status = "cancelled" if handle.cancel.is_set() else "converged"
    result.reproducible = bool(fixed_iters is not None and not truncated
                               and not handle.cancel.is_set())

    # ── Puntuación FINAL: una sola vara para todo lo que se va a enseñar ──────
    # Importa que sea una sola. La normalización es relativa al conjunto (doc 06
    # §4.2), así que puntuar la recomendada contra un conjunto y las alternativas
    # contra otro produce el absurdo de una «alternativa» con más puntos que la
    # recomendación. Aquí se puntúa todo junto, una vez, y de ahí sale todo.
    finalists = list(archive.routes) + [c for c in usable_pool
                                        if c not in archive.routes]
    if result.recommended is not None and result.recommended not in finalists:
        finalists.append(result.recommended)
    if finalists:
        _score_all(finalists, model)
        # Y la recomendación es la mejor de TODO lo encontrado, no sólo del frente
        # de Pareto: el archivo rechaza rutas por parecerse más de un 70% a otra ya
        # guardada, y una de ésas puede ser la mejor ruta de la ejecución.
        champion = min(finalists, key=lambda c: _feasible_first(c, model))
        if (result.recommended is None
                or _feasible_first(champion, model)
                < _feasible_first(result.recommended, model)):
            log(f"promoción: la mejor ruta estaba fuera del frente de Pareto "
                f"(score {champion.global_score:.1f})")
            result.recommended = champion

    # Veredicto honesto: si el mejor resultado incumple gravemente la distancia o
    # deja checkpoints sin cubrir, NO se presenta como recomendación. Se devuelve
    # igualmente para que el usuario vea qué se intentó, pero marcado como
    # infactible y con la explicación del conflicto (FR-3.5).
    rec = result.recommended
    if rec and result.status == "converged":
        ref = compliant_target(model)
        gate = _distance_gate(ref, model.distance.tolerance_m)
        err = abs(compliant_distance(rec.metrics, model) - ref)
        problems: list = []
        if err > gate:
            problems.append(BlockingConstraint(
                id="distance.unsatisfiable", rule="VR-5",
                detail=(
                    msg("verdict_distance", model.lang,
                        d=compliant_distance(rec.metrics, model) / 1000,
                        t=ref / 1000, p=100 * err / ref)
                    + msg("verdict_distance_zones" if model.mandatory_zones
                          else "verdict_distance_plain", model.lang)
                ),
                suggested_relaxation={
                    "distance.target_m": int(round(
                        compliant_distance(rec.metrics, model), -2))},
            ))
        if rec.mandatory_missing:
            problems.append(BlockingConstraint(
                id="zones.uncovered", rule="VR-4",
                detail=msg("verdict_uncovered", model.lang,
                           list=", ".join(rec.mandatory_missing)),
                suggested_relaxation={
                    "buffer_m": 600,
                    "distance.target_m": int(model.distance.target_m * 1.25),
                },
            ))
        if problems:
            result.status = "infeasible"
            base = result.infeasibility
            result.infeasibility = InfeasibilityReport(
                blocking=tuple(problems),
                warnings=(base.warnings if base else ()),
                preflight_ms=(base.preflight_ms if base else 0),
            )
            log(f"veredicto: infactible — {len(problems)} conflicto(s) irresolubles")
    # Alternativas: selección voraz por score entre las rutas utilizables,
    # exigiendo que no se solapen más del 70% con ninguna ya elegida (doc 06 §7.2).
    rec_id = result.recommended.route_id if result.recommended else None
    # Ya están puntuadas arriba, contra el mismo conjunto que la recomendada. NO se
    # vuelven a puntuar aquí: hacerlo cambiaría la vara y reaparecería el absurdo.
    # Solo se ofrecen alternativas EN TOLERANCIA. Una ruta de 19,4 km no es una
    # alternativa para un objetivo de 21,1 km con ±1%, por muy buena que sea en
    # los demás índices: la distancia es una restricción dura, no un objetivo.
    pool_alt = [c for c in finalists if not c.mandatory_missing and not c.hard_violations]
    cands_alt = sorted((r for r in pool_alt
                        if r.route_id != rec_id and r.in_tolerance),
                       key=lambda c: -c.global_score)
    dropped = sum(1 for r in pool_alt
                  if r.route_id != rec_id and not r.in_tolerance)
    chosen: list[RouteCandidate] = []
    ref_sets = [set(result.recommended.node_path)] if result.recommended else []
    for c in cands_alt:
        s = set(c.node_path)
        if all(len(s & o) / max(min(len(s), len(o)), 1) <= 0.70 for o in ref_sets):
            chosen.append(c)
            ref_sets.append(s)
        if len(chosen) >= 4:
            break
    result.alternatives = chosen
    log(f"alternativas: {len(chosen)} diversas · {len(cands_alt)} en tolerancia · "
        f"{dropped} descartadas por distancia")
    log(f"anytime: {iteration} iteraciones · {accepted} mejoras aceptadas · "
        f"archivo {len(archive)} rutas")
    log(f"conjunto de candidatos: {candidate_set_hash(archive.routes)}")
    emit("done", result.status, 1.0)
    handle.result = result
    return result
