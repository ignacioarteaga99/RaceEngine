"""Anclaje de la salida y la meta a la red REALMENTE utilizable.

Por qué existe este módulo
──────────────────────────
Hasta ahora el motor hacía `g.nearest_node(lat, lon)` y seguía adelante. Ese nodo
es el más cercano del grafo **sin mirar las restricciones de la petición**. En una
ciudad con topografía marcada (Viña del Mar es el caso que lo destapó) pasa esto:

  · El usuario pincha sobre una calle real y perfectamente utilizable.
  · El nodo más próximo pertenece a un ramal que las restricciones duras dejan sin
    ninguna arista admisible: una escalera, un pasaje privado, un tramo cuya
    pendiente supera el máximo, o un trozo de red separado de la malla principal.
  · A partir de ahí TODO falla: el grafo admisible se ve vacío desde la salida y el
    preflight responde «no hay ninguna calle utilizable cerca del punto de partida»
    sobre una avenida por la que cabe un maratón.

El diagnóstico era correcto sobre el nodo equivocado. La corrección no es relajar
la restricción: es elegir bien el nodo. Aquí se ancla la salida al nodo utilizable
más cercano y, si ese nodo cuelga de un fragmento aislado, se prueban unos pocos
candidatos más alejados hasta encontrar uno conectado a red suficiente.

El coste está acotado a la fuerza: como mucho `MAX_ATTEMPTS` Dijkstras acotados, y
el caso normal —el primer candidato sirve— cuesta exactamente uno, igual que antes.
El resultado se cachea por (versión de grafo, huella del modelo) para que preflight
y generación no lo calculen dos veces.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

import numpy as np

from raceengine_contracts import ConstraintModel, RouteShape
from raceengine_routing import RoutingWeights

from .context import GraphContext
from .weights import build_weights, effective_radius_m

# Un candidato de salida vale si desde él se alcanza red suficiente para trazar
# algo. Por debajo de esto es un fragmento aislado, no una red.
MIN_REACH_NODES = 40
# Alcance a partir del cual un candidato se considera claramente bueno y no hace
# falta sondear los demás. En una malla urbana normal, el primero lo supera.
AMPLE_REACH_NODES = 1200
# Presupuesto del sondeo de candidatos, en metros. Corto a propósito: sólo tiene
# que distinguir un fragmento aislado de la malla principal, no medir el alcance
# real. El árbol completo se calcula una vez, sobre el candidato elegido.
PROBE_BUDGET_M = 2500.0
# Cuántos anclajes distintos se prueban antes de rendirse.
MAX_ATTEMPTS = 5
# Separación mínima entre candidatos: dos nodos de la misma esquina pertenecen al
# mismo fragmento, así que probar los dos no aporta información.
SPACING_M = 250.0
# Radios de búsqueda crecientes alrededor del clic.
SEARCH_RADII_M = (300.0, 900.0, 2500.0)
# Fracción del objetivo que se explora al medir alcanzabilidad. Un circuito de L
# metros no se aleja más de ~L/2 de la salida; 0,62 deja margen para el rodeo.
REACH_FRACTION = 0.62


@dataclass
class Anchored:
    """Salida y meta ancladas, con los pesos y la alcanzabilidad ya calculados."""

    start: int
    finish: int
    start_moved_m: float
    finish_moved_m: float
    weights: RoutingWeights
    diag: dict
    reach: np.ndarray            # float32[N] distancia REAL por calle desde la salida
    reach_budget_m: float
    attempts: int
    reach_nodes: int
    raw_start: int = -1          # el nodo que habría devuelto `nearest_node` a secas

    @property
    def start_relocated(self) -> bool:
        """True si hubo que apartarse del nodo más próximo al clic.

        Se compara contra el nodo crudo, no contra el número de intentos: cuando el
        nodo más próximo no es utilizable, el primer candidato que se prueba YA es
        otro, y aun así la salida se ha movido y hay que decírselo al usuario.
        """
        return self.raw_start >= 0 and self.start != self.raw_start


def usable_node_mask(g, allowed: np.ndarray, esrc: np.ndarray) -> np.ndarray:
    """bool[N] — nodos con al menos una arista admisible incidente.

    Un nodo sin ninguna arista admisible no es un punto de partida: es un cul-de-sac
    del que no se sale bajo estas restricciones.
    """
    ok = np.zeros(g.n_nodes, dtype=bool)
    if allowed.any():
        ok[esrc[allowed]] = True
        ok[g.indices[allowed]] = True
    return ok


def _candidates(g, px: float, py: float, usable: np.ndarray, raw: int) -> list[int]:
    """Nodos utilizables cercanos al clic, del más próximo al más lejano y separados."""
    out: list[int] = []
    picked: list[tuple[float, float]] = []

    def offer(i: int) -> bool:
        x, y = float(g.x[i]), float(g.y[i])
        for qx, qy in picked:
            if (x - qx) ** 2 + (y - qy) ** 2 < SPACING_M ** 2:
                return False
        out.append(int(i))
        picked.append((x, y))
        return len(out) >= MAX_ATTEMPTS

    if raw >= 0 and usable[raw] and offer(raw):
        return out

    for radius in SEARCH_RADII_M:
        idx = g.grid.within(px, py, radius)
        if idx is None or len(idx) == 0:
            continue
        idx = np.asarray(idx)
        idx = idx[usable[idx]]
        if not len(idx):
            continue
        d2 = (g.x[idx] - px) ** 2 + (g.y[idx] - py) ** 2
        for i in idx[np.argsort(d2)]:
            if offer(int(i)):
                return out
    if not out and raw >= 0:
        out.append(int(raw))
    return out


_CACHE: "OrderedDict[tuple, Anchored]" = OrderedDict()
_CACHE_MAX = 6


def anchor(ctx: GraphContext, model: ConstraintModel) -> Anchored:
    """Ancla salida y meta, construye pesos y mide alcanzabilidad. Cacheado."""
    key = (ctx.version, model.fingerprint())
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
        return hit
    res = _anchor_uncached(ctx, model)
    _CACHE[key] = res
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)
    return res


def _anchor_uncached(ctx: GraphContext, model: ConstraintModel) -> Anchored:
    g = ctx.graph
    if model.distance.is_free:
        # Sin distancia objetivo no hay de qué sacar el presupuesto de alcance. Lo
        # fija el radio EFECTIVO, que en modo libre se estira solo hasta cubrir la
        # meta y los puntos de paso: una ruta de 65 km con el presupuesto de un
        # 10K daba todos los puntos de paso por inalcanzables.
        crudo = g.nearest_node(model.start.lat, model.start.lon)
        base = effective_radius_m(g, model, crudo if crudo >= 0 else 0)
        budget = max(base * 2.5, 20_000.0)
    else:
        budget = max(model.distance.target_m * REACH_FRACTION, 400.0)
    px, py = g.project(model.start.lat, model.start.lon)

    raw = g.nearest_node(model.start.lat, model.start.lon)
    if raw < 0:
        # Grafo vacío: no hay nada que anclar. El preflight lo reporta.
        empty = np.full(g.n_nodes, np.inf, dtype=np.float32)
        w, diag = build_weights(g, model, ctx.esrc, 0)
        return Anchored(start=-1, finish=-1, start_moved_m=0.0, finish_moved_m=0.0,
                        weights=w, diag=diag, reach=empty, reach_budget_m=budget,
                        attempts=0, reach_nodes=0, raw_start=-1)

    # La máscara de utilizables se deriva de una construcción de pesos centrada en
    # el nodo crudo. El radio de búsqueda cuenta como restricción, así que un nodo
    # fuera de él tampoco es un anclaje válido.
    w0, _ = build_weights(g, model, ctx.esrc, raw)
    usable = usable_node_mask(g, w0.allowed_edges, ctx.esrc)

    # ── Selección del anclaje ────────────────────────────────────────────────
    # Se SONDEA cada candidato con un presupuesto corto y se elige el que alcanza
    # MÁS red, no el primero que supera un mínimo.
    #
    # Aceptar el primero que superara un umbral bajo era un error grave: en una
    # ciudad densa, el nodo más próximo al clic puede pertenecer a un fragmento
    # peatonal de doscientos nodos. Ese fragmento supera cualquier mínimo modesto,
    # el bucle se detenía ahí, y a partir de ese momento CUALQUIER zona obligatoria
    # —aunque estuviera a 900 m— se declaraba inalcanzable. El diagnóstico apuntaba
    # a las zonas cuando el problema era el anclaje.
    #
    # El sondeo es barato porque su presupuesto está acotado: basta para distinguir
    # un fragmento aislado de la malla principal. El árbol de caminos mínimos
    # completo se calcula UNA sola vez, ya sobre el ganador.
    probe_budget = min(budget, PROBE_BUDGET_M)
    cands = _candidates(g, px, py, usable, raw)
    scored: list[tuple[int, int, float]] = []      # (nodo, nodos sondeados, distancia)
    for node in cands:
        w_i, _ = build_weights(g, model, ctx.esrc, node)
        probe = ctx.provider.reachable(node, w_i, probe_budget)
        n_probe = int(np.isfinite(probe).sum())
        moved = float(np.hypot(float(g.x[node]) - px, float(g.y[node]) - py))
        scored.append((node, n_probe, moved))
        # Si el primer candidato ya alcanza red holgada, no hace falta sondear más.
        if n_probe >= AMPLE_REACH_NODES:
            break

    # Mayor alcance primero; a igualdad de alcance, el más cercano al clic. Un
    # candidato que alcanza casi tanta red como el mejor pero está mucho más cerca
    # del punto pedido es preferible: no conviene mover la salida por un 5%.
    best_probe = max(n for _, n, _ in scored) if scored else 0
    viable = [c for c in scored if c[1] >= max(MIN_REACH_NODES, 0.75 * best_probe)]
    node, _, moved = min(viable or scored, key=lambda c: c[2])

    w, diag = build_weights(g, model, ctx.esrc, node)
    reach = ctx.provider.reachable(node, w, budget)
    best = Anchored(
        start=int(node), finish=int(node), start_moved_m=moved, finish_moved_m=0.0,
        weights=w, diag=diag, reach=reach, reach_budget_m=budget,
        attempts=len(scored), reach_nodes=int(np.isfinite(reach).sum()),
        raw_start=int(raw),
    )

    # La meta de una ruta abierta se ancla al nodo utilizable más cercano ALCANZABLE
    # desde la salida; si ninguno lo es, al más cercano y que el preflight avise.
    if model.routetype.shape == RouteShape.OPEN and model.finish is not None:
        fx, fy = g.project(model.finish.lat, model.finish.lon)
        reachable_mask = usable & np.isfinite(best.reach)
        fnode = g.nearest_node(model.finish.lat, model.finish.lon, mask=reachable_mask)
        if fnode < 0:
            fnode = g.nearest_node(model.finish.lat, model.finish.lon, mask=usable)
        if fnode < 0:
            fnode = g.nearest_node(model.finish.lat, model.finish.lon)
        best.finish = int(fnode)
        if fnode >= 0:
            best.finish_moved_m = float(
                np.hypot(float(g.x[fnode]) - fx, float(g.y[fnode]) - fy))
    else:
        best.finish = best.start

    return best
