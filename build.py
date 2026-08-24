#!/usr/bin/env python3
"""Construye el grafo de una región. Multiplataforma, sin PYTHONPATH.

    python build.py demo
    python build.py santiago_centro
    python build.py santiago_centro --elevation local_dem --dem data/dem/santiago.tif
    python build.py --list
"""
from __future__ import annotations

import sys

from serve import bootstrap_path, check_deps, check_python  # noqa: F401
# (importar serve ya reconfigura la consola a UTF-8 en Windows)


def main() -> None:
    check_python()
    bootstrap_path()
    check_deps()

    import argparse
    import json

    from raceengine_geodata.regions import REGIONS, available_regions, build_region

    ap = argparse.ArgumentParser(prog="build.py",
                                 description="Pipeline de datos de RaceEngine")
    ap.add_argument("region", nargs="?", choices=sorted(REGIONS))
    ap.add_argument("--list", action="store_true", help="lista regiones y su estado")
    ap.add_argument("--elevation", default="auto",
                    choices=["auto", "none", "opentopodata", "local_dem"])
    ap.add_argument("--dem", default=None, help="ruta a un GeoTIFF/COG local")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    if a.list or not a.region:
        for r in available_regions():
            mark = "✓" if r["built"] else "·"
            print(f" {mark} {r['id']:<18} {r['label']:<34} "
                  f"{'construida' if r['built'] else 'no construida'}")
        if not a.region:
            print("\nUso: python build.py <region>")
        return

    build_region(a.region, elevation=a.elevation, dem_path=a.dem, force=a.force)


if __name__ == "__main__":
    sys.exit(main())
