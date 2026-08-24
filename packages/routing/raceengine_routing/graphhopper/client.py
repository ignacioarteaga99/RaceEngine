"""GraphHopperProvider — adaptador al servicio GraphHopper (doc 11 §6).

Estado: implementado y utilizable, pero NO es el proveedor por defecto. Para
activarlo hace falta un GraphHopper corriendo con el grafo de la región importado:

    docker compose up graphhopper
    export RACEENGINE_ROUTING=graphhopper
    export RACEENGINE_GH_URL=http://localhost:8989

El motor no cambia: habla contra el mismo Protocol `RoutingProvider`.

Limitación conocida (riesgo GH-R1 del doc 11 §6.4): la Matrix API de GraphHopper
es de licencia comercial. Este adaptador NO la usa; la reachability se resuelve
con /spt, que sí está en el bundle Apache-2.0.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

import numpy as np

from ..base import RouteLeg, RoutingWeights


class GraphHopperError(RuntimeError):
    pass


class GraphHopperProvider:
    name = "graphhopper"

    def __init__(self, base_url: str = "http://localhost:8989", profile: str = "foot",
                 timeout: float = 20.0, custom_model: dict | None = None):
        self.base_url = base_url.rstrip("/")
        self.profile = profile
        self.timeout = timeout
        self.custom_model = custom_model

    # ── HTTP ─────────────────────────────────────────────────────────────────
    def _get(self, path: str, params: list[tuple[str, str]]) -> dict:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise GraphHopperError(f"GraphHopper no respondió ({url}): {exc}") from exc

    def _post(self, path: str, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise GraphHopperError(f"GraphHopper no respondió ({path}): {exc}") from exc

    def health(self) -> bool:
        try:
            self._get("/health", [])
            return True
        except GraphHopperError:
            return False

    # ── Primitivas de routing ────────────────────────────────────────────────
    def route_points(self, points: list[tuple[float, float]],
                     elevation: bool = True) -> dict:
        """Ruta a través de una lista de (lat, lon). Devuelve el `path` crudo."""
        body: dict = {
            "points": [[lon, lat] for lat, lon in points],
            "profile": self.profile,
            "points_encoded": False,
            "elevation": elevation,
            "instructions": False,
            "details": ["road_class", "surface"],
        }
        if self.custom_model:
            body["custom_model"] = self.custom_model
            body["ch.disable"] = True
        data = self._post("/route", body)
        paths = data.get("paths") or []
        if not paths:
            raise GraphHopperError("GraphHopper devolvió 0 rutas")
        return paths[0]

    def round_trip(self, start: tuple[float, float], distance_m: float,
                   seed: int = 0, heading: float | None = None) -> dict:
        params = [
            ("point", f"{start[0]},{start[1]}"),
            ("profile", self.profile),
            ("algorithm", "round_trip"),
            ("round_trip.distance", str(int(distance_m))),
            ("round_trip.seed", str(int(seed))),
            ("points_encoded", "false"),
            ("elevation", "true"),
            ("instructions", "false"),
        ]
        if heading is not None:
            params.append(("heading", str(int(heading))))
        data = self._get("/route", params)
        paths = data.get("paths") or []
        if not paths:
            raise GraphHopperError("round_trip devolvió 0 rutas")
        return paths[0]

    def isochrone(self, center: tuple[float, float], distance_m: float,
                  buckets: int = 1) -> dict:
        return self._get("/isochrone", [
            ("point", f"{center[0]},{center[1]}"),
            ("profile", self.profile),
            ("distance_limit", str(int(distance_m))),
            ("buckets", str(buckets)),
        ])

    # ── Protocol ─────────────────────────────────────────────────────────────
    def shortest_path(self, src, dst, w: RoutingWeights, max_distance_m=None,
                      avoid_nodes=None) -> RouteLeg | None:
        """`src`/`dst` son (lat, lon) cuando el proveedor es GraphHopper."""
        try:
            path = self.route_points([src, dst])
        except GraphHopperError:
            return None
        coords = path.get("points", {}).get("coordinates", [])
        dist = float(path.get("distance", 0.0))
        leg = RouteLeg(cost=dist, distance_m=dist, nodes=[], edges=[])
        leg.coordinates = [(c[0], c[1], c[2] if len(c) > 2 else 0.0) for c in coords]  # type: ignore[attr-defined]
        return leg

    def reachable(self, src, w: RoutingWeights, budget_m: float) -> np.ndarray:
        raise NotImplementedError(
            "Con GraphHopper la reachability se obtiene de /isochrone o /spt; "
            "el motor la solicita por ese camino."
        )
