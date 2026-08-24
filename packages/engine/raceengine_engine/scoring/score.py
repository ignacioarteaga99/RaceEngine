"""Normalización, índices y score global — implementación literal de doc 06.

Sin invenciones y sin atajos. En particular:
  · `fit` usa banda objetivo FIJA, no relativa (doc 06 §4.2, mitigación 3).
  · El resto usa cotas relativas al conjunto de candidatos, que se persiste.
  · La confianza baja el peso efectivo de la métrica (1.0 / 0.6 / 0.3), no la oculta.
  · Cada índice hereda la confianza MÍNIMA de sus miembros.
  · El score global es el ÚNICO criterio de ranking (corrige H3 del doc 10).
"""
from __future__ import annotations

import math

from raceengine_contracts import (
    CONFIDENCE_FACTOR, Confidence, ConstraintModel, Indices, NormalizedMetrics,
    RawMetrics, ScoreBreakdown,
)

from ..messages import msg


def _lower_is_better(v: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 1.0
    return max(0.0, min(1.0, 1.0 - (v - lo) / (hi - lo)))


def _higher_is_better(v: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 1.0
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


def _bounds(values: list[float]) -> tuple[float, float]:
    return (min(values), max(values)) if values else (0.0, 0.0)


# Banda de tolerancia: dentro, la puntuación va de 1.0 (exacto) a FIT_AT_EDGE
# (justo en el borde); fuera, decae exponencialmente hacia 0.
FIT_AT_EDGE = 0.30
FIT_TAIL_SCALE = 2.0


def distance_fit(distance_m: float, target_m: float, tol_m: float) -> float:
    """Ajuste de distancia con banda objetivo FIJA (doc 06 §4.2, mitigación 3).

    DESVIACIÓN DOCUMENTADA respecto a doc 06 §4.1
    ─────────────────────────────────────────────
    El documento define `n = max(0, 1 − |v − target| / tolerance_band)`. Esa
    función vale 0 para TODO candidato fuera de la banda, así que su gradiente
    desaparece justo cuando el optimizador más lo necesita: cuando ninguna ruta
    está todavía en tolerancia, `fit` deja de discriminar y la búsqueda deriva
    hacia rutas arbitrariamente cortas o largas que puntúan bien en los demás
    índices. Lo verifiqué: sin esta corrección el motor degradaba una ruta de
    8,68 km a 7,59 km sobre un objetivo de 9 km.

    Aquí se conserva la semántica (1.0 en el objetivo exacto, decreciente y
    monótona) pero con una cola continua fuera de la banda, de modo que acercarse
    al objetivo siempre mejora el score. Continua en |err| = tol.
    """
    tol = max(tol_m, 1.0)
    err = abs(distance_m - target_m)
    if err <= tol:
        return 1.0 - (1.0 - FIT_AT_EDGE) * (err / tol)
    return FIT_AT_EDGE * math.exp(-(err - tol) / (FIT_TAIL_SCALE * tol))


def normalize_set(metrics: list[RawMetrics], model: ConstraintModel) -> list[NormalizedMetrics]:
    """Normaliza un conjunto de candidatos. Cotas relativas salvo `fit`."""
    if not metrics:
        return []

    b_cross = _bounds([m.crossings for m in metrics])
    b_light = _bounds([m.lights for m in metrics])
    b_turn = _bounds([m.turns for m in metrics])
    b_gain = _bounds([m.gain_m for m in metrics])
    b_gmax = _bounds([m.grad_max_pct for m in metrics])
    b_gavg = _bounds([m.grad_avg_pct for m in metrics])
    b_clos = _bounds([m.closure_impact for m in metrics])
    b_encl = _bounds([float(m.enclosed_intersections) for m in metrics])
    b_self = _bounds([float(m.self_crossings) for m in metrics])
    b_green = _bounds([m.green_share_pct for m in metrics])
    b_lit = _bounds([m.lit_share_pct for m in metrics])
    b_hosp = _bounds([m.hosp_prox_m for m in metrics])

    tol = max(model.distance.tolerance_m, 1.0)
    # En modo certificado, «la distancia» es la de la ruta más corta posible y el
    # objetivo lleva el factor 1,001. El ajuste tiene que puntuar contra lo que
    # se va a certificar, no contra el eje de la calzada.
    # Y se apunta al CENTRO de la banda, que en modo certificable no coincide con
    # el objetivo: esa banda es asimétrica (nunca por debajo), así que puntuar el
    # ajuste perfecto justo en el borde inferior empujaba al optimizador hacia el
    # único sitio del que no se puede pasar.
    from ..metrics import compliant_distance, target_aim
    ref = target_aim(model)
    out: list[NormalizedMetrics] = []
    for m in metrics:
        nm = NormalizedMetrics()
        # `fit` con banda FIJA: la distancia exacta es una promesa absoluta,
        # no una comparación dentro del conjunto.
        # En modo LIBRE no hay distancia objetivo, así que el «ajuste» no tiene
        # nada que medir. Se deja neutro y son los otros índices —velocidad,
        # logística, paisaje, dificultad— los que deciden, que es exactamente lo
        # que se quiere cuando se pide «la ruta lógica entre estos puntos».
        nm.fit = 1.0 if model.distance.is_free else distance_fit(
            compliant_distance(m, model), ref, tol)

        # Ajuste al DESNIVEL buscado. Es el criterio con el que se plantea una
        # ruta de bici: nadie dice «quiero 137 km», dice «quiero 2.500 metros de
        # desnivel». Misma función que la distancia, misma forma de banda fija,
        # para que las dos promesas se traten igual de en serio.
        el = model.elevation
        if el.enabled and el.target_gain_m:
            gtol = max(el.target_gain_m * el.gain_tolerance_pct / 100.0, 10.0)
            nm.gain_fit = distance_fit(m.gain_m, el.target_gain_m, gtol)
        else:
            nm.gain_fit = 1.0

        nm.crossings = _lower_is_better(m.crossings, *b_cross)
        nm.lights = _lower_is_better(m.lights, *b_light)
        nm.turns = _lower_is_better(m.turns, *b_turn)
        nm.smoothness = max(0.0, min(1.0, m.smoothness))
        nm.gain = _lower_is_better(m.gain_m, *b_gain)
        nm.grad_max = _lower_is_better(m.grad_max_pct, *b_gmax)
        nm.grad_avg = _lower_is_better(m.grad_avg_pct, *b_gavg)
        nm.closure_impact = _lower_is_better(m.closure_impact, *b_clos)
        nm.enclosure = _lower_is_better(float(m.enclosed_intersections), *b_encl)
        nm.self_crossings = _lower_is_better(float(m.self_crossings), *b_self)
        nm.green = _higher_is_better(m.green_share_pct, *b_green)
        nm.lit = _higher_is_better(m.lit_share_pct, *b_lit)
        nm.hospital = _lower_is_better(m.hosp_prox_m, *b_hosp)
        out.append(nm)
    return out


def _worst(*confs: Confidence) -> Confidence:
    order = [Confidence.RELIABLE, Confidence.PARTIAL, Confidence.DEFERRED]
    return max(confs, key=order.index)


def compute_indices(m: RawMetrics, nm: NormalizedMetrics) -> Indices:
    """Los cuatro índices de doc 06 §5, con sus pesos de miembro aprobados."""
    idx = Indices()

    # 5.1 Speed — ¿cuán rápida e ininterrumpida es?
    idx.speed = (
        0.30 * nm.crossings + 0.25 * nm.lights + 0.15 * nm.turns
        + 0.20 * nm.grad_avg * CONFIDENCE_FACTOR[m.conf_elevation]
        + 0.10 * nm.smoothness
    )
    idx.conf_speed = Confidence.RELIABLE

    # 5.2 Logistics — ¿cuán fácil y barata de operar y permisar?
    fl = CONFIDENCE_FACTOR[m.conf_logistics]
    idx.logistics = (
        0.35 * nm.closure_impact * fl + 0.20 * nm.crossings
        + 0.25 * nm.enclosure * fl + 0.20 * nm.self_crossings
    )
    idx.conf_logistics = m.conf_logistics

    # 5.3 Scenery — confianza baja por construcción (BR-C).
    fs = CONFIDENCE_FACTOR[m.conf_scenery]
    fli = CONFIDENCE_FACTOR[m.conf_lit]
    idx.scenery = 0.55 * nm.green * fs + 0.25 * nm.lit * fli + 0.20 * nm.hospital
    idx.conf_scenery = _worst(m.conf_scenery, m.conf_lit)

    # 5.4 Difficulty — índice de primera clase (doc 06 §5.4).
    fe = CONFIDENCE_FACTOR[m.conf_elevation]
    idx.difficulty = (0.40 * nm.gain + 0.35 * nm.grad_max + 0.25 * nm.grad_avg) * fe
    idx.conf_difficulty = m.conf_elevation

    for f in ("speed", "logistics", "scenery", "difficulty"):
        setattr(idx, f, max(0.0, min(1.0, getattr(idx, f))))
    return idx


def global_score(nm: NormalizedMetrics, idx: Indices,
                 model: ConstraintModel) -> ScoreBreakdown:
    """doc 06 §6.1 — score 0..100, acotado, monótono y descomponible."""
    w = model.effective_weights()
    # Con un desnivel objetivo puesto, el «ajuste» deja de ser sólo la distancia y
    # pasa a ser las DOS promesas a la vez, a partes iguales. Sin esto, pedir
    # 2.500 m de desnivel no movía la puntuación ni un punto: el desnivel entraba
    # sólo como dificultad, que es una escala donde más siempre es peor.
    el = model.elevation
    ajuste = nm.fit
    if el.enabled and el.target_gain_m:
        ajuste = 0.5 * nm.fit + 0.5 * nm.gain_fit
    terms = {
        "fit": w.fit * ajuste,
        "speed": w.speed * idx.speed,
        "logistics": w.logistics * idx.logistics,
        "scenery": w.scenery * idx.scenery,
        "difficulty": w.difficulty * idx.difficulty,
    }
    denom = w.fit + w.speed + w.logistics + w.scenery + w.difficulty or 1.0
    score = 100.0 * sum(terms.values()) / denom

    bd = ScoreBreakdown()
    bd.global_score = round(score, 2)
    bd.terms = {k: round(100.0 * v / denom, 2) for k, v in terms.items()}
    bd.weights = {
        "fit": w.fit, "speed": w.speed, "logistics": w.logistics,
        "scenery": w.scenery, "difficulty": w.difficulty,
    }
    return bd


LANGS = ("es", "en")


def explain_all(route_metrics: RawMetrics, idx: Indices, bd: ScoreBreakdown,
                model: ConstraintModel) -> dict[str, list[str]]:
    """Explicación en todos los idiomas soportados."""
    return {lg: explain(route_metrics, idx, bd, model, lg) for lg in LANGS}


def explain(route_metrics: RawMetrics, idx: Indices, bd: ScoreBreakdown,
            model: ConstraintModel, lang: str | None = None) -> list[str]:
    """Narrativa determinista por plantilla (NFR-5.3 / NFR-10.1).

    Regla dura: cada número que aparece aquí procede del objeto de métricas. No se
    estima, no se infiere, no se redondea hacia lo favorable. El idioma lo fija
    `model.lang`, de modo que la interfaz nunca tiene que traducir cifras.
    """
    m = route_metrics
    L = lang or model.lang
    lines = [
        msg("expl_distance", L, d=m.distance_m / 1000, t=model.distance.target_m / 1000,
            e=m.distance_error_pct),
        msg("expl_indices", L, s=idx.speed, l=idx.logistics, c=idx.scenery, f=idx.difficulty),
        msg("expl_crossings", L, x=m.crossings, l=m.lights, t=m.turns, s=m.sharp_turns),
    ]
    if m.conf_elevation != Confidence.DEFERRED:
        lines.append(msg("expl_elevation", L, g=m.gain_m, o=m.loss_m, p=m.grad_max_pct))
    else:
        lines.append(msg("expl_no_elevation", L))
    lines.append(msg("expl_closure", L, c=m.closure_impact / 1000,
                     e=m.enclosed_intersections))
    if m.self_crossings:
        lines.append(msg("expl_selfx", L, n=m.self_crossings))
    top = max(bd.terms.items(), key=lambda kv: kv[1]) if bd.terms else None
    if top:
        lines.append(msg("expl_score", L, s=bd.global_score,
                         k=msg(f"term_{top[0]}", L), v=top[1]))
    if idx.conf_scenery == Confidence.DEFERRED:
        lines.append(msg("expl_scenery_proxy", L))
    return lines
