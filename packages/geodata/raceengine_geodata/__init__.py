"""raceengine_geodata — pipeline offline de datos (OSM + DEM + confianza)."""
from .graph import (
    ALL_CLASSES, CLOSURE_WEIGHT, EXTRA_CLASSES, RACE_CLASSES, GraphBuilder,
    GridIndex, RaceGraph,
    bake_elevation, compute_coverage, connectivity, merge_graphs,
)
from .regions import (
    CORRIDOR_GROUP, CUSTOM_GROUP, REGIONS, RegionSpec, add_corridor_region,
    add_custom_region, available_regions, build_region, corridor_length_km,
    load_region, region_parts, remove_custom_region,
)

__all__ = [
    "RaceGraph", "GraphBuilder", "GridIndex", "RACE_CLASSES", "EXTRA_CLASSES",
    "ALL_CLASSES", "CLOSURE_WEIGHT", "connectivity",
    "bake_elevation", "compute_coverage", "REGIONS", "RegionSpec",
    "build_region", "load_region", "available_regions", "add_custom_region",
    "remove_custom_region", "CUSTOM_GROUP", "merge_graphs", "region_parts",
    "add_corridor_region", "CORRIDOR_GROUP", "corridor_length_km",
]
