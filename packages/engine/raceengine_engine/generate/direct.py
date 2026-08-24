"""Ruta directa: de aquí a allí, pasando por ahí.

Qué problema resuelve
─────────────────────
El motor nació para DISEÑAR un recorrido: «dame 42,195 km que cumplan esto». Esa
es la tarea de un organizador de carreras, y la distancia es su promesa central.

Pero hay una petición completamente distinta que el mismo motor debería atender y
no atendía: **«llévame de San Vicente a Rapel pasando por el embalse, y hazlo lo
más llano posible»**. Ahí no hay distancia objetivo. La distancia es *consecuencia*
de la ruta, no una restricción sobre ella, y perseguirla no restringe nada: obliga
al motor a inventar rodeos para rellenar kilómetros que nadie pidió.

Con `distance.mode == "free"` el pipeline cambia entero. No hay generación de
circuitos, no hay ajuste de distancia, no hay banda de tolerancia. Hay encadenado
de caminos mínimos bajo los pesos que dicte el OBJETIVO, que es lo que convierte
«más llana», «más corta» o «menos tráfico» en rutas realmente distintas.

El orden de los puntos de paso
──────────────────────────────
Si se marcan tres sitios por los que hay que pasar, el orden en que se pinchan no
es el orden en que conviene recorrerlos. Aquí se resuelve el orden ÓPTIMO, que es
un viajante de comercio con extremos fijos:

  · hasta 8 puntos, programación dinámica exacta (Held-Karp): 2⁸·8² operaciones,
    invisible al lado de una sola consulta de ruta;
  · más allá, vecino más próximo y luego 2-opt hasta que no mejore.

La matriz de distancias se calcula con árboles de caminos mínimos —uno por punto—
en vez de con distancias en línea recta. Cuesta más y es lo correcto: en montaña,
dos puntos a dos kilómetros en línea recta pueden estar a treinta por carretera, y
un orden decidido sobre la recta manda a la ruta a cruzar un valle que no existe.
"""
from __future__ import annotations

import itertools
import uuid

import numpy as np

from raceengine_contracts import ConstraintModel, RouteShape

from ..context import GraphContext
from .loop_generator import RawLoop, route_chain

# Por encima de esto, el exacto deja de caber y se pasa a heurística.
EXACT_ORDER_MAX = 8
# Un enlace entre dos puntos de paso no puede medir más que esto respecto a la
# recta que los separa. Sin cota, un punto inalcanzable arrastra a A* por todo el
# grafo antes de rendirse.
LEG_SLACK = 6.0
LEG_FLOOR_M = 3000.0


def _reach_matrix(ctx: GraphContext, w, nodos: list[int]) -> np.ndarray:
    """Distancias por CARRETERA entre cada par, no en línea recta.

    La diferencia importa justo donde se usa esto: en montaña, dos puntos a dos
    kilómetros en línea recta pueden estar a treinta por carretera.
    """
    n = len(nodos)
    d = np.full((n, n), np.inf, dtype=np.float64)
    for i, a in enumerate(nodos):
        arbol = ctx.provider.reachable(a, w, budget_m=1e9)
        for j, b in enumerate(nodos):
            if i != j:
                d[i, j] = float(arbol[b])
        d[i, i] = 0.0
    return d


def _order_exact(d: np.ndarray) -> list[int]:
    """Held-Karp con extremos fijos: 0 es la salida y n−1 la meta."""
    n = d.shape[0]
    medio = list(range(1, n - 1))
    if not medio:
        return [0, n - 1]
    m = len(medio)
    INF = float("inf")
    # dp[mascara][ultimo] = coste mínimo visitando `mascara` y acabando en `ultimo`
    dp = [[INF] * m for _ in range(1 << m)]
    prev = [[-1] * m for _ in range(1 << m)]
    for i in range(m):
        dp[1 << i][i] = d[0, medio[i]]
    for mask in range(1 << m):
        for last in range(m):
            c = dp[mask][last]
            if c == INF or not (mask >> last) & 1:
                continue
            for nxt in range(m):
                if (mask >> nxt) & 1:
                    continue
                nm = mask | (1 << nxt)
                cand = c + d[medio[last], medio[nxt]]
                if cand < dp[nm][nxt]:
                    dp[nm][nxt] = cand
                    prev[nm][nxt] = last
    full = (1 << m) - 1
    mejor, fin = INF, -1
    for last in range(m):
        cand = dp[full][last] + d[medio[last], n - 1]
        if cand < mejor:
            mejor, fin = cand, last
    if fin < 0:
        return [0] + medio + [n - 1]
    orden, mask, last = [], full, fin
    while last >= 0:
        orden.append(medio[last])
        p = prev[mask][last]
        mask ^= (1 << last)
        last = p
    return [0] + orden[::-1] + [n - 1]


