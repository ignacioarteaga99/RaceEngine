"""ConstraintModel — modelo canónico de restricciones (doc 04_Optimization_Layers_Spec).

Reglas de diseño heredadas de la auditoría (doc 10):
  · El modelo es INMUTABLE (frozen). Nada puede mutarlo a mitad de una generación.
    Corrige la clase de bug de `apply_mandatory_radius_boost` del MVP.
  · Cada parámetro declara si es HARD o SOFT (BR-4.a).
  · Las zonas son GeoJSON (punto con buffer o polígono), no círculos ad hoc.
  · Se elimina `preferred_zone` por decisión de producto.
"""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .geo import GeoJSONPoint, GeoJSONPolygon, GeoPoint


# ──────────────────────────────────────────────────────────────────────────────
# Enumeraciones
# ──────────────────────────────────────────────────────────────────────────────
class Objective(str, Enum):
    """Objetivo primario del Smart Mode → tabla de pesos de doc 06 §6.2."""

    BALANCED = "balanced"
    FLATTEST = "flattest"
    FEWEST_CLOSURES = "fewest_closures"
    MOST_SCENIC = "most_scenic"
    FASTEST = "fastest"
    # Lo contrario de `flattest`, y no es una rareza: en cicloturismo el desnivel
    # es el producto, no el peaje. Sin esto, «quiero una ruta de montaña» sólo se
    # podía pedir desactivando la preferencia por lo llano, que es pedirlo al revés.
    CLIMBING = "climbing"


class Discipline(str, Enum):
    """Para qué es el recorrido. Decide qué vías existen.

    No es una preferencia estética: es la restricción más dura de todas, porque
    determina el grafo sobre el que se busca. Una nacional es la columna vertebral
    de una marcha cicloturista y un sitio donde jamás se manda a un corredor; un
    camino agrícola es el pan del gravel y una trampa para un 10K urbano.

    Vive en la raíz del modelo y no dentro de L6 · Calles porque es anterior a
    cualquier capa: primero se decide de qué deporte hablamos, y luego se afinan
    las restricciones dentro de ese mundo.
    """

    RUNNING = "running"          # carrera a pie en ciudad — comportamiento histórico
    TRAIL = "trail"              # carrera por monte: sendas y caminos
    HIKING = "hiking"            # senderismo: además escaleras y lo más técnico
    ROAD = "road"                # bici de ruta, asfalto
    GRAVEL = "gravel"            # ruta + caminos y pistas
    MTB = "mtb"                  # además sendas


# Clases de vía admisibles por disciplina. Los nombres son los de `highway=*` de
# OpenStreetMap; la traducción a índices de `eclass` la hace el motor.
#
# `running` reproduce EXACTAMENTE el prefiltro histórico (doc 07 §6.3.1): un
# recorrido a pie generado antes y otro generado ahora usan el mismo mundo.
DISCIPLINE_CLASSES: dict[str, tuple[str, ...]] = {
    "running": (
        "residential", "living_street", "tertiary", "secondary", "primary",
        "tertiary_link", "secondary_link", "primary_link", "unclassified",
        "pedestrian",
    ),
    # Trail: lo mismo que correr en ciudad, más el monte. No lleva nacionales —
    # una carrera por monte que sale a la nacional deja de ser una carrera por
    # monte— y sí lleva sendas, cañadas y caminos agrícolas.
    "trail": (
        "residential", "living_street", "tertiary", "unclassified", "pedestrian",
        "track", "path", "bridleway", "footway", "cycleway", "service",
    ),
    # Senderismo: como trail, más las escaleras. Andando se suben; corriendo una
    # carrera, no se mandan.
    "hiking": (
        "residential", "living_street", "tertiary", "unclassified", "pedestrian",
        "track", "path", "bridleway", "footway", "cycleway", "service", "steps",
    ),
    "road": (
        "residential", "living_street", "tertiary", "secondary", "primary",
        "tertiary_link", "secondary_link", "primary_link", "unclassified",
        "trunk", "trunk_link", "cycleway", "service",
    ),
    "gravel": (
        "residential", "living_street", "tertiary", "secondary", "primary",
        "tertiary_link", "secondary_link", "primary_link", "unclassified",
        "trunk", "trunk_link", "cycleway", "service", "track", "path",
    ),
    "mtb": (
        "residential", "living_street", "tertiary", "secondary", "primary",
        "tertiary_link", "secondary_link", "primary_link", "unclassified",
        "trunk", "trunk_link", "cycleway", "service", "track", "path",
        "bridleway", "pedestrian",
    ),
}

