"""GraphContext — todo el trabajo estático, hecho una sola vez.

El MVP recomputaba en cada ejecución: cargar pickle → race_nodes() → grados →
dos GridIndex. Y el proceso moría al terminar (doc 10 §2, B9). Aquí el contexto
se pre-calienta al arrancar el worker y se comparte entre peticiones.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from raceengine_geodata import RaceGraph, load_region
from raceengine_routing import LocalRoutingProvider

_CONTEXTS: dict[str, "GraphContext"] = {}


@dataclass
class GraphContext:
    region_id: str
    graph: RaceGraph
    provider: LocalRoutingProvider
    esrc: np.ndarray                 # int32[E] nodo origen de cada arista
    intersections: np.ndarray        # bool[N]
    built_at: float
    # Coste medio de una consulta de camino mínimo sobre ESTE grafo, en ms.
    # Es la escala que permite convertir un presupuesto en segundos a una cantidad
    # de trabajo. Sin ella, una calibración hecha sobre una región de 5.000 nodos
    # se aplicaba tal cual a una de 120.000 y el motor pedía veinte veces más
    # trabajo del que cabía en el presupuesto.
    astar_ms: float = 1.0

    @property
    def version(self) -> str:
        return self.graph.version or self.region_id

    def elevation_available(self) -> bool:
        return bool(np.ptp(self.graph.elev) > 1.0)

    def confidence_level(self, attr: str, default: str = "deferred") -> str:
        info = self.graph.coverage.get(attr)
        return info.get("level", default) if info else default


CALIBRATION_PAIRS = 24
# Cuánto tiene que cambiar la medida para que se acepte la nueva. Más de un
# peldaño entero de la escala geométrica.
HYSTERESIS = 1.45
CALIBRATION_LEG_M = 1500.0


def calibrate(ctx: GraphContext) -> float:
    """Mide cuánto cuesta una consulta de ruta típica en ESTE grafo, en ms.

    Determinista a propósito: los pares se eligen por índice, no al azar, así que
    dos arranques sobre la misma región miden lo mismo. Es el número que convierte
    «20 segundos de presupuesto» en «tantas iteraciones», y de ahí que el resultado
    pueda ser a la vez acotado en tiempo y reproducible.
    """
    from raceengine_routing import RoutingWeights

    g = ctx.graph
    if g.n_nodes < 8 or g.n_edges < 8:
        return 1.0
    w = RoutingWeights(
        allowed_edges=(g.eclass >= 0),
        weight=g.elen.astype(np.float32),
        min_ratio=1.0, max_ratio=1.0,
    )
    step = max(1, g.n_nodes // (CALIBRATION_PAIRS * 2))
    seeds = list(range(0, g.n_nodes, step))[:CALIBRATION_PAIRS]
    t0 = time.perf_counter()
    done = 0
    for s in seeds:
        # Destino a ~1,5 km del origen, elegido de forma determinista.
        near = g.grid.within(float(g.x[s]), float(g.y[s]), CALIBRATION_LEG_M)
        if near is None or len(near) < 4:
            continue
        # El destino debe estar de verdad a ~1,5 km: un nodo cualquiera del disco
        # puede caer a cincuenta metros, y entonces la medida no representa nada.
        d = np.hypot(g.x[near] - float(g.x[s]), g.y[near] - float(g.y[s]))
        dst = int(near[int(np.argmax(d))])
        if dst == s or float(d.max()) < CALIBRATION_LEG_M * 0.5:
            continue
        ctx.provider.shortest_path(int(s), dst, w,
                                   max_distance_m=CALIBRATION_LEG_M * 2.5)
        done += 1
    if not done:
        return 1.0
    raw = max(0.05, (time.perf_counter() - t0) * 1000.0 / done)
    # Se REDONDEA a una escala geométrica gruesa. Una medida de tiempo tiene ruido
    # de un 10–20 %, y si ese ruido se propaga al número de iteraciones, dos
    # arranques del servidor sobre la misma región producen rutas distintas —
    # justo lo que el modo reproducible existe para evitar. Con peldaños de 1,3×
    # el ruido normal no cambia de peldaño.
    import math
    k = round(math.log(raw / 0.05) / math.log(1.3))
    nuevo = 0.05 * (1.3 ** k)

    # HISTÉRESIS. Cuantizar solo no basta: si el valor real cae justo al lado de
    # un peldaño, el ruido normal lo hace saltar de uno a otro y vuelve el
    # problema que la cuantización venía a resolver. Con la máquina cargada se vio
    # exactamente eso — dos medidas seguidas del mismo grafo dieron 0,53 y 0,69,
    # que son peldaños contiguos.
    #
    # Una vez medido, sólo se cambia si la máquina ha cambiado DE VERDAD: un
    # factor de 1,45, que es más de un peldaño entero. Reiniciar el servidor no
    # debe cambiar el recorrido que devuelve.
    #
    # La histéresis se mide contra lo ya medido para ESTE contexto, así que el
    # resultado se guarda aquí mismo. Dejar que lo hiciera sólo quien llama era
    # el fallo: dos llamadas seguidas no se veían la una a la otra y volvían a
    # saltar de peldaño.
    previo = getattr(ctx, "astar_ms", 0.0) or 0.0
    if previo > 0 and (previo / HYSTERESIS) <= raw <= (previo * HYSTERESIS):
        return previo
    try:
        ctx.astar_ms = nuevo
    except Exception:  # noqa: BLE001 — un contexto de sólo lectura no debe romper
        pass
    return nuevo


def build_context(region_id: str) -> GraphContext:
    t0 = time.time()
    g = load_region(region_id)
    esrc = np.repeat(
        np.arange(g.n_nodes, dtype=np.int32), np.diff(g.indptr).astype(np.int32)
    )
    ctx = GraphContext(
        region_id=region_id,
        graph=g,
        provider=LocalRoutingProvider(g),
        esrc=esrc,
        intersections=g.is_intersection,
        built_at=time.time(),
        astar_ms=0.0,      # 0 = «sin medir»: la primera calibración manda
    )
    ctx.astar_ms = calibrate(ctx)
    print(f"[context] «{region_id}» precalentado en {time.time() - t0:.2f}s · "
          f"{g.n_nodes:,} nodos · {int(ctx.intersections.sum()):,} intersecciones · "
          f"elevación={'sí' if ctx.elevation_available() else 'no'} · "
          f"coste de ruta {ctx.astar_ms:.2f} ms")
    return ctx


def get_context(region_id: str) -> GraphContext:
    if region_id not in _CONTEXTS:
        _CONTEXTS[region_id] = build_context(region_id)
    return _CONTEXTS[region_id]


def warm(region_ids: list[str]) -> None:
    for r in region_ids:
        try:
            get_context(r)
        except Exception as exc:  # noqa: BLE001
            print(f"[context] no se pudo precalentar «{r}»: {exc}")
