"""Elevación: baldosas de Copernicus y muestreo bilineal (sin red)."""
from __future__ import annotations

import numpy as np
import pytest

from raceengine_geodata.elevation import _bilineal, baldosa_copernicus, make_provider


def test_baldosa_copernicus():
    assert baldosa_copernicus(-33.43, -70.61) == "Copernicus_DSM_COG_10_S34_00_W071_00_DEM"
    assert baldosa_copernicus(41.39, 2.17) == "Copernicus_DSM_COG_10_N41_00_E002_00_DEM"
    assert baldosa_copernicus(-0.5, -0.5) == "Copernicus_DSM_COG_10_S01_00_W001_00_DEM"


class _Inversa:
    """Como ~transform de rasterio para una rejilla de 1×1 con origen (0, 0)."""
    def __mul__(self, xy):
        return xy


def test_bilineal_entre_centros_de_pixel():
    band = np.array([[0.0, 10.0], [20.0, 30.0]])
    # El centro del píxel (0,0) está en (0,5; 0,5): ahí vale exactamente 0.
    assert _bilineal(band, _Inversa(), np.array([0.5]), np.array([0.5]))[0] == pytest.approx(0.0)
    # A mitad de camino entre los cuatro centros: el promedio.
    assert _bilineal(band, _Inversa(), np.array([1.0]), np.array([1.0]))[0] == pytest.approx(15.0)
    # Sólo en x: entre 0 y 10.
    assert _bilineal(band, _Inversa(), np.array([0.75]), np.array([0.5]))[0] == pytest.approx(2.5)


def test_proveedor_copernicus(tmp_path):
    p = make_provider("copernicus", tmp_path)
    assert p.name == "local_dem" and "Copernicus" in p.attribution
