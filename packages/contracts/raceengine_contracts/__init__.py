"""raceengine_contracts — fuente única de verdad de los contratos del dominio."""
from .geo import (
    EARTH_R, GeoJSONPoint, GeoJSONPolygon, GeoPoint,
    angle_delta, bearing_deg, haversine, point_in_ring,
)
from .constraints import (
    CYCLING, CYCLING_PRESETS, DISCIPLINE_CLASSES, OBJECTIVE_WEIGHTS,
    OFFROAD_FOOT, PRESETS, TRAIL_PRESETS,
    ConstraintModel,
    CustomZone, Discipline, DistanceLayer,
    ElevationLayer, ExperienceLayer, GeometryLayer, IntersectionLayer,
    LogisticsLayer, MandatoryTouchPolicy, Objective, RouteShape, RouteTypeLayer,
    ScoringWeights, StreetsLayer, Surface, ZoneBehavior,
)
from .metrics import (
    CONFIDENCE_FACTOR, Confidence, Indices, NormalizedMetrics, OpsFlag,
    RawMetrics, ScoreBreakdown,
)
from .results import (
    Attribution, BlockingConstraint, GenerationResult, Incumbent,
    InfeasibilityReport, Provenance, RouteCandidate, Stage,
)

__all__ = [n for n in dir() if not n.startswith("_")]
