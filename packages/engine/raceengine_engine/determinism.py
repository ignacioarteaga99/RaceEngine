"""Determinismo y reproducibilidad — cierra NFR-6 (doc 10 §3.2, doc 11 §7).

Las tres roturas del MVP y su corrección:
  (a) La caché de candidatos cortocircuitaba la generación con una clave que no
      incluía la versión del grafo ni del motor → aquí la clave es el fingerprint
      completo, así que servir de caché es indistinguible de recomputar.
  (b) `apply_mandatory_radius_boost` mutaba el modelo de restricciones a mitad de
      ejecución → aquí el ConstraintModel es `frozen`.
  (c) La normalización relativa dependía del reloj de pared → aquí el conjunto de
      candidatos se persiste y forma parte de la clave (doc 06 §4.2 mit. 2).
"""
from __future__ import annotations

import hashlib

import numpy as np

from raceengine_contracts import ConstraintModel

ENGINE_VERSION = "2.0.0"


def make_seed(model: ConstraintModel, graph_version: str) -> str:
    return model.fingerprint(graph_version, ENGINE_VERSION)


def seeded_rng(seed: str) -> np.random.Generator:
    """Un único stream sembrado para todos los operadores estocásticos."""
    return np.random.default_rng(int(seed[:16], 16))


def candidate_set_hash(routes) -> str:
    """Huella del conjunto de candidatos, para la reproducibilidad del scoring."""
    h = hashlib.blake2b(digest_size=8)
    for r in sorted(routes, key=lambda r: (round(r.metrics.distance_m, 2), len(r.node_path))):
        h.update(str(round(r.metrics.distance_m, 2)).encode())
        h.update(str(len(r.node_path)).encode())
    return h.hexdigest()
