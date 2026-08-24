"""Puntuar varios recorridos en una sola escala.

Por qué existe este módulo
──────────────────────────
La norma de doc 06 §4.2 es que la normalización es RELATIVA al conjunto de
candidatos. Eso obliga a una regla de producto que es fácil de romper sin darse
cuenta: **todo lo que se enseña junto se puntúa junto, una sola vez, al final.**

Cuando llegó la importación esa regla se rompió. La ruta generada venía puntuada
contra sus rivales del generador; la importada se puntuaba aparte, contra el par
de referencias sintéticas del trazado manual. Eran dos escalas distintas
presentadas como si fueran una, y comparar «61 contra 74» no significaba nada.

Aquí se rehace la puntuación de un conjunto cualquiera de recorridos —generados,
importados, dibujados a mano— en una única normalización. Con un solo recorrido
se cae a las referencias sintéticas, porque una cota relativa con n = 1 colapsa
(min = max) y daría un 100 sin significado; en ese caso el resultado se marca
como escala absoluta para que la interfaz pueda decirlo.
"""
from __future__ import annotations

import uuid

from raceengine_contracts import ConstraintModel, RouteCandidate

from .context import GraphContext
from .determinism import ENGINE_VERSION
from .metrics import compute_metrics, hard_violations, in_tolerance, ops_flags_for
from .repair import evaluate_all, zone_masks
from .scoring import compute_indices, explain_all, global_score, normalize_set


def _coords(g, nodes: list[int]) -> list[tuple[float, float, float]]:
    return [(round(float(g.lon[n]), 6), round(float(g.lat[n]), 6),
             round(float(g.elev[n]), 1)) for n in nodes]


def score_set(ctx: GraphContext, model: ConstraintModel,
              paths: list[dict]) -> list[RouteCandidate]:
    """Puntúa `paths` = [{route_id, nodes, generator}] en una escala común.

    Devuelve candidatos en el mismo orden. Un camino ilegible se omite.
    """
    from .draw import _reference_pair
    from .engine import edges_for_path

    g = ctx.graph
    masks = zone_masks(ctx, model)

    prepared: list[tuple[dict, list[int], list[int]]] = []
    for p in paths:
        nodes = [int(n) for n in p.get("nodes", [])]
        if len(nodes) < 2:
            continue
        edges = edges_for_path(g, nodes)
        if not edges:
            continue
        prepared.append((p, nodes, edges))
    if not prepared:
        return []

    mets = [compute_metrics(g, n, e, model, g.coverage) for _, n, e in prepared]

    # Con un solo recorrido no hay conjunto contra el que normalizar: se usan las
    # referencias sintéticas y la escala pasa a ser absoluta (ver docstring).
    solo = len(mets) == 1
    pool = mets + (_reference_pair(model.distance.target_m) if solo else [])
    norms = normalize_set(pool, model)

    out: list[RouteCandidate] = []
    for i, (p, nodes, edges) in enumerate(prepared):
        m = mets[i]
        rc = RouteCandidate(
            route_id=str(p.get("route_id") or uuid.uuid4().hex[:12]),
            coordinates=_coords(g, nodes),
            node_path=list(nodes),
            metrics=m,
        )
        rc.in_tolerance = in_tolerance(m, model)
        rc.hard_violations = hard_violations(m, model, ctx.elevation_available())
        rc.ops_flags = ops_flags_for(m, model)
        touches = evaluate_all(g, nodes, edges, model.mandatory_zones, masks,
                               model.touch_policy, model.lang)
        rc.mandatory_covered = [t.name for t in touches if t.touched]
        rc.mandatory_missing = [t.name for t in touches if not t.touched]

        rc.normalized = norms[i]
        rc.indices = compute_indices(m, norms[i])
        rc.breakdown = global_score(norms[i], rc.indices, model)
        penalty = 12.0 * len(rc.hard_violations) + 8.0 * len(rc.mandatory_missing)
        rc.global_score = max(0.0, rc.breakdown.global_score - penalty)
        rc.breakdown.global_score = rc.global_score
        rc.breakdown.explanation_i18n = explain_all(m, rc.indices, rc.breakdown, model)
        rc.breakdown.explanation = list(
            rc.breakdown.explanation_i18n.get(model.lang, []))
        rc.provenance.generator = str(p.get("generator") or "compare")
        rc.provenance.graph_version = ctx.version
        rc.provenance.engine_version = ENGINE_VERSION
        out.append(rc)
    return out
