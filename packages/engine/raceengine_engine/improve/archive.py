"""Archivo de Pareto con diversidad espacial — doc 06 §7.

El MVP deduplicaba por igualdad EXACTA de firma de aristas: dos rutas que difieren
en una sola arista contaban como distintas, que es justo el defecto que BR-B
declara ("near-duplicate routes are a defect"). Aquí se aplica la regla aprobada:
dos rutas seleccionadas comparten como máximo el 70% de su longitud.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from raceengine_contracts import Indices, RouteCandidate


@dataclass
class ArchiveEntry:
    route: RouteCandidate
    edge_set: frozenset[int]
    ring: list[int] = field(default_factory=list)


def _overlap(a: frozenset[int], b: frozenset[int]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _dominates(x: Indices, y: Indices, fx: float, fy: float) -> bool:
    """x domina a y si no es peor en nada y es mejor en algo (5 objetivos)."""
    xs = (fx, x.speed, x.logistics, x.scenery, x.difficulty)
    ys = (fy, y.speed, y.logistics, y.scenery, y.difficulty)
    return all(a >= b - 1e-9 for a, b in zip(xs, ys)) and any(a > b + 1e-9 for a, b in zip(xs, ys))


class ParetoArchive:
    def __init__(self, max_overlap: float = 0.70, capacity: int = 24):
        self.max_overlap = max_overlap
        self.capacity = capacity
        self.entries: list[ArchiveEntry] = []

    def __len__(self) -> int:
        return len(self.entries)

    @property
    def routes(self) -> list[RouteCandidate]:
        return [e.route for e in self.entries]

    def offer(self, route: RouteCandidate, ring: list[int] | None = None) -> bool:
        """Intenta añadir. Devuelve True si entró en el archivo."""
        eset = frozenset(route.node_path)
        entry = ArchiveEntry(route=route, edge_set=eset, ring=list(ring or []))

        # 1. Descartar si está dominado por alguien del frente.
        for e in self.entries:
            if _dominates(e.route.indices, route.indices,
                          e.route.normalized.fit, route.normalized.fit):
                return False

        # 2. Retirar a los que este candidato domina.
        self.entries = [
            e for e in self.entries
            if not _dominates(route.indices, e.route.indices,
                              route.normalized.fit, e.route.normalized.fit)
        ]

        # 3. Diversidad espacial: si solapa demasiado con uno existente,
        #    solo lo sustituye si es mejor.
        for i, e in enumerate(self.entries):
            if _overlap(eset, e.edge_set) > self.max_overlap:
                if route.global_score > e.route.global_score:
                    self.entries[i] = entry
                    return True
                return False

        self.entries.append(entry)
        self.entries.sort(key=lambda e: -e.route.global_score)
        if len(self.entries) > self.capacity:
            self.entries = self.entries[: self.capacity]
        return True

    def best(self) -> RouteCandidate | None:
        return self.entries[0].route if self.entries else None

    def diverse(self, n: int) -> list[RouteCandidate]:
        """Selección diversa: mayor score primero, respetando el solape máximo."""
        chosen: list[ArchiveEntry] = []
        for e in sorted(self.entries, key=lambda x: -x.route.global_score):
            if all(_overlap(e.edge_set, c.edge_set) <= self.max_overlap for c in chosen):
                chosen.append(e)
            if len(chosen) >= n:
                break
        return [c.route for c in chosen]
