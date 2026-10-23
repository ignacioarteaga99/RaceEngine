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
    """Muestrea uno o varios GeoTIFF/COG locales (baldosas de un DEM), con
    interpolación bilineal: el píxel más cercano mete escalones de hasta un
    metro cada 30 m, que en una calle se leen como repechos.

    Requiere `rasterio`. Para Copernicus GLO-30, ver `CopernicusProvider`.
    """

    name = "local_dem"
    attribution = "Copernicus DEM GLO-30 © ESA"

    def __init__(self, path: str | Path | list):
        paths = path if isinstance(path, (list, tuple)) else [path]
        self.paths = [Path(p) for p in paths]
        for p in self.paths:
            if not p.exists():
                raise FileNotFoundError(f"DEM no encontrado: {p}")

    def sample(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        import rasterio  # dependencia opcional

        lat = np.asarray(lat, dtype=np.float64)
        lon = np.asarray(lon, dtype=np.float64)
        vals = np.full(len(lat), np.nan, dtype=np.float64)
        for p in self.paths:
            with rasterio.open(p) as ds:
                b = ds.bounds
                dentro = (lon >= b.left) & (lon <= b.right) & (lat >= b.bottom) & (lat <= b.top) & np.isnan(vals)
                if not dentro.any():
                    continue
                band = ds.read(1).astype(np.float64)
                nodata = ds.nodata if ds.nodata is not None else -32768
                band[band == nodata] = np.nan
                vals[dentro] = _bilineal(band, ~ds.transform, lon[dentro], lat[dentro])
        if np.isnan(vals).any():
            med = float(np.nanmedian(vals)) if not np.all(np.isnan(vals)) else 0.0
            vals = np.nan_to_num(vals, nan=med)
        return vals.astype(np.float32)


def _bilineal(band: np.ndarray, inversa, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Interpolación bilineal entre los centros de los cuatro píxeles vecinos."""
    col, row = inversa * (x, y)
    col = np.asarray(col) - 0.5
    row = np.asarray(row) - 0.5
    c0 = np.clip(np.floor(col).astype(int), 0, band.shape[1] - 2)
    r0 = np.clip(np.floor(row).astype(int), 0, band.shape[0] - 2)
    fc = np.clip(col - c0, 0.0, 1.0)
    fr = np.clip(row - r0, 0.0, 1.0)
    return (band[r0, c0] * (1 - fc) * (1 - fr) + band[r0, c0 + 1] * fc * (1 - fr)
            + band[r0 + 1, c0] * (1 - fc) * fr + band[r0 + 1, c0 + 1] * fc * fr)


COPERNICUS_URL = "https://copernicus-dem-30m.s3.amazonaws.com"
COPERNICUS_ATRIBUCION = ("Copernicus DEM GLO-30 © DLR e.V. 2010-2014 y © Airbus Defence and "
                         "Space GmbH 2014-2018, provisto bajo COPERNICUS por la Unión Europea y la ESA")


def baldosa_copernicus(lat: float, lon: float) -> str:
    """Nombre de la baldosa de 1°×1° de Copernicus GLO-30 que contiene el punto
    (la esquina suroeste: -33,4 / -70,6 → S34 / W071)."""
    la, lo = int(math.floor(lat)), int(math.floor(lon))
    ns = f"{'N' if la >= 0 else 'S'}{abs(la):02d}"
    ew = f"{'E' if lo >= 0 else 'W'}{abs(lo):03d}"
    return f"Copernicus_DSM_COG_10_{ns}_00_{ew}_00_DEM"


class CopernicusProvider:
    """Copernicus DEM GLO-30 (30 m, TanDEM-X 2011–2015), de la copia pública
    en AWS: gratis, sin registro y con uso comercial permitido citando la
    fuente. Baja sólo las baldosas que tocan los puntos y las guarda en caché.

    Comparado con SRTM contra el altímetro barométrico de carreras reales en
    Santiago: ~10 % menos error de pendiente y ~35 % menos error de altura.
    """

    name = "local_dem"
    attribution = COPERNICUS_ATRIBUCION

    def __init__(self, cache_dir: Path):
        self.dir = Path(cache_dir) / "copernicus"

    def _baldosa(self, nombre: str) -> Path | None:
        import requests

        dest = self.dir / f"{nombre}.tif"
        if dest.exists():
            return dest
        self.dir.mkdir(parents=True, exist_ok=True)
        r = requests.get(f"{COPERNICUS_URL}/{nombre}/{nombre}.tif", timeout=300, stream=True)
        if r.status_code == 404:          # mar abierto: no hay baldosa
            return None
        r.raise_for_status()
        tmp = dest.with_suffix(".part")
        with open(tmp, "wb") as fh:
            for trozo in r.iter_content(1 << 20):
                fh.write(trozo)
        tmp.replace(dest)
        return dest

    def sample(self, lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
        lat = np.asarray(lat, dtype=np.float64)
        lon = np.asarray(lon, dtype=np.float64)
        esquinas = np.unique(np.column_stack([np.floor(lat), np.floor(lon)]), axis=0)
        nombres = [baldosa_copernicus(a + 0.5, o + 0.5) for a, o in esquinas]
        print(f"[elevation] Copernicus GLO-30: {len(nombres)} baldosa(s)")
        paths = [p for p in (self._baldosa(n) for n in nombres) if p is not None]
        if not paths:
            return np.zeros(len(lat), dtype=np.float32)
        return LocalDEMProvider(paths).sample(lat, lon)


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
    if kind == "copernicus":
        return CopernicusProvider(cache_dir)
    if kind == "opentopodata":
        p = OpenTopoDataProvider(cache_dir)
        p.progress = progress
        return p
    return NullElevationProvider()