CYCLING = ("road", "gravel", "mtb")
# Disciplinas de monte: comparten el modelo de ritmo por pendiente (Tobler), que
# no se parece ni al asfalto ni a la bici.
OFFROAD_FOOT = ("trail", "hiking")


class RouteShape(str, Enum):
    CIRCUIT = "circuit"          # start = finish
    OUT_AND_BACK = "out_and_back"
    OPEN = "open"                # A → B


class Surface(str, Enum):
    PAVED = "paved"
    ASPHALT = "asphalt"
    CONCRETE = "concrete"
    GRAVEL = "gravel"
    DIRT = "dirt"
    COBBLE = "cobble"
    UNKNOWN = "unknown"


class ZoneBehavior(str, Enum):
    FORBIDDEN = "forbidden"            # HARD — la ruta no puede entrar
    MANDATORY = "mandatory"            # HARD — la ruta debe pasar
    START = "start"
    FINISH = "finish"
    AID_STATION = "aid_station"


# ──────────────────────────────────────────────────────────────────────────────
# Capas
# ──────────────────────────────────────────────────────────────────────────────
class DistanceLayer(BaseModel):
    """L1 · Distancia — 🔒 HARD."""

    model_config = ConfigDict(frozen=True)
    # Hasta 400 km: una marcha cicloturista larga o una etapa de gran fondo no
    # cabían en el tope de 100 km, que estaba pensado para un maratón.
    target_m: float = Field(default=10_000, ge=500, le=400_000)
    tolerance_pct: float = Field(default=2.0, ge=0.5, le=15.0)
    unit: Literal["m", "km", "mi"] = "km"

    # Cómo se plantea el recorrido.
    #
    #   target — la distancia manda. Es lo correcto en competición: un 10K son
    #            10 km y un maratón son 42,195, y eso se anuncia.
    #   range  — la distancia es una horquilla y lo que manda es el DESNIVEL.
    #            Es como se plantea el cicloturismo: nadie dice «quiero 137 km»,
    #            dice «quiero 2.500 metros de desnivel y que me dé el día».
    #   free   — NO hay distancia objetivo. Se da salida, puntos de paso y meta, y
    #            lo que se pide es la ruta lógica entre ellos: la que resulte de
    #            aplicar el objetivo elegido —más llana, más corta, menos tráfico—
    #            y la que salga, salga. Es lo que uno quiere cuando dice «llévame
    #            de aquí a allí pasando por ahí», y forzar kilómetros ahí no es
    #            una restricción: es un estorbo que inventa rodeos.
    #
    # No es un matiz de interfaz: cambia el pipeline entero. En `free` no hay
    # generación de circuitos ni ajuste de distancia, hay encadenado de caminos.
    mode: Literal["target", "range", "free"] = "target"
    range_min_m: float = Field(default=0, ge=0, le=400_000)
    range_max_m: float = Field(default=0, ge=0, le=400_000)

    # Cómo se mide el recorrido.
    #
    #   centerline — longitud por el eje de la calzada. Es lo que mide cualquier
    #                herramienta de rutas, y lo que vale para entrenar.
    #   certified  — longitud por la RUTA MÁS CORTA POSIBLE (tangentes cortadas,
    #                a un palmo del bordillo) y objetivo multiplicado por el
    #                factor de prevención de recorrido corto de 1,001, que es
    #                como mide el medidor oficial de World Athletics / AIMS.
    #
    # La diferencia no es cosmética: en un recorrido urbano con cientos de curvas
    # son cientos de metros. Un maratón que en el eje mide 42,195 km mediría
    # CORTO el día de la certificación, y un maratón corto no es un maratón.
    measurement: Literal["centerline", "certified"] = "centerline"

    @property
    def is_free(self) -> bool:
        return self.mode == "free"

    @property
    def tolerance_m(self) -> float:
        if self.mode == "free":
            # Banda enorme a propósito: todo lo que consulta `tolerance_m` —el
            # ajustador, la puerta de distancia desmedida, el ajuste del score—
            # deja de morder solo, sin tener que enterarse de que hay un modo más.
            return 400_000.0
        if self.mode == "range":
            # En modo horquilla la «tolerancia» es la semianchura del rango: así
            # todo lo que ya usaba `tolerance_m` —el ajustador, la puerta de
            # distancia desmedida, el ajuste de la puntuación— sigue funcionando
            # sin enterarse de que hay dos modos.
            return max((self.range_max_m - self.range_min_m) / 2.0, 1.0)
        return self.target_m * self.tolerance_pct / 100.0

    @property
    def min_m(self) -> float:
        if self.mode == "free":
            return 0.0
        if self.mode == "range":
            return self.range_min_m
        return self.target_m - self.tolerance_m

    @property
    def max_m(self) -> float:
        if self.mode == "free":
            return 400_000.0
        if self.mode == "range":
            return self.range_max_m
        return self.target_m + self.tolerance_m

    @property
    def center_m(self) -> float:
        """El punto de la banda al que apuntar cuando no hay otro criterio."""
        if self.mode == "free":
            return 0.0
        if self.mode == "range":
            return (self.range_min_m + self.range_max_m) / 2.0
        return self.target_m

    def in_tolerance(self, d: float) -> bool:
        return self.min_m <= d <= self.max_m

    @model_validator(mode="after")
    def _check_range(self) -> "DistanceLayer":
        if self.mode == "range":
            if self.range_max_m <= self.range_min_m:
                raise ValueError(
                    "VR-1b: en modo horquilla, range_max_m debe superar a range_min_m")
            if self.range_min_m <= 0:
                raise ValueError("VR-1b: range_min_m debe ser positivo")
        return self


