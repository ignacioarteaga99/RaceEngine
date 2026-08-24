"""Proveedores de elevación.

La auditoría (doc 10, B7 y H1) identificó dos defectos independientes en el MVP:
  · La elevación se pedía a `open-elevation.com` en tiempo de ejecución — violación
    del principio "no depender de nadie en el camino caliente" (doc 09 §1.3).
  · La elevación llegaba DESPUÉS de la selección, así que no influía en nada.

Aquí la elevación se resuelve UNA VEZ al construir la región y se hornea en las
aristas del grafo. El proveedor recomendado es un DEM local (Copernicus GLO-30,
doc 07 §3.1). Los proveedores HTTP existen solo para arrancar rápido.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Protocol

import numpy as np


class ElevationProvider(Protocol):
    name: str
    attribution: str

    def sample(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray: ...


class NullElevationProvider:
    """Sin datos de elevación. Marca la confianza como 🔴 y no inventa nada."""

    name = "null"
    attribution = ""

    def sample(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        return np.zeros(len(lat), dtype=np.float32)


class LocalDEMProvider:
    """Muestrea un GeoTIFF/COG local. Es el camino correcto para producción.

    Requiere `rasterio`. Descarga de tiles Copernicus GLO-30:
    https://dataspace.copernicus.eu (registro gratuito)
    """

    name = "local_dem"
    attribution = "Copernicus DEM GLO-30 © ESA"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"DEM no encontrado: {self.path}")

    def sample(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        import rasterio  # dependencia opcional

        with rasterio.open(self.path) as ds:
            band = ds.read(1)
            rows, cols = ds.index(lon, lat)
            rows = np.clip(np.asarray(rows), 0, band.shape[0] - 1)
            cols = np.clip(np.asarray(cols), 0, band.shape[1] - 1)
            vals = band[rows, cols].astype(np.float32)
        nodata = ds.nodata if ds.nodata is not None else -32768
        vals[vals == nodata] = np.nan
        if np.isnan(vals).any():
            med = float(np.nanmedian(vals)) if not np.all(np.isnan(vals)) else 0.0
            vals = np.nan_to_num(vals, nan=med)
        return vals


# Tope de celdas de rejilla. Por encima se afloja el paso: es preferible medir un
# corredor de 200 km con rejilla de 400 m que no medirlo.
MAX_GRID_CELLS = 160_000
MAX_GRID_STEP_M = 500.0


class OpenTopoDataProvider:
    """Muestreo por HTTP sobre REJILLA + interpolación bilineal.

    La primera versión muestreaba nodo a nodo. Con una comuna de ~100.000 nodos y
    un límite de ~1 petición/segundo eso son más de 15 minutos de espera —
    inaceptable, y además absurdo: el DEM subyacente tiene 30 m de resolución, así
    que pedir un punto por nodo es sobremuestrear un dato que no existe.

    Aquí se muestrea una rejilla regular de `grid_m` metros sobre la región y se
    interpola bilinealmente a cada nodo. Para un radio de 9 km con rejilla de 120 m
    son ~22.500 puntos = 225 peticiones ≈ 4 minutos, frente a 15+.

    Se ejecuta UNA VEZ al construir la región, nunca durante una generación.
    """

    name = "opentopodata"
    attribution = "SRTM / OpenTopoData"

    def __init__(self, cache_dir: Path, dataset: str = "srtm30m", batch: int = 100,
                 grid_m: float = 120.0):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.dataset = dataset
        self.batch = batch
        self.grid_m = grid_m
        self.progress = None      # callable(done, total, mensaje)
        self.cache_file = self.cache_dir / f"elev_{dataset}.json"
        self._cache: dict[str, float] = {}
        if self.cache_file.exists():
            try:
                self._cache = json.loads(self.cache_file.read_text())
            except Exception:  # noqa: BLE001
                self._cache = {}

    @staticmethod
    def _key(la: float, lo: float) -> str:
        return f"{la:.5f},{lo:.5f}"

    def _report(self, done: int, total: int, msg: str) -> None:
        if self.progress:
            try:
                self.progress(done, total, msg)
            except Exception:  # noqa: BLE001
                pass

    def _fetch_points(self, glat: np.ndarray, glon: np.ndarray) -> np.ndarray:
        """Consulta la rejilla, usando la caché de disco para lo ya conocido."""
        import requests

        vals = np.full(len(glat), np.nan, dtype=np.float64)
        pending: list[int] = []
        for i, (la, lo) in enumerate(zip(glat.tolist(), glon.tolist())):
            v = self._cache.get(self._key(la, lo))
            if v is None:
                pending.append(i)
            else:
                vals[i] = v

        n_batches = (len(pending) + self.batch - 1) // self.batch
        if pending:
            eta = n_batches * 1.05 / 60.0
            print(f"[elevation] rejilla de {len(glat):,} puntos · "
                  f"{len(pending):,} por consultar en {n_batches} lotes "
                  f"(~{eta:.1f} min) · {len(self._cache):,} en caché")
        self._report(0, max(n_batches, 1), "muestreando elevación")

        for bi, s in enumerate(range(0, len(pending), self.batch), start=1):
            chunk = pending[s: s + self.batch]
            locs = "|".join(f"{glat[i]:.5f},{glon[i]:.5f}" for i in chunk)
            try:
                r = requests.get(
                    f"https://api.opentopodata.org/v1/{self.dataset}",
                    params={"locations": locs}, timeout=45,
                )
                r.raise_for_status()
                for i, res in zip(chunk, r.json().get("results", [])):
                    e = res.get("elevation")
                    e = float("nan") if e is None else float(e)
                    vals[i] = e
                    if e == e:
                        self._cache[self._key(float(glat[i]), float(glon[i]))] = e
            except Exception as exc:  # noqa: BLE001
                print(f"[elevation] lote {bi}/{n_batches} interrumpido: {exc}")
                break
            self._report(bi, n_batches, f"elevación · lote {bi}/{n_batches}")
            time.sleep(1.05)  # la API pública limita a ~1 req/s
            if bi % 10 == 0:
                self.cache_file.write_text(json.dumps(self._cache))
        self.cache_file.write_text(json.dumps(self._cache))
        return vals

    def sample(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        if len(lat) == 0:
            return np.zeros(0, dtype=np.float32)

        # 1. Rejilla regular que cubre la nube de nodos, con un margen.
        lat0, lat1 = float(lat.min()), float(lat.max())
        lon0, lon1 = float(lon.min()), float(lon.max())
        cos_lat = max(math.cos(math.radians((lat0 + lat1) / 2)), 0.1)

        # El paso se ADAPTA al tamaño de la región. Con paso fijo de 120 m, un
        # corredor de 200 km —una marcha cicloturista— da una caja envolvente de
        # dos millones de puntos de rejilla: veinte mil peticiones y varias horas.
        # Se afloja el paso hasta que el número de celdas cabe en el tope, en
        # peldaños deterministas para que dos construcciones de la misma región
        # midan en los mismos sitios.
        step = self.grid_m
        while True:
            dlat = step / 111_320.0
            dlon = step / (111_320.0 * cos_lat)
            n_lat = int((lat1 - lat0) / dlat) + 3
            n_lon = int((lon1 - lon0) / dlon) + 3
            if n_lat * n_lon <= MAX_GRID_CELLS or step >= MAX_GRID_STEP_M:
                break
            step *= 1.5
        if step > self.grid_m:
            print(f"[elevation] región grande: rejilla a {step:.0f} m "
                  f"en vez de {self.grid_m:.0f} m ({n_lat * n_lon:,} celdas)")

        lats = np.arange(lat0 - dlat, lat1 + 2 * dlat, dlat)
        lons = np.arange(lon0 - dlon, lon1 + 2 * dlon, dlon)
        gy, gx = np.meshgrid(lats, lons, indexing="ij")

        # Sólo se piden las celdas que tienen red debajo. Una región circular
        # desperdicia un 21 % de su caja envolvente; un corredor a lo largo de una
        # carretera desperdicia el 95 %, y ahí la diferencia es entre horas y
        # minutos. Se guarda una corona de una celda alrededor para que la
        # interpolación bilineal tenga siempre las cuatro esquinas.
        fi = np.clip(((lat - lats[0]) / dlat).astype(int), 0, len(lats) - 2)
        fj = np.clip(((lon - lons[0]) / dlon).astype(int), 0, len(lons) - 2)
        need = np.zeros((len(lats), len(lons)), dtype=bool)
        for di in (0, 1):
            for dj in (0, 1):
                need[np.clip(fi + di, 0, len(lats) - 1),
                     np.clip(fj + dj, 0, len(lons) - 1)] = True
        idx = np.nonzero(need.ravel())[0]
        total = need.size
        print(f"[elevation] {len(idx):,} celdas con red de {total:,} de la caja "
              f"({100.0 * len(idx) / max(total, 1):.0f} %)")

        vals = np.full(need.size, np.nan, dtype=np.float64)
        vals[idx] = self._fetch_points(gy.ravel()[idx], gx.ravel()[idx])
        vals = vals.reshape(gy.shape)
        if np.all(np.isnan(vals)):
            print("[elevation] no se obtuvo ningún dato; se deja sin elevación")
            return np.zeros(len(lat), dtype=np.float32)
        vals = np.nan_to_num(vals, nan=float(np.nanmedian(vals)))

        # 2. Interpolación bilineal de la rejilla a cada nodo.
        fi = np.clip((lat - lats[0]) / dlat, 0, len(lats) - 1.001)
        fj = np.clip((lon - lons[0]) / dlon, 0, len(lons) - 1.001)
        i0, j0 = fi.astype(int), fj.astype(int)
        ti, tj = fi - i0, fj - j0
        out = (
            vals[i0, j0] * (1 - ti) * (1 - tj)
            + vals[i0 + 1, j0] * ti * (1 - tj)
            + vals[i0, j0 + 1] * (1 - ti) * tj
            + vals[i0 + 1, j0 + 1] * ti * tj
        )
        self._report(1, 1, "elevación interpolada")
        return out.astype(np.float32)


def make_provider(kind: str, cache_dir: Path, dem_path: str | None = None,
                  progress=None) -> ElevationProvider:
    if kind == "local_dem" and dem_path:
        return LocalDEMProvider(dem_path)
    if kind == "opentopodata":
        p = OpenTopoDataProvider(cache_dir)
        p.progress = progress
        return p
    return NullElevationProvider()
