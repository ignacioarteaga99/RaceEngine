"""Catálogo de métricas crudas — doc 06 §3.

Cada métrica lleva su nivel de confianza (doc 07 §4.1) para que se propague hasta
los índices tomando el mínimo. Nada se presenta como medido si es inferido.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Confidence(str, Enum):
    RELIABLE = "reliable"    # 🟢 ≥80% de cobertura de atributo → factor 1.0
    PARTIAL = "partial"      # 🟡 40–79%                        → factor 0.6
    DEFERRED = "deferred"    # 🔴 <40%                          → factor 0.3


CONFIDENCE_FACTOR: dict[Confidence, float] = {
    Confidence.RELIABLE: 1.0,
    Confidence.PARTIAL: 0.6,
    Confidence.DEFERRED: 0.3,
}


class RawMetrics(BaseModel):
    """Hechos medibles de una ruta. Se persisten SIEMPRE (doc 06 §4.2 mit. 1)."""

    model_config = ConfigDict(frozen=False)

    # 3.1 Distancia y forma
    distance_m: float = 0.0
    distance_error_pct: float = 0.0
    turns: int = 0
    sharp_turns: int = 0
    uturns: int = 0
    smoothness: float = 0.0          # 0..1 — formaliza `elegance` del MVP
    avg_straight_m: float = 0.0
    longest_straight_m: float = 0.0

    # 3.2 Elevación
    gain_m: float = 0.0
    loss_m: float = 0.0
    grad_max_pct: float = 0.0
    grad_avg_pct: float = 0.0
    longest_climb_m: float = 0.0

    # 3.3 Intersecciones y fluidez
    crossings: int = 0
    lights: int = 0

    # 3.4 Tiempo (supuestos declarados en la UI, FR-6.3)
    est_time_min: float = 0.0
    assumed_pace_min_km: float = 5.0

    # 3.5 Logística
    closure_len_m: float = 0.0
    closure_impact: float = 0.0      # ponderado por clase de vía
    enclosed_intersections: int = 0  # proxy de aislamiento urbano (del MVP, formalizado)
    self_crossings: int = 0          # ya NO descarta: es coste + bandera operativa
    segment_reuse_pct: float = 0.0

    # ── Medición certificable (ver engine/measure.py) ──────────────────────
    spr_m: float = 0.0               # longitud por la ruta más corta posible
    spr_saving_m: float = 0.0        # cuánto acorta cortar tangentes
    certified_m: float = 0.0         # longitud que buscaría el medidor oficial

    # ── Capacidad ───────────────────────────────────────────────────────────
    min_width_m: float = 0.0         # anchura estimada del punto más estrecho
    bottleneck_m: float = 0.0        # dónde está ese punto, desde la salida
    flow_per_min: float = 0.0        # corredores por minuto que admite
    field_clear_min: float = 0.0     # minutos que tarda el pelotón en pasarlo
    width_needed_m: float = 0.0      # anchura que haría falta para ese pelotón

    # ── Ciclismo ─────────────────────────────────────────────────────────────
    # Un ciclista no lee «1.850 m de desnivel»: lee «tres puertos, el más duro de
    # 6,8 km al 7,2 %». El agregado no distingue una ruta dura de una sólo larga.
    climbs: int = 0                  # puertos: subida sostenida ≥ 500 m y ≥ 30 m
    hardest_climb_pct: float = 0.0   # pendiente media del puerto de más desnivel
    hardest_climb_m: float = 0.0     # y su longitud
    unpaved_share_pct: float = 0.0   # porcentaje de ruta sobre firme sin asfaltar

    # 3.6 Experiencia y seguridad
    green_share_pct: float = 0.0
    lit_share_pct: float = 0.0
    hosp_prox_m: float = 0.0

    # Confianza por familia de métricas
    conf_elevation: Confidence = Confidence.DEFERRED
    conf_surface: Confidence = Confidence.PARTIAL
    conf_lit: Confidence = Confidence.PARTIAL
    conf_logistics: Confidence = Confidence.PARTIAL
    conf_scenery: Confidence = Confidence.DEFERRED


class NormalizedMetrics(BaseModel):
    """Cada métrica mapeada a [0,1] de 'bondad'. doc 06 §4."""

    model_config = ConfigDict(frozen=False)
    fit: float = 0.0             # banda objetivo FIJA (doc 06 §4.2 mit. 3)
    # Ajuste al DESNIVEL buscado. Sólo cuenta si el usuario pidió una cifra de
    # desnivel; en una carrera a pie no se pide, se limita.
    gain_fit: float = 1.0
    crossings: float = 0.0
    lights: float = 0.0
    turns: float = 0.0
    smoothness: float = 0.0
    grad_avg: float = 0.0
    gain: float = 0.0
    grad_max: float = 0.0
    closure_impact: float = 0.0
    self_crossings: float = 0.0
    enclosure: float = 0.0
    green: float = 0.0
    lit: float = 0.0
    hospital: float = 0.0


class Indices(BaseModel):
    """Los cuatro índices de doc 06 §5. Cada uno hereda la confianza mínima."""

    model_config = ConfigDict(frozen=False)
    speed: float = 0.0
    logistics: float = 0.0
    scenery: float = 0.0
    difficulty: float = 0.0

    conf_speed: Confidence = Confidence.RELIABLE
    conf_logistics: Confidence = Confidence.PARTIAL
    conf_scenery: Confidence = Confidence.DEFERRED
    conf_difficulty: Confidence = Confidence.DEFERRED


class OpsFlag(BaseModel):
    """Hecho operativo que la capa de operaciones debe atender (FR-8).

    `message` va en el idioma de la petición; `message_i18n` lleva todas las
    variantes. Emitir ambas evita tener que regenerar la ruta —un cómputo de
    segundos— solo porque el usuario pulsó el botón de idioma.
    """

    model_config = ConfigDict(frozen=True)
    code: str
    message: str
    message_i18n: dict[str, str] = Field(default_factory=dict)
    severity: str = "info"          # info | warning | critical
    lat: float | None = None
    lon: float | None = None


class ScoreBreakdown(BaseModel):
    """Descomposición del score global — exigida por FR-6.5 (transparencia)."""

    model_config = ConfigDict(frozen=False)
    global_score: float = 0.0
    terms: dict[str, float] = Field(default_factory=dict)   # nombre → contribución
    weights: dict[str, float] = Field(default_factory=dict)
    explanation: list[str] = Field(default_factory=list)
    explanation_i18n: dict[str, list[str]] = Field(default_factory=dict)