class RouteTypeLayer(BaseModel):
    """L2 · Tipo de recorrido — 🔒 HARD."""

    model_config = ConfigDict(frozen=True)
    shape: RouteShape = RouteShape.CIRCUIT
    start_equals_finish: bool = True
    max_loops: int = Field(default=1, ge=1, le=10)
    min_loop_length_m: float = Field(default=1000, ge=200, le=50_000)
    allow_segment_reuse: bool = False

    # Vueltas al mismo circuito. Nadie diseña «42 km reutilizando tramos»: diseña
    # «tres vueltas de 14». Y no es una preferencia estética — un circuito de
    # vueltas concentra cierres, personal de cruce, hidratación y ambulancias en
    # una fracción de la superficie, que es la partida más cara del presupuesto.
    laps: int = Field(default=1, ge=1, le=40)


class ElevationLayer(BaseModel):
    """L3 · Elevación.

    A diferencia del MVP, estos parámetros SÍ se aplican durante la búsqueda:
    la elevación se hornea en las aristas al construir el grafo (corrige H1).
    """

    model_config = ConfigDict(frozen=True)
    enabled: bool = True
    max_gain_m: float | None = Field(default=None, ge=0, le=5000)      # 🔒 HARD si se define
    max_loss_m: float | None = Field(default=None, ge=0, le=5000)      # 🔒 HARD si se define
    max_gradient_pct: float | None = Field(default=None, ge=0, le=30)  # 🔒 HARD si se define
    avoid_long_climbs: bool = False                                    # 🎚️ SOFT
    long_climb_m: float = Field(default=800, ge=100, le=5000)
    prefer_flatter: bool = True                                        # 🎚️ SOFT

    # Desnivel BUSCADO, no máximo permitido. Es el criterio con el que se plantea
    # una ruta de bici: «dame 2.500 metros de desnivel». Con esto puesto, el
    # ajuste de la puntuación deja de premiar lo llano y premia acercarse a la
    # cifra — que puede significar buscar cuestas, no evitarlas.
    target_gain_m: float | None = Field(default=None, ge=0, le=15_000)  # 🎚️ SOFT
    gain_tolerance_pct: float = Field(default=15.0, ge=2.0, le=60.0)


class GeometryLayer(BaseModel):
    """L4 · Geometría."""

    model_config = ConfigDict(frozen=True)
    # Hasta 150 km: una gran fondo de 180 km necesita un radio de búsqueda que
    # ni de lejos cabía en el tope de 50 km, pensado para un maratón urbano.
    search_radius_max_m: float = Field(default=5000, ge=500, le=150_000)  # 🔒 HARD
    search_radius_min_m: float = Field(default=0, ge=0, le=10_000)        # 🎚️ SOFT
    smoothness: float = Field(default=0.5, ge=0.0, le=1.0)                # 🎚️ SOFT
    max_turns: int | None = Field(default=None, ge=0, le=500)             # 🔀
    max_uturns: int = Field(default=4, ge=0, le=50)                       # 🔀
    minimize_zigzag: bool = True                                          # 🎚️ SOFT


