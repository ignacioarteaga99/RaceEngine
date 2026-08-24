"""CLI del pipeline de datos:  python -m raceengine_geodata.cli build <region>"""
from __future__ import annotations

import argparse
import json

from .regions import REGIONS, available_regions, build_region


def main() -> None:
    p = argparse.ArgumentParser(prog="raceengine-geodata",
                                description="Pipeline de datos de RaceEngine")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="Construye el grafo de una región")
    b.add_argument("region", choices=sorted(REGIONS))
    b.add_argument("--elevation", default="auto",
                   choices=["auto", "none", "opentopodata", "local_dem"])
    b.add_argument("--dem", default=None, help="Ruta a un GeoTIFF/COG local")
    b.add_argument("--force", action="store_true")

    sub.add_parser("list", help="Lista las regiones y su estado")

    a = p.parse_args()
    if a.cmd == "list":
        print(json.dumps(available_regions(), indent=2, ensure_ascii=False))
        return
    kind = "none" if a.elevation == "none" else a.elevation
    build_region(a.region, elevation=kind, dem_path=a.dem, force=a.force)


if __name__ == "__main__":
    main()
