"""Cálculo del catálogo de métricas crudas — doc 06 §3.

Dos correcciones respecto al MVP que conviene tener presentes:

  · `self_crossings` (el `at_grade` del MVP) YA NO DESCARTA la ruta. Es un coste y
    una bandera operativa. Un maratón urbano real se cruza a sí mismo; el filtro
    duro del MVP vaciaba el espacio de soluciones antes de evaluarlo (doc 10 §3.5).

  · `enclosed_intersections` se conserva porque la intuición es buena, pero
    reimplementada: el MVP hacía point-in-polygon en Python puro sobre todos los
    nodos del grafo contra un polígono de miles de vértices (10⁸–10⁹ operaciones,
    doc 10 §2 B6). Aquí está vectorizado con numpy y prefiltrado por bbox.
"""
from __future__ import annotations

import math

import numpy as np

from raceengine_contracts import (
    CYCLING, OFFROAD_FOOT, ConstraintModel, Confidence, RawMetrics, angle_delta,
    bearing_deg,
)
from raceengine_geodata import RaceGraph

from ..cycling import (
    climb_summary, climbs, foot_time_min, ride_time_min, unpaved_share_pct,
)
from ..measure import (
    SHORT_COURSE_PREVENTION_FACTOR, capacity, route_measurement,
)

TURN_THRESHOLD_DEG = 35.0
SHARP_TURN_DEG = 105.0
UTURN_DEG = 150.0
STRAIGHT_THRESHOLD_DEG = 22.0


def _points_in_polygon(px: np.ndarray, py: np.ndarray,
                       vx: np.ndarray, vy: np.ndarray) -> np.ndarray:
    """Ray casting vectorizado: todos los puntos contra un polígono a la vez."""
    inside = np.zeros(len(px), dtype=bool)
    n = len(vx)
    j = n - 1
    for i in range(n):
        yi, yj = vy[i], vy[j]
        cond = (yi > py) != (yj > py)
        if cond.any():
            denom = (yj - yi) or 1e-12
            xint = (vx[j] - vx[i]) * (py - yi) / denom + vx[i]
            inside ^= cond & (px < xint)
        j = i
    return inside