class IntersectionLayer(BaseModel):
    """L5 · Intersecciones."""

    model_config = ConfigDict(frozen=True)
    max_crossings: int | None = Field(default=None, ge=0, le=1000)
    max_traffic_lights: int | None = Field(default=None, ge=0, le=500)
    penalize_crossings: bool = True                                       # 🎚️ SOFT


class StreetsLayer(BaseModel):
    """L6 · Calles."""

    model_config = ConfigDict(frozen=True)
    allowed_surfaces: tuple[Surface, ...] = (
        Surface.PAVED, Surface.ASPHALT, Surface.CONCRETE, Surface.UNKNOWN,
    )
    avoid_private: bool = True                                            # 🔒 HARD
    # min_width y pavement_quality son advisory-only en v1 (doc 04 §L6, VR-6).


class LogisticsLayer(BaseModel):
    """L7 · Logística."""

    model_config = ConfigDict(frozen=True)
    minimize_closures: bool = True                                        # 🎚️ SOFT
    prioritize_wide_streets: bool = True                                  # 🎚️ SOFT
    near_hospitals: bool = True                                           # 🎚️ SOFT
    hospital_radius_m: float = Field(default=3000, ge=200, le=5000)

    # Tamaño del pelotón. Sin este dato no se puede decir si el recorrido cabe:
    # un tramo de 6 m con 5.000 inscritos es un embudo, y eso se sabe con
    # aritmética antes de que exista la carrera.
    field_size: int | None = Field(default=None, ge=1, le=200_000)


class ExperienceLayer(BaseModel):
    """L8 · Experiencia."""

    model_config = ConfigDict(frozen=True)
    prioritize_parks: bool = False                                        # 🎚️ SOFT
    avoid_tunnels: bool = True                                            # 🔒 HARD


class CustomZone(BaseModel):
    """L10 · Zonas dibujadas por el usuario.

    Acepta punto+buffer o polígono. Se elimina el comportamiento `preferred`.
    """

    model_config = ConfigDict(frozen=True)
    id: str = ""
    name: str = ""
    behavior: ZoneBehavior
    geometry: GeoJSONPoint | GeoJSONPolygon
    buffer_m: float = Field(default=250.0, ge=0, le=5000)

    @property
    def center(self) -> tuple[float, float]:
        """Devuelve (lat, lon)."""
        if isinstance(self.geometry, GeoJSONPoint):
            return (self.geometry.lat, self.geometry.lon)
        lon, lat = self.geometry.centroid()
        return (lat, lon)


class MandatoryTouchPolicy(BaseModel):
    """Política de paso por zonas obligatorias (portada del MVP, doc 10 §5).

    Una zona obligatoria es un CHECKPOINT que se toca, no un imán de scoring.
    """

    model_config = ConfigDict(frozen=True)
    enabled: bool = True
    min_visits: int = 1
    max_visits: int = 1
    max_dwell_m: float = 1400.0
    reject_repeated_visits: bool = True
    reject_excessive_dwell: bool = True


class ScoringWeights(BaseModel):
    """Pesos del score global (doc 06 §6.1). Solo Expert Mode."""

    model_config = ConfigDict(frozen=True)
    fit: float = 0.30
    speed: float = 0.20
    logistics: float = 0.25
    scenery: float = 0.15
    difficulty: float = 0.10

    def normalized(self) -> "ScoringWeights":
        s = self.fit + self.speed + self.logistics + self.scenery + self.difficulty
        s = s or 1.0
        return ScoringWeights(
            fit=self.fit / s, speed=self.speed / s, logistics=self.logistics / s,
            scenery=self.scenery / s, difficulty=self.difficulty / s,
        )


# Tabla objetivo → pesos, doc 06 §6.2 (valores aprobados).
OBJECTIVE_WEIGHTS: dict[Objective, ScoringWeights] = {
    Objective.FLATTEST:        ScoringWeights(fit=.30, speed=.15, logistics=.15, scenery=.05, difficulty=.35),
    Objective.FEWEST_CLOSURES: ScoringWeights(fit=.30, speed=.15, logistics=.40, scenery=.05, difficulty=.10),
    Objective.MOST_SCENIC:     ScoringWeights(fit=.25, speed=.10, logistics=.15, scenery=.40, difficulty=.10),
    Objective.FASTEST:         ScoringWeights(fit=.30, speed=.40, logistics=.15, scenery=.05, difficulty=.10),
    Objective.BALANCED:        ScoringWeights(fit=.30, speed=.20, logistics=.25, scenery=.15, difficulty=.10),
    # `difficulty` baja a casi nada: castigar la dureza cuando la dureza es lo que
    # se busca sería contradecirse. El peso se va a `fit`, que con un desnivel
    # objetivo puesto ya mide medio distancia y medio desnivel.
    Objective.CLIMBING:        ScoringWeights(fit=.50, speed=.10, logistics=.15, scenery=.22, difficulty=.03),
}


