import math

import pytest

from raceengine_geodata import grid


def _dist_m(a, b, c, d):
    k = math.cos(math.radians((a + c) / 2))
    return math.hypot((d - b) * k, c - a) * 111_195.0


@pytest.mark.parametrize("lat,lon", [(-33.45, -70.65), (51.5074, -0.1278), (35.68, 139.65), (0.0, 0.0),
                                     (-54.8, -68.3), (64.15, -21.94), (40.7128, -74.006), (-33.87, 151.21),
                                     (12.3, 179.99), (-12.3, -179.99)])
def test_el_punto_queda_lejos_del_borde_de_su_celda(lat, lon):
    rid = grid.celda(lat, lon)
    assert grid.es_celda(rid)
    clat, clon = grid.centro(rid)
    assert grid.RADIO_M - _dist_m(lat, lon, clat, clon) > 2000      # margen para que la ruta salga
    assert grid.celda(clat, clon) == rid                            # el centro es de la misma celda


def test_celdas_vecinas_y_ids_invalidos():
    a, b = grid.celda(-33.45, -70.65), grid.celda(-33.45, -70.55)
    assert a != b
    assert not grid.es_celda("santiago_centro")
    with pytest.raises(KeyError):
        grid.centro("g1_999999")


def test_spec_y_registro():
    from raceengine_geodata.regions import REGIONS
    rid = grid.celda(48.8566, 2.3522)
    s = grid.registrar(rid, "París, Francia")
    try:
        assert REGIONS[rid] is s and s.radius_m == grid.RADIO_M and s.group == grid.GRUPO
        assert s.label == "París, Francia"
    finally:
        REGIONS.pop(rid, None)
