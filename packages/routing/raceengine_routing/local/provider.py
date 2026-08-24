"""LocalRoutingProvider — A* y Dijkstra acotado sobre el CSR del RaceGraph.

Diferencias deliberadas con el A* del MVP (doc 10 §2, B1/B2):

  MVP:  heapq.heappush(pq, (f, g, u, path + [v], edges + [e]))
        → copia la lista completa del camino en CADA relajación: O(L·E).
        → además, cuando f y g empatan, Python compara las LISTAS dentro del heap.

  Aquí: came_from / came_edge como arrays int32, reconstrucción única al final.
        El heap solo contiene (f, contador, nodo). Coste O(E log V).

  MVP:  edge_allowed() ejecutaba 2+ haversine por arista relajada.
  Aquí: `w.allowed_edges[e]` es un acceso a un bool precalculado.
"""
from __future__ import annotations

import heapq

import numpy as np

from raceengine_geodata import RaceGraph

from ..base import RouteLeg, RoutingWeights

_INF = np.float32(np.inf)


class LocalRoutingProvider:
    name = "local"

    def __init__(self, graph: RaceGraph):
        self.g = graph
        n = graph.n_nodes
        # Buffers reutilizables: evitan reasignar arrays grandes en cada llamada.
        self._dist = np.full(n, np.inf, dtype=np.float64)
        self._came_from = np.full(n, -1, dtype=np.int32)
        self._came_edge = np.full(n, -1, dtype=np.int32)
        self._stamp = np.zeros(n, dtype=np.int32)
        self._epoch = 0

    # ── utilidades ────────────────────────────────────────────────────────────
    def _reset(self) -> None:
        self._epoch += 1

    def _seen(self, node: int) -> bool:
        return self._stamp[node] == self._epoch

    def _touch(self, node: int) -> None:
        self._stamp[node] = self._epoch

    def _reconstruct(self, src: int, dst: int, w: RoutingWeights) -> RouteLeg:
        nodes: list[int] = []
        edges: list[int] = []
        cur = dst
        while cur != -1 and cur != src:
            nodes.append(int(cur))
            e = int(self._came_edge[cur])
            edges.append(e)
            cur = int(self._came_from[cur])
        nodes.append(int(src))
        nodes.reverse()
        edges.reverse()
        dist_m = float(self.g.elen[edges].sum()) if edges else 0.0
        return RouteLeg(cost=float(self._dist[dst]), distance_m=dist_m,
                        nodes=nodes, edges=edges)

    # ── A* ────────────────────────────────────────────────────────────────────
    def shortest_path(self, src: int, dst: int, w: RoutingWeights,
                      max_distance_m: float | None = None,
                      avoid_nodes: np.ndarray | None = None) -> RouteLeg | None:
        # La cota llega en metros; el coste acumulado está ponderado. Convertir
        # con el multiplicador MÁXIMO garantiza que nunca se descarta un camino
        # que sí cabe en la distancia pedida.
        max_cost = None if max_distance_m is None else max_distance_m * max(w.max_ratio, 1.0)
        if src == dst:
            return RouteLeg(cost=0.0, distance_m=0.0, nodes=[src], edges=[])

        g = self.g
        indptr, indices = g.indptr, g.indices
        weight, allowed = w.weight, w.allowed_edges
        penalty = w.penalty
        gx, gy = g.x, g.y
        tx, ty = gx[dst], gy[dst]
        ratio = w.min_ratio  # mantiene la heurística admisible

        self._reset()
        dist = self._dist
        came_from, came_edge = self._came_from, self._came_edge

        dist[src] = 0.0
        came_from[src] = -1
        came_edge[src] = -1
        self._touch(src)

        def h(node: int) -> float:
            dx = gx[node] - tx
            dy = gy[node] - ty
            return float(np.sqrt(dx * dx + dy * dy)) * ratio

        counter = 0
        pq: list[tuple[float, int, int]] = [(h(src), 0, src)]
        closed = set()

        while pq:
            f, _, u = heapq.heappop(pq)
            if u in closed:
                continue
            if u == dst:
                return self._reconstruct(src, dst, w)
            closed.add(u)
            du = dist[u]

            for e in range(indptr[u], indptr[u + 1]):
                if not allowed[e]:
                    continue
                v = int(indices[e])
                if v in closed:
                    continue
                if avoid_nodes is not None and avoid_nodes[v] and v != dst:
                    continue
                nd = du + float(weight[e])
                pen = penalty.get(e)
                if pen:
                    nd += pen
                if max_cost is not None and nd > max_cost:
                    continue
                if not self._seen(v) or nd < dist[v]:
                    self._touch(v)
                    dist[v] = nd
                    came_from[v] = u
                    came_edge[v] = e
                    counter += 1
                    heapq.heappush(pq, (nd + h(v), counter, v))

        return None

    # ── Dijkstra acotado (reachability / SPT) ─────────────────────────────────
    def reachable(self, src: int, w: RoutingWeights, budget_m: float) -> np.ndarray:
        """Distancia REAL por calle desde `src`, acotada a `budget_m`.

        Sustituye a `within_radius` del MVP, que asumía que la distancia en línea
        recta predice la distancia por calle. Con un río, un cerro o una autopista
        de por medio, no lo hace.
        """
        g = self.g
        indptr, indices, elen = g.indptr, g.indices, g.elen
        allowed = w.allowed_edges

        out = np.full(g.n_nodes, np.inf, dtype=np.float32)
        out[src] = 0.0
        pq: list[tuple[float, int]] = [(0.0, src)]
        while pq:
            d, u = heapq.heappop(pq)
            if d > out[u]:
                continue
            for e in range(indptr[u], indptr[u + 1]):
                if not allowed[e]:
                    continue
                v = int(indices[e])
                nd = d + float(elen[e])
                if nd <= budget_m and nd < out[v]:
                    out[v] = nd
                    heapq.heappush(pq, (nd, v))
        return out
