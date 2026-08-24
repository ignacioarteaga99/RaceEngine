"""Política de paso por zonas obligatorias.

Portado del módulo mejor diseñado del MVP (doc 10 §5), adaptado a los contratos
nuevos. La idea clave se conserva: una zona obligatoria es un CHECKPOINT que se
toca, no un imán de scoring. Las visitas se cuentan como componentes CONTIGUAS —
pasar por 40 nodos dentro del radio en un solo paso es una visita, no cuarenta.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from raceengine_contracts import CustomZone, MandatoryTouchPolicy
from raceengine_geodata import RaceGraph


@dataclass
class ZoneTouch:
    name: str
    visits: int
    dwell_m: float
    touched: bool
    violations: list[str]


def _inside_mask_for_path(zone_mask: np.ndarray, nodes: list[int]) -> list[bool]:
    return [bool(zone_mask[n]) for n in nodes]


def analyze_touch(g: RaceGraph, nodes: list[int], edges: list[int],
                  zone: CustomZone, zone_mask: np.ndarray,
                  policy: MandatoryTouchPolicy, lang: str = "es") -> ZoneTouch:
    inside = _inside_mask_for_path(zone_mask, nodes)

    # Visitas = número de componentes contiguas dentro de la zona.
    visits = 0
    prev = False
    for cur in inside:
        if cur and not prev:
            visits += 1
        prev = cur

    # Permanencia: longitud recorrida con al menos un extremo dentro.
    dwell = 0.0
    for i, e in enumerate(edges):
        if i + 1 < len(inside) and (inside[i] or inside[i + 1]):
            dwell += float(g.elen[e])

    from ..messages import msg

    violations: list[str] = []
    name = zone.name or zone.id or "zona"
    if visits < policy.min_visits:
        violations.append(msg("touch_missing", lang, z=name))
    if policy.reject_repeated_visits and visits > policy.max_visits:
        violations.append(msg("touch_repeat", lang, z=name, v=visits, m=policy.max_visits))
    if policy.reject_excessive_dwell and dwell > policy.max_dwell_m:
        violations.append(msg("touch_dwell", lang, z=name, d=dwell, m=policy.max_dwell_m))

    return ZoneTouch(name=name, visits=visits, dwell_m=dwell,
                     touched=visits >= policy.min_visits, violations=violations)


def evaluate_all(g: RaceGraph, nodes: list[int], edges: list[int],
                 zones: list[CustomZone], masks: dict[str, np.ndarray],
                 policy: MandatoryTouchPolicy, lang: str = "es") -> list[ZoneTouch]:
    if not policy.enabled:
        return []
    out = []
    for z in zones:
        key = z.id or z.name
        mask = masks.get(key)
        if mask is None:
            continue
        out.append(analyze_touch(g, nodes, edges, z, mask, policy, lang))
    return out
