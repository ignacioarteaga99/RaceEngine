"""Contratos de salida: candidato, incumbente y resultado de generación."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from .metrics import Indices, NormalizedMetrics, OpsFlag, RawMetrics, ScoreBreakdown


class Stage(str, Enum):
    SEED = "seed"
    REPAIRED = "repaired"
    ADJUSTED = "adjusted"
    IMPROVED = "improved"


class Provenance(BaseModel):
    """Registro de auditoría por ruta (NFR-6). El MVP no tenía nada de esto."""

    model_config = ConfigDict(frozen=False)
    seed: str = ""
    graph_version: str = ""
    engine_version: str = ""
    operators: list[str] = Field(default_factory=list)
    iteration: int = 0
    generator: str = ""


class RouteCandidate(BaseModel):
    model_config = ConfigDict(frozen=False)

    route_id: str = ""
    coordinates: list[tuple[float, float, float]] = Field(default_factory=list)  # lon, lat, elev
    node_path: list[int] = Field(default_factory=list)

    metrics: RawMetrics = Field(default_factory=RawMetrics)
    normalized: NormalizedMetrics = Field(default_factory=NormalizedMetrics)
    indices: Indices = Field(default_factory=Indices)
    global_score: float = 0.0
    breakdown: ScoreBreakdown = Field(default_factory=ScoreBreakdown)

    mandatory_covered: list[str] = Field(default_factory=list)
    mandatory_missing: list[str] = Field(default_factory=list)
    in_tolerance: bool = False
    hard_violations: list[str] = Field(default_factory=list)
    ops_flags: list[OpsFlag] = Field(default_factory=list)
    provenance: Provenance = Field(default_factory=Provenance)

    def geojson(self) -> dict:
        return {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": self.coordinates},
            "properties": {
                "route_id": self.route_id,
                "global_score": self.global_score,
                "distance_m": self.metrics.distance_m,
                "in_tolerance": self.in_tolerance,
            },
        }


class Incumbent(BaseModel):
    """Contrato anytime: siempre existe una respuesta válida, y solo mejora."""

    model_config = ConfigDict(frozen=False)
    route: RouteCandidate
    stage: Stage
    found_at_ms: int = 0
    iteration: int = 0
    improvement: float | None = None


class BlockingConstraint(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    rule: str
    detail: str
    suggested_relaxation: dict = Field(default_factory=dict)


class InfeasibilityReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    blocking: tuple[BlockingConstraint, ...] = ()
    warnings: tuple[str, ...] = ()
    preflight_ms: int = 0

    @property
    def feasible(self) -> bool:
        return len(self.blocking) == 0


class Attribution(BaseModel):
    """Servicio central de atribución (NFR-11.1/11.2)."""

    model_config = ConfigDict(frozen=True)
    text: str = "© OpenStreetMap contributors (ODbL)"
    elevation: str = ""


class GenerationResult(BaseModel):
    model_config = ConfigDict(frozen=False)

    generation_id: str
    status: str = "running"      # running | converged | cancelled | infeasible | failed
    recommended: RouteCandidate | None = None
    alternatives: list[RouteCandidate] = Field(default_factory=list)
    seed: str = ""
    graph_version: str = ""
    engine_version: str = ""
    elapsed_ms: int = 0
    stage: str = ""
    progress: float = 0.0
    infeasibility: InfeasibilityReport | None = None
    attribution: Attribution = Field(default_factory=Attribution)
    log: list[str] = Field(default_factory=list)
    # ¿Volvería a salir exactamente esto con la misma configuración? Falso si el
    # presupuesto por reloj cortó la búsqueda antes de completar el trabajo fijado.
    reproducible: bool = False