def _order_heuristic(d: np.ndarray) -> list[int]:
    """Vecino más próximo + 2-opt. Para cuando el exacto ya no cabe."""
    n = d.shape[0]
    libres = set(range(1, n - 1))
    orden = [0]
    while libres:
        act = orden[-1]
        sig = min(libres, key=lambda j: d[act, j])
        orden.append(sig)
        libres.discard(sig)
    orden.append(n - 1)

    def coste(o: list[int]) -> float:
        return float(sum(d[a, b] for a, b in zip(o, o[1:])))

    mejor = coste(orden)
    cambiado = True
    while cambiado:
        cambiado = False
        for i in range(1, len(orden) - 2):
            for j in range(i + 1, len(orden) - 1):
                nueva = orden[:i] + orden[i:j + 1][::-1] + orden[j + 1:]
                c = coste(nueva)
                if c < mejor - 1e-6:
                    orden, mejor, cambiado = nueva, c, True
    return orden


def waypoint_order(ctx: GraphContext, w, nodos: list[int]) -> list[int]:
    """Orden en que conviene recorrer los puntos, con extremos fijos."""
    if len(nodos) <= 3:
        return list(range(len(nodos)))
    d = _reach_matrix(ctx, w, nodos)
    if not np.isfinite(d).all():
        # Algún punto no se alcanza: se respeta el orden en que se marcaron y que
        # el preflight explique cuál falla. Reordenar sobre infinitos no informa.
        return list(range(len(nodos)))
    if len(nodos) - 2 <= EXACT_ORDER_MAX:
        return _order_exact(d)
    return _order_heuristic(d)


def direct_routes(ctx: GraphContext, model: ConstraintModel, anc,
                  masks: dict, variantes: int = 3) -> list[RawLoop]:
    """Ruta lógica de la salida a la meta pasando por las zonas obligatorias.

    Devuelve la mejor y unas cuantas alternativas, obtenidas penalizando las
    aristas ya usadas: es la forma barata de ofrecer «y si no, por aquí».
    """
    g = ctx.graph
    if anc.start < 0:
        return []
    fin = anc.finish if model.routetype.shape == RouteShape.OPEN else anc.start
    if fin < 0:
        fin = anc.start

    # Un punto de paso se representa por el nodo utilizable más cercano a su
    # centro. Basta: la zona es un círculo y tocarla en su centro la cubre.
    paso: list[int] = []
    for z in model.mandatory_zones:
        mask = masks.get(z.id or z.name)
        if mask is None or not mask.any():
            continue
        idx = np.nonzero(mask)[0]
        alc = anc.reach[idx]
        ok = np.isfinite(alc)
        if not ok.any():
            continue
        paso.append(int(idx[ok][int(np.argmin(alc[ok]))]))

    nodos = [int(anc.start)] + paso + [int(fin)]
    orden = waypoint_order(ctx, anc.weights, nodos)
    cadena = [nodos[i] for i in orden]
    # Un circuito vuelve al principio; una ruta punto a punto no.
    if model.routetype.shape != RouteShape.OPEN and cadena[-1] != cadena[0]:
        cadena.append(cadena[0])

    recta = 0.0
    for a, b in zip(cadena, cadena[1:]):
        recta += float(np.hypot(g.x[b] - g.x[a], g.y[b] - g.y[a]))
    cap = max(LEG_FLOOR_M, recta * LEG_SLACK)

    salidas: list[RawLoop] = []
    vistas: set[tuple] = set()
    penal: dict[int, float] = {}
    for k in range(max(1, variantes)):
        reuso = 0.0 if model.routetype.allow_segment_reuse else 0.4
        loop = route_chain(ctx, anc.weights, cadena, reuse_penalty=reuso,
                           max_leg_cost=cap, penalty0=penal)
        if loop is None and k == 0:
            # La cota existe para que A* no se pasee por medio grafo cuando un
            # punto es inalcanzable. Pero cuando SÍ es alcanzable y el camino da
            # un rodeo largo —bordear un embalse, cruzar un río por el único
            # puente— la cota se lo come y el usuario recibe «no hay ruta» sobre
            # algo que existe. Devolver nada es peor que tardar un poco más.
            loop = route_chain(ctx, anc.weights, cadena, reuse_penalty=reuso,
                               max_leg_cost=None, penalty0=penal)
        if loop is None:
            break
        firma = tuple(loop.edges)
        if firma in vistas:
            # La penalización no consiguió mover la ruta: no hay una alternativa
            # de verdad, y repetir la misma tres veces sería enseñar una opción
            # disfrazada de tres.
            break
        vistas.add(firma)
        loop.generator = "direct" if not salidas else f"direct_alt{len(salidas)}"
        salidas.append(loop)
        # Para la siguiente variante, lo ya recorrido cuesta más. Con un 35 % la
        # alternativa se parece a la buena sin ser la misma; con más, se va por
        # cualquier sitio con tal de no repetir.
        for e in loop.edges:
            penal[e] = penal.get(e, 0.0) + 0.35 * float(g.elen[e])
    return salidas
