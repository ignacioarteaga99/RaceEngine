#!/usr/bin/env python3
"""Construye una región y la prepara para publicarla (lo usa build-region.yml).

    python scripts/ci_region.py build  <id> [--label L --lat X --lon Y --radius-m R]
                                           [--elevation auto|none|opentopodata]
    python scripts/ci_region.py smoke  <id>      # genera un 10K y mide la RAM
    python scripts/ci_region.py merge  <manifest.json> <entrada.json>

`build` deja en dist/: <id>.pkl.gz y <id>.entry.json (la fila del manifiesto).
`smoke` comprueba que el grafo sirve para generar y cuánta memoria pico usa:
es el dato que dice si cabe en un servidor de 512 MB.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import resource
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for rel in ("packages/contracts", "packages/routing", "packages/geodata",
            "packages/engine"):
    sys.path.insert(0, str(ROOT / rel))

DIST = ROOT / "dist"


def peak_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # Linux: KiB


def cmd_build(a: argparse.Namespace) -> None:
    from raceengine_geodata.regions import REGIONS, add_custom_region, build_region, graph_path

    if a.region not in REGIONS:
        if a.lat is None or a.lon is None:
            sys.exit(f"«{a.region}» no está en el catálogo: pasa --label, --lat y --lon")
        add_custom_region(a.region, a.label or a.region, a.lat, a.lon, a.radius_m)
    spec = REGIONS[a.region]
    t0 = time.time()
    g = build_region(a.region, elevation=a.elevation, force=True)
    raw = graph_path(a.region).read_bytes()
    gz = gzip.compress(raw, 6)
    DIST.mkdir(exist_ok=True)
    (DIST / f"{a.region}.pkl.gz").write_bytes(gz)
    entry = {
        "id": spec.id, "label": spec.label, "lat": spec.lat, "lon": spec.lon,
        "radius_m": spec.radius_m, "group": spec.group,
        "corridor": [list(p) for p in spec.corridor], "buffer_m": spec.buffer_m,
        "nodes": int(g.n_nodes), "edges": int(g.n_edges),
        "coverage": g.coverage, "version": g.version or spec.id,
        "elevation": bool(float(g.elev.max() - g.elev.min()) > 1.0),
        "size": len(raw), "size_gz": len(gz),
        "sha256": hashlib.sha256(gz).hexdigest(),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "build_s": round(time.time() - t0, 1), "build_peak_mb": round(peak_mb()),
    }
    (DIST / f"{a.region}.entry.json").write_text(
        json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{spec.id}: {g.n_nodes:,} nodos · {len(raw) / 1e6:.1f} MB "
          f"({len(gz) / 1e6:.1f} MB gzip) · pico {peak_mb():.0f} MB")


def cmd_smoke(a: argparse.Namespace) -> None:
    from raceengine_contracts import ConstraintModel, DistanceLayer, GeoPoint
    from raceengine_engine import generate, get_context
    from raceengine_geodata.regions import REGIONS

    spec = REGIONS[a.region]
    t0 = time.time()
    get_context(a.region)
    after_load = peak_mb()
    r = generate(ConstraintModel(
        region=a.region, start=GeoPoint(lat=spec.lat, lon=spec.lon),
        distance=DistanceLayer(target_m=10_000, tolerance_pct=2.0),
        fast_budget_ms=3000, total_budget_ms=15_000))
    best = r.recommended
    dist = f"{best.metrics.distance_m / 1000:.2f} km" if best else f"sin ruta ({r.status})"
    print(f"{a.region}: RAM tras cargar {after_load:.0f} MB · pico generando un 10K "
          f"{peak_mb():.0f} MB · {dist} · {time.time() - t0:.1f}s")
    entry = DIST / f"{a.region}.entry.json"
    if entry.exists():
        e = json.loads(entry.read_text(encoding="utf-8"))
        e["serve_peak_mb"] = round(peak_mb())
        entry.write_text(json.dumps(e, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_merge(a: argparse.Namespace) -> None:
    path = Path(a.manifest)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    rows = {e["id"]: e for e in data.get("regions", [])}
    new = json.loads(Path(a.entry).read_text(encoding="utf-8"))
    rows[new["id"]] = new
    path.write_text(json.dumps({"regions": sorted(rows.values(), key=lambda e: e["id"])},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"manifiesto: {len(rows)} regiones")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("region")
    b.add_argument("--label")
    b.add_argument("--lat", type=float)
    b.add_argument("--lon", type=float)
    b.add_argument("--radius-m", type=int, default=8000)
    b.add_argument("--elevation", default="auto")
    s = sub.add_parser("smoke")
    s.add_argument("region")
    m = sub.add_parser("merge")
    m.add_argument("manifest")
    m.add_argument("entry")
    a = ap.parse_args()
    {"build": cmd_build, "smoke": cmd_smoke, "merge": cmd_merge}[a.cmd](a)


if __name__ == "__main__":
    main()