# ──────────────────────────────────────────────────────────────────────────────
# El modelo completo
# ──────────────────────────────────────────────────────────────────────────────
class ConstraintModel(BaseModel):
    """Contrato único entre frontend, API y motor. INMUTABLE."""

    model_config = ConfigDict(frozen=True)

    region: str = "demo"
    # Anterior a todas las capas: decide sobre qué grafo se busca (ver Discipline).
    discipline: Discipline = Discipline.RUNNING
    start: GeoPoint
    finish: GeoPoint | None = None

    distance: DistanceLayer = DistanceLayer()
    routetype: RouteTypeLayer = RouteTypeLayer()
    elevation: ElevationLayer = ElevationLayer()
    geometry: GeometryLayer = GeometryLayer()
    intersections: IntersectionLayer = IntersectionLayer()
    streets: StreetsLayer = StreetsLayer()
    logistics: LogisticsLayer = LogisticsLayer()
    experience: ExperienceLayer = ExperienceLayer()
    zones: tuple[CustomZone, ...] = ()
    touch_policy: MandatoryTouchPolicy = MandatoryTouchPolicy()

    objective: Objective = Objective.BALANCED
    weights: ScoringWeights | None = None
    mode: Literal["smart", "expert"] = "smart"
    # Idioma de los textos que produce el MOTOR (explicaciones, avisos, veredictos).
    # Vive aquí y no en el cliente para no duplicar la lógica de qué decir.
    lang: Literal["es", "en"] = "es"

    # Presupuestos (doc 11 §3.2)
    fast_budget_ms: int = Field(default=3000, ge=500, le=60_000)
    total_budget_ms: int = Field(default=25_000, ge=1000, le=300_000)

    # Reproducibilidad estricta (NFR-6.1). Con un número fijo de iteraciones el
    # conjunto de candidatos deja de depender del reloj de pared, y por tanto la
    # normalización relativa de doc 06 §4 se vuelve exactamente reproducible.
    # Es el modo que usa el golden master en CI.
    deterministic_iterations: int | None = Field(default=None, ge=0, le=100_000)

    # Modo reproducible (por defecto).
    #
    # Es la diferencia entre «gasta 20 segundos de reloj» y «haz una cantidad fija
    # de trabajo». Con presupuesto por reloj, dos ejecuciones idénticas terminan en
    # iteraciones distintas —porque la máquina no siempre tarda lo mismo— y por lo
    # tanto devuelven rutas distintas. Eso rompe la promesa de NFR-6.1 y, peor, es
    # desconcertante: el usuario cambia nada y obtiene otra cosa.
    #
    # Con `reproducible=True` el motor convierte el presupuesto en un número de
    # iteraciones derivado del objetivo, y ése es el criterio de parada. El reloj
    # queda sólo como red de seguridad. Consecuencia asumida: en una máquina lenta
    # la misma petición tarda más, en vez de devolver un resultado peor y distinto.
    reproducible: bool = True

    @model_validator(mode="after")
    def _coherence(self) -> "ConstraintModel":
        if self.routetype.start_equals_finish and self.routetype.shape == RouteShape.OPEN:
            raise ValueError("VR-1: start_equals_finish=true es incompatible con shape=open")
        if self.routetype.shape == RouteShape.OPEN and self.finish is None:
            raise ValueError(
                "VR-1: un recorrido abierto (inicio ≠ meta) necesita un punto de meta"
            )
        return self

    # ── Accesores de zonas ────────────────────────────────────────────────────
    @property
    def mandatory_zones(self) -> list[CustomZone]:
        return [z for z in self.zones if z.behavior == ZoneBehavior.MANDATORY]

    @property
    def forbidden_zones(self) -> list[CustomZone]:
        return [z for z in self.zones if z.behavior == ZoneBehavior.FORBIDDEN]

    def effective_weights(self) -> ScoringWeights:
        w = self.weights or OBJECTIVE_WEIGHTS[self.objective]
        return w.normalized()

    # ── Determinismo (doc 11 §7) ──────────────────────────────────────────────
    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        )

    def fingerprint(self, graph_version: str = "", engine_version: str = "") -> str:
        """Hash estable de la petición. Base de la semilla y de la caché."""
        h = hashlib.blake2b(digest_size=16)
        h.update(self.canonical_json().encode("utf-8"))
        h.update(graph_version.encode("utf-8"))
        h.update(engine_version.encode("utf-8"))
        return h.hexdigest()


