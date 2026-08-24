"""RoutingProvider — el sustrato de routing es reemplazable (doc 09 AD-3 / P5).

El motor NUNCA importa un motor de routing concreto: habla contra este Protocol.
Hoy hay dos implementaciones:
  · LocalRoutingProvider   — A* sobre CSR en numpy. Cero infraestructura.
  · GraphHopperProvider    — delega en un servicio GraphHopper (grafo contraído).

Los tests de contrato (`tests/test_routing_contract.py`) corren idénticos contra
ambos, que es lo que hace la sustitución segura.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np


@dataclass
class RouteLeg:
    """Un tramo calculado entre dos nodos."""

    cost: float                      # coste ponderado (no metros)
    distance_m: float                # metros reales
    nodes: list[int] = field(default_factory=list)
    edges: list[int] = field(default_factory=list)   # índices de arista en CSR

    @property
    def ok(self) -> bool:
        return len(self.nodes) > 1


@dataclass
class RoutingWeights:
    """Modelo de coste por petición.

    Es el equivalente Python de un *custom model* de GraphHopper: expresa las
    restricciones blandas de doc 04 de forma declarativa, en vez de con
    multiplicadores mágicos dentro del bucle de A* como hacía el MVP.
    """

    allowed_edges: np.ndarray                 # bool[E]
    weight: np.ndarray                        # float32[E] coste ponderado
    min_ratio: float = 1.0                    # min(weight/length) — heurística admisible
    max_ratio: float = 1.0                    # max(weight/length) — conversión metros→coste
    penalty: dict[int, float] = field(default_factory=dict)   # penalización temporal


@runtime_checkable
class RoutingProvider(Protocol):
    name: str

    def shortest_path(self, src: int, dst: int, w: RoutingWeights,
                      max_distance_m: float | None = None,
                      avoid_nodes: np.ndarray | None = None) -> RouteLeg | None:
        """Camino de coste mínimo.

        `max_distance_m` es una cota en METROS, no en coste ponderado. El
        proveedor la convierte internamente usando `w.max_ratio`. Mezclarlas fue
        un bug real: con `prefer_flatter` una arista cuesta hasta 2,5× su
        longitud, así que tramos válidos se rechazaban por exceder una cota que
        estaba expresada en la unidad equivocada — y las zonas obligatorias
        cuesta arriba se quedaban sin cubrir.
        """

    def reachable(self, src: int, w: RoutingWeights, budget_m: float) -> np.ndarray:
        """Árbol de caminos mínimos acotado. Devuelve float32[N] de distancias
        (inf donde es inalcanzable). Sustituye al radio euclídeo del MVP."""
