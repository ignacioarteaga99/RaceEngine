"""Tipos geográficos compartidos. Todo en WGS84, distancias en metros."""
from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EARTH_R = 6371008.8  # radio medio WGS84


class GeoPoint(BaseModel):
    model_config = ConfigDict(frozen=True)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)

    def as_tuple(self) -> tuple[float, float]:
        return (self.lat, self.lon)


class GeoJSONPoint(BaseModel):
    model_config = ConfigDict(frozen=True)
    type: Literal["Point"] = "Point"
    coordinates: tuple[float, float]  # lon, lat

    @property
    def lat(self) -> float:
        return self.coordinates[1]

    @property
    def lon(self) -> float:
        return self.coordinates[0]


class GeoJSONPolygon(BaseModel):
    model_config = ConfigDict(frozen=True)
    type: Literal["Polygon"] = "Polygon"
    coordinates: list[list[tuple[float, float]]]  # anillos, lon/lat

    @property
    def exterior(self) -> list[tuple[float, float]]:
        return self.coordinates[0] if self.coordinates else []

    def centroid(self) -> tuple[float, float]:
        ring = self.exterior
        if not ring:
            return (0.0, 0.0)
        n = len(ring)
        return (sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n)


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia sobre la esfera en metros."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    y = math.sin(math.radians(lon2 - lon1)) * math.cos(math.radians(lat2))
    x = math.cos(math.radians(lat1)) * math.sin(math.radians(lat2)) - math.sin(
        math.radians(lat1)
    ) * math.cos(math.radians(lat2)) * math.cos(math.radians(lon2 - lon1))
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def angle_delta(a: float, b: float) -> float:
    """Diferencia angular mínima entre dos rumbos, en [0, 180]."""
    return abs((b - a + 180.0) % 360.0 - 180.0)


def point_in_ring(lon: float, lat: float, ring: list[tuple[float, float]]) -> bool:
    """Ray casting. `ring` es una lista de (lon, lat)."""
    if len(ring) < 3:
        return False
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat):
            xint = (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi
            if lon < xint:
                inside = not inside
        j = i
    return inside
