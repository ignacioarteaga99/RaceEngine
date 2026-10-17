"""Grafos precompilados en un almacén remoto (GitHub Releases).

Construir una región real necesita 0,6–1,6 GB de RAM (medido: Santiago Centro y
Santiago Metro), más de lo que da un servidor gratuito. Así que en despliegues
pequeños las regiones se construyen fuera —en GitHub Actions, workflow
`build-region.yml`— y se publican comprimidas junto a un `manifest.json`.

Con `RACEENGINE_GRAPHS_URL` apuntando a esa carpeta de descargas, el servidor:

  · ofrece en el selector todas las regiones del manifiesto como construidas,
    con sus cifras (nodos, cobertura…) sin tener que cargarlas en memoria;
  · descarga el grafo la primera vez que se usa y lo deja en `data/graphs/`.

El disco de un servidor gratuito se borra al reiniciarse; da igual: la descarga
son segundos (12 MB Santiago Centro, 34 MB Santiago Metro, comprimidos).

Sin la variable, nada de esto se activa y todo funciona como siempre.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import threading
import time
import urllib.request
from pathlib import Path

GRAPHS_URL = os.environ.get("RACEENGINE_GRAPHS_URL", "").rstrip("/")
_TTL_S = 600.0

_lock = threading.Lock()
_manifest: dict[str, dict] = {}
_fetched_at = 0.0
_downloading: dict[str, threading.Lock] = {}


def enabled() -> bool:
    return bool(GRAPHS_URL)


def manifest(force: bool = False) -> dict[str, dict]:
    """id → metadatos. Se refresca cada 10 min; si la red falla, el último bueno."""
    global _manifest, _fetched_at
    if not enabled():
        return {}
    with _lock:
        if not force and _manifest and time.time() - _fetched_at < _TTL_S:
            return _manifest
        try:
            with urllib.request.urlopen(f"{GRAPHS_URL}/manifest.json", timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
            _manifest = {e["id"]: e for e in data.get("regions", [])}
            _fetched_at = time.time()
            _register_specs(_manifest)
        except Exception as exc:  # noqa: BLE001 — sin red, se sigue con lo que hubiera
            print(f"[remote] no se pudo leer el manifiesto: {exc}")
            _fetched_at = time.time() - _TTL_S + 60   # reintenta en un minuto
        return _manifest


def _register_specs(entries: dict[str, dict]) -> None:
    """Las regiones a medida construidas en CI también tienen que existir aquí."""
    from .regions import CUSTOM_GROUP, REGIONS, RegionSpec

    for rid, e in entries.items():
        if rid in REGIONS or "lat" not in e:
            continue
        REGIONS[rid] = RegionSpec(
            id=rid, label=e.get("label", rid), lat=float(e["lat"]), lon=float(e["lon"]),
            radius_m=int(e.get("radius_m", 8000)), synthetic=False,
            group=e.get("group", CUSTOM_GROUP),
            corridor=tuple((float(a), float(b)) for a, b in e.get("corridor", ())),
            buffer_m=int(e.get("buffer_m", 0)),
        )


def has(region_id: str) -> bool:
    return region_id in manifest()


def fetch(region_id: str, dest: Path, progress=None) -> Path:
    """Descarga `<id>.pkl.gz`, verifica el sha256 y deja `<id>.pkl` en `dest`.

    Un cerrojo por región: dos peticiones simultáneas no descargan dos veces.
    """
    entry = manifest().get(region_id)
    if entry is None:
        raise FileNotFoundError(f"«{region_id}» no está en el almacén remoto")
    with _lock:
        lk = _downloading.setdefault(region_id, threading.Lock())
    with lk:
        if dest.exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_gz = dest.with_suffix(".pkl.gz.part")
        tmp = dest.with_suffix(".pkl.part")
        url = f"{GRAPHS_URL}/{region_id}.pkl.gz"
        print(f"[remote] descargando {url}")
        t0 = time.time()
        h = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=60) as r, tmp_gz.open("wb") as f:
            total = int(r.headers.get("Content-Length") or entry.get("size_gz") or 0)
            done = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if progress and total:
                    progress(90.0 * done / total,
                             f"descargando {done / 1e6:.0f} / {total / 1e6:.0f} MB")
        want = entry.get("sha256")
        if want and h.hexdigest() != want:
            tmp_gz.unlink(missing_ok=True)
            raise IOError(f"descarga de «{region_id}» corrupta (sha256 no coincide)")
        if progress:
            progress(92.0, "descomprimiendo")
        with gzip.open(tmp_gz, "rb") as src, tmp.open("wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
        tmp_gz.unlink(missing_ok=True)
        tmp.replace(dest)                     # atómico: nunca un .pkl a medias
        print(f"[remote] «{region_id}» lista en {time.time() - t0:.1f}s")
        return dest