# Presets del doc 04 §5 (seis).
PRESETS: dict[str, dict] = {
    "mile":     {"target_m": 1609,  "tolerance_pct": 0.5, "radius_m": 1000,  "shape": "open"},
    "urban_5k": {"target_m": 5000,  "tolerance_pct": 2.0, "radius_m": 2500,  "shape": "circuit"},
    "urban_10k": {"target_m": 10000, "tolerance_pct": 2.0, "radius_m": 4000, "shape": "circuit"},
    "half":     {"target_m": 21097, "tolerance_pct": 1.0, "radius_m": 8000,  "shape": "circuit"},
    "marathon": {"target_m": 42195, "tolerance_pct": 1.0, "radius_m": 15000, "shape": "circuit"},
    "corporate": {"target_m": 5000, "tolerance_pct": 5.0, "radius_m": 2000,  "shape": "circuit"},
}

# Presets de bicicleta. Aquí la horquilla de distancia es ancha a propósito: en
# una marcha nadie promete 137 km exactos, promete un recorrido y un desnivel.
# Los que sí son competición —la crono, el criterium— vuelven a la distancia dura.
CYCLING_PRESETS: dict[str, dict] = {
    "crit": {                      # criterium urbano: vueltas rápidas a un circuito
        "discipline": "road", "target_m": 40_000, "tolerance_pct": 2.0,
        "radius_m": 3000, "shape": "circuit", "laps": 20, "mode": "target",
    },
    "tt_40": {                     # contrarreloj de 40 km
        "discipline": "road", "target_m": 40_000, "tolerance_pct": 1.0,
        "radius_m": 12_000, "shape": "open", "mode": "target",
    },
    "granfondo_120": {
        "discipline": "road", "mode": "range",
        "range_min_m": 110_000, "range_max_m": 135_000,
        "target_gain_m": 1800, "radius_m": 40_000, "shape": "circuit",
    },
    "granfondo_180": {
        "discipline": "road", "mode": "range",
        "range_min_m": 165_000, "range_max_m": 195_000,
        "target_gain_m": 3000, "radius_m": 60_000, "shape": "circuit",
    },
    "gravel_80": {
        "discipline": "gravel", "mode": "range",
        "range_min_m": 70_000, "range_max_m": 95_000,
        "target_gain_m": 1200, "radius_m": 30_000, "shape": "circuit",
    },
    "mtb_50": {
        "discipline": "mtb", "mode": "range",
        "range_min_m": 42_000, "range_max_m": 58_000,
        "target_gain_m": 1500, "radius_m": 20_000, "shape": "circuit",
    },
}

# Presets de monte a pie. El trail de competición sí anuncia distancia; la ruta
# de senderismo se plantea por horas y desnivel, nunca por kilómetros exactos.
TRAIL_PRESETS: dict[str, dict] = {
    "trail_10": {
        "discipline": "trail", "mode": "target", "target_m": 10_000,
        "tolerance_pct": 3.0, "target_gain_m": 400, "radius_m": 6000,
        "shape": "circuit",
    },
    "trail_21": {
        "discipline": "trail", "mode": "target", "target_m": 21_000,
        "tolerance_pct": 3.0, "target_gain_m": 900, "radius_m": 12_000,
        "shape": "circuit",
    },
    "trail_42": {
        "discipline": "trail", "mode": "target", "target_m": 42_000,
        "tolerance_pct": 3.0, "target_gain_m": 2000, "radius_m": 20_000,
        "shape": "circuit",
    },
    "ultra_80": {
        "discipline": "trail", "mode": "range",
        "range_min_m": 72_000, "range_max_m": 92_000,
        "target_gain_m": 4000, "radius_m": 35_000, "shape": "circuit",
    },
    "hike_day": {
        "discipline": "hiking", "mode": "range",
        "range_min_m": 12_000, "range_max_m": 22_000,
        "target_gain_m": 800, "radius_m": 12_000, "shape": "circuit",
    },
    "hike_summit": {
        "discipline": "hiking", "mode": "range",
        "range_min_m": 8_000, "range_max_m": 18_000,
        "target_gain_m": 1400, "radius_m": 12_000, "shape": "open",
    },
}