def _enclosed_intersections(g: RaceGraph, nodes: list[int]) -> int:
    if len(nodes) < 8:
        return 0
    vx = g.x[nodes]
    vy = g.y[nodes]
    minx, maxx = float(vx.min()), float(vx.max())
    miny, maxy = float(vy.min()), float(vy.max())

    cand = (
        (g.x >= minx) & (g.x <= maxx) & (g.y >= miny) & (g.y <= maxy)
        & g.is_intersection
    )
    on_route = np.zeros(g.n_nodes, dtype=bool)
    on_route[np.asarray(nodes, dtype=np.int64)] = True
    cand &= ~on_route
    idx = np.nonzero(cand)[0]
    if len(idx) == 0:
        return 0

    # Submuestreo del polígono: la precisión no mejora con 5.000 vértices.
    step = max(1, len(vx) // 900)
    return int(_points_in_polygon(g.x[idx], g.y[idx], vx[::step], vy[::step]).sum())


def _hospital_proximity(g: RaceGraph, nodes: list[int]) -> float:
    hosp = np.nonzero(g.is_hospital)[0]
    if len(hosp) == 0 or not nodes:
        return 0.0
    n = np.asarray(nodes, dtype=np.int64)
    step = max(1, len(n) // 250)
    n = n[::step]
    dx = g.x[n][:, None] - g.x[hosp][None, :]
    dy = g.y[n][:, None] - g.y[hosp][None, :]
    d = np.sqrt(dx * dx + dy * dy).min(axis=1)
    return float(d.mean())


def compute_metrics(g: RaceGraph, nodes: list[int], edges: list[int],
                    model: ConstraintModel, coverage: dict) -> RawMetrics:
    m = RawMetrics()
    if not edges:
        return m

    e = np.asarray(edges, dtype=np.int64)
    n = np.asarray(nodes, dtype=np.int64)
    lengths = g.elen[e]
    total = float(lengths.sum())

    # ── 3.1 Distancia y forma ────────────────────────────────────────────────
    m.distance_m = total
    m.distance_error_pct = 100.0 * (total - model.distance.target_m) / max(model.distance.target_m, 1.0)

    bearings = np.array([
        bearing_deg(float(g.lat[a]), float(g.lon[a]), float(g.lat[b]), float(g.lon[b]))
        for a, b in zip(nodes, nodes[1:])
    ]) if len(nodes) > 1 else np.array([])

    deltas = np.array([angle_delta(a, b) for a, b in zip(bearings, bearings[1:])]) \
        if len(bearings) > 1 else np.array([])

    m.turns = int((deltas > TURN_THRESHOLD_DEG).sum())
    m.sharp_turns = int((deltas > SHARP_TURN_DEG).sum())
    m.uturns = int((deltas > UTURN_DEG).sum())

    # Tramos rectos: acumulación entre cambios de dirección significativos.
    runs: list[float] = []
    cur = float(lengths[0]) if len(lengths) else 0.0
    for i, d in enumerate(deltas):
        nxt = float(lengths[i + 1]) if i + 1 < len(lengths) else 0.0
        if d <= STRAIGHT_THRESHOLD_DEG:
            cur += nxt
        else:
            runs.append(cur)
            cur = nxt
    runs.append(cur)
    m.avg_straight_m = float(np.mean(runs)) if runs else total
    m.longest_straight_m = float(max(runs)) if runs else total

    km = max(total / 1000.0, 0.001)
    avg_st = min(1.0, m.avg_straight_m / 300.0)
    longest = min(1.0, m.longest_straight_m / 1000.0)
    turn_rate = max(0.0, 1.0 - (m.turns / km) / 8.0)
    sharp_rate = max(0.0, 1.0 - (m.sharp_turns / km) / 2.0)
    zigzag = max(0.0, 1.0 - (float(deltas.sum()) / km) / 650.0) if len(deltas) else 1.0
    # Formaliza el `elegance` del MVP como una métrica declarada (doc 10 §3.6).
    m.smoothness = float(
        0.28 * avg_st + 0.26 * longest + 0.22 * turn_rate + 0.14 * sharp_rate + 0.10 * zigzag
    )

    # ── 3.2 Elevación ────────────────────────────────────────────────────────
    has_elev = bool(np.ptp(g.elev) > 1.0)
    if has_elev:
        elev_path = g.elev[n]
        d_elev = np.diff(elev_path)
        m.gain_m = float(d_elev[d_elev > 0].sum())
        m.loss_m = float(-d_elev[d_elev < 0].sum())
        grads = np.abs(g.egrad[e])
        m.grad_max_pct = float(grads.max()) if len(grads) else 0.0
        m.grad_avg_pct = float((m.gain_m + m.loss_m) / total * 100.0) if total else 0.0

        # Subida sostenida más larga.
        longest_climb = climb = 0.0
        for i, de in enumerate(d_elev):
            if de > 0:
                climb += float(lengths[i]) if i < len(lengths) else 0.0
                longest_climb = max(longest_climb, climb)
            else:
                climb = 0.0
        m.longest_climb_m = longest_climb
        lvl = coverage.get("elevation", {}).get("level", "partial")
        m.conf_elevation = Confidence(lvl) if lvl in {c.value for c in Confidence} else Confidence.PARTIAL
    else:
        m.conf_elevation = Confidence.DEFERRED

    # ── 3.3 Intersecciones ───────────────────────────────────────────────────
    m.crossings = int(g.is_intersection[n].sum())
    m.lights = int((g.is_intersection[n] & g.is_signal[n]).sum())

    # ── 3.4 Tiempo ───────────────────────────────────────────────────────────
    disciplina = getattr(model.discipline, "value", model.discipline)
    if disciplina in CYCLING:
        # Un ritmo fijo por kilómetro es una mentira sobre una bicicleta: se pasa
        # de 40 km/h bajando a 8 subiendo. El tiempo se integra arista a arista
        # con la velocidad que corresponde a CADA pendiente; dos rutas de 100 km
        # con el mismo desnivel pueden diferir una hora según cómo esté repartido.
        m.est_time_min = ride_time_min(g, e, disciplina)
        # Un semáforo cuesta lo mismo, pero girar en bici cuesta más: hay que
        # frenar y recuperar la velocidad.
        m.est_time_min += m.lights * 0.25 + m.turns * 0.035
        m.assumed_pace_min_km = (m.est_time_min / max(total / 1000.0, 1e-6))
        m.unpaved_share_pct = unpaved_share_pct(g, e)
        for k, v in climb_summary(climbs(g, e)).items():
            setattr(m, k, v)
    elif disciplina in OFFROAD_FOOT:
        # Ni el ritmo fijo del asfalto ni la potencia de la bici: la función de
        # Tobler, cuyo óptimo está bajando un 5 % y no en llano. Un modelo
        # simétrico se equivoca justo donde más se camina.
        m.est_time_min = foot_time_min(g, e, disciplina)
        m.assumed_pace_min_km = m.est_time_min / max(total / 1000.0, 1e-6)
        m.unpaved_share_pct = unpaved_share_pct(g, e)
        for k, v in climb_summary(climbs(g, e)).items():
            setattr(m, k, v)
    else:
        pace = m.assumed_pace_min_km
        base = total / 1000.0 * pace
        m.est_time_min = base + m.lights * 0.25 + m.turns * 0.02

    # ── 3.5 Logística ────────────────────────────────────────────────────────
    # Los cierres se cuentan sobre aristas ÚNICAS (sin dirección): una calle por
    # la que se pasa tres veces se cierra una vez. Importa para los circuitos de
    # vueltas, que es justamente su ventaja operativa.
    und_all = np.minimum(e, _reverse_index(g, e))
    uniq = np.unique(und_all)
    m.closure_len_m = float(g.elen[uniq].sum())
    m.closure_impact = float((g.elen[uniq] * g.eclosure[uniq]).sum())

    counts = np.bincount(n, minlength=g.n_nodes)
    interior = counts.copy()
    if len(nodes) > 1 and nodes[0] == nodes[-1]:
        interior[nodes[0]] -= 1
    m.self_crossings = int(np.maximum(interior - 1, 0).sum())

    und = und_all
    _, cnt = np.unique(und, return_counts=True)
    reused = int(np.maximum(cnt - 1, 0).sum())
    m.segment_reuse_pct = 100.0 * reused / max(len(e), 1)

    m.enclosed_intersections = _enclosed_intersections(g, nodes)

    # ── 3.7 Medición certificable y capacidad ────────────────────────────────
    # La longitud que cuenta para certificar no es la del eje: es la de la ruta
    # más corta posible, y el medidor oficial busca esa longitud multiplicada por
    # el factor de prevención de recorrido corto.
    med = route_measurement(g, nodes, edges)
    m.spr_m = med["spr_m"]
    m.spr_saving_m = med["saving_m"]
    m.certified_m = model.distance.target_m * SHORT_COURSE_PREVENTION_FACTOR

    cap = capacity(g, nodes, edges, model.logistics.field_size)
    m.min_width_m = cap["min_width_m"]
    m.bottleneck_m = cap["bottleneck_m"]
    m.flow_per_min = cap["flow_per_min"]
    m.field_clear_min = cap["field_clear_min"]
    m.width_needed_m = cap["width_needed_m"]

    # ── 3.6 Experiencia y seguridad ──────────────────────────────────────────
    m.green_share_pct = float(100.0 * lengths[g.epark[e]].sum() / total) if total else 0.0
    m.lit_share_pct = float(100.0 * lengths[g.elit[e]].sum() / total) if total else 0.0
    m.hosp_prox_m = _hospital_proximity(g, nodes)

    def _conf(attr: str, default: Confidence) -> Confidence:
        lvl = coverage.get(attr, {}).get("level")
        return Confidence(lvl) if lvl in {c.value for c in Confidence} else default

    m.conf_surface = _conf("surface", Confidence.PARTIAL)
    m.conf_lit = _conf("lit", Confidence.PARTIAL)
    m.conf_logistics = Confidence.PARTIAL      # proxies por clase de vía (doc 04 L7)
    m.conf_scenery = Confidence.DEFERRED       # proxy de parques (doc 04 L8, BR-C)
    return m


def _reverse_index(g: RaceGraph, e: np.ndarray) -> np.ndarray:
    """Índice de la arista inversa. La tabla vive en el propio grafo."""
    return g.reverse[e]


def compliant_distance(m: RawMetrics, model: ConstraintModel) -> float:
    """La longitud que se compara contra el objetivo.

    En modo certificado es la SPR, porque es la que medirá el juez. En modo eje
    es la longitud del grafo. Toda la cadena —tolerancia, ajuste, veredicto— usa
    esta función y no `distance_m` a secas, para que no haya dos verdades.
    """
    if model.distance.measurement == "certified" and m.spr_m > 0:
        return m.spr_m
    return m.distance_m


def compliant_target(model: ConstraintModel) -> float:
    """El objetivo contra el que se compara.

    Lleva el 1,001 en modo certificado, y en modo HORQUILLA es el centro del
    rango: cuando el criterio es el desnivel, la distancia no tiene un valor
    exacto que perseguir, tiene una banda dentro de la que da igual caer.
    """
    if model.distance.measurement == "certified":
        return model.distance.target_m * SHORT_COURSE_PREVENTION_FACTOR
    return model.distance.center_m


def in_tolerance(m: RawMetrics, model: ConstraintModel) -> bool:
    """¿Cae la distancia dentro de lo aceptable?

    En modo certificable la banda es ASIMÉTRICA, y esto no es un capricho: la
    certificación no es simétrica. Un recorrido que mide de más sigue siendo
    válido —los corredores hacen unos metros extra y nadie protesta—, pero uno
    que mide de menos no es homologable, ni por un metro. Aplicar aquí un ±2 %
    dejaba pasar por «en tolerancia» un maratón 800 m corto, y el panel acababa
    diciendo «✓ en tolerancia» junto a «✗ no certificaría», que es la clase de
    contradicción que hace que nadie se fíe de ninguno de los dos números.
    """
    tol = model.distance.tolerance_m
    t = compliant_target(model)
    d = compliant_distance(m, model)
    if model.distance.measurement == "certified":
        return t <= d <= (t + 2.0 * tol)
    return (t - tol) <= d <= (t + tol)


def target_aim(model: ConstraintModel) -> float:
    """El punto de la banda al que conviene apuntar.

    En modo eje, el centro. En modo certificable, el centro de la banda
    asimétrica: apuntar al borde inferior sería garantizar que la mitad de los
    resultados se queden cortos, que es exactamente lo que no puede pasar.
    """
    t = compliant_target(model)
    if model.distance.measurement == "certified":
        return t + model.distance.tolerance_m
    return t


def hard_violations(m: RawMetrics, model: ConstraintModel, has_elev: bool) -> list[str]:
    """Restricciones DURAS evaluadas sobre la ruta terminada."""
    out: list[str] = []
    el = model.elevation
    if el.enabled and has_elev:
        if el.max_gain_m is not None and m.gain_m > el.max_gain_m:
            out.append(
                f"Desnivel positivo {m.gain_m:,.0f} m supera el máximo de {el.max_gain_m:,.0f} m"
            )
        if el.max_loss_m is not None and m.loss_m > el.max_loss_m:
            out.append(
                f"Desnivel negativo {m.loss_m:,.0f} m supera el máximo de {el.max_loss_m:,.0f} m"
            )
        if el.max_gradient_pct is not None and m.grad_max_pct > el.max_gradient_pct + 0.01:
            out.append(
                f"Pendiente máxima {m.grad_max_pct:.1f}% supera el límite de {el.max_gradient_pct:.1f}%"
            )
    it = model.intersections
    if it.max_crossings is not None and m.crossings > it.max_crossings:
        out.append(f"{m.crossings} cruces superan el máximo de {it.max_crossings}")
    if it.max_traffic_lights is not None and m.lights > it.max_traffic_lights:
        out.append(f"{m.lights} semáforos superan el máximo de {it.max_traffic_lights}")
    ge = model.geometry
    if ge.max_turns is not None and m.turns > ge.max_turns:
        out.append(f"{m.turns} giros superan el máximo de {ge.max_turns}")
    if m.uturns > ge.max_uturns:
        out.append(f"{m.uturns} giros en U superan el máximo de {ge.max_uturns}")
    if not model.routetype.allow_segment_reuse and m.segment_reuse_pct > 12.0:
        out.append(f"Reutilización de tramos {m.segment_reuse_pct:.0f}% (no permitida)")
    return out


def ops_flags_for(m: RawMetrics, model: ConstraintModel) -> list:
    """Hechos operativos derivados (FR-8.1). Antes eran motivos de descarte."""
    from raceengine_contracts import OpsFlag

    from ..messages import msg

    LANGS = ("es", "en")
    L = model.lang
    flags: list[OpsFlag] = []

    def add(code: str, key: str, severity: str, **kw) -> None:
        flags.append(OpsFlag(
            code=code, severity=severity,
            message=msg(key, L, **kw),
            message_i18n={lg: msg(key, lg, **kw) for lg in LANGS},
        ))

    if m.self_crossings > 0:
        add("at_grade_crossing", "ops_at_grade",
            "warning" if m.self_crossings > 2 else "info", n=m.self_crossings)
    if m.lights > 0:
        add("signalised_intersections", "ops_signals", "info", n=m.lights)
    if m.hosp_prox_m > 3000:
        add("hospital_distance", "ops_hospital", "warning", d=m.hosp_prox_m / 1000)
    if m.grad_max_pct > 8:
        add("steep_segment", "ops_steep", "info", p=m.grad_max_pct)
    return flags
