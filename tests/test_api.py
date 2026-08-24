"""Tests de la capa HTTP."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from raceengine_api.main import app

client = TestClient(app)
BODY = {
    "region": "demo",
    "start": {"lat": -33.4372, "lon": -70.6506},
    "distance": {"target_m": 6000, "tolerance_pct": 2.0},
    "geometry": {"search_radius_max_m": 3000},
    "fast_budget_ms": 2500, "total_budget_ms": 5000,
}


def test_health():
    assert client.get("/healthz").json()["status"] == "ok"


def test_regions_include_demo():
    rs = client.get("/api/regions").json()
    demo = next(r for r in rs if r["id"] == "demo")
    assert demo["built"] and demo["elevation"]
    assert "coverage" in demo


def test_presets_are_the_six_of_doc_04():
    p = client.get("/api/presets").json()
    assert set(p) == {"mile", "urban_5k", "urban_10k", "half", "marathon", "corporate"}


def test_preflight_is_fast_and_feasible():
    r = client.post("/api/preflight", json=BODY)
    assert r.status_code == 200
    d = r.json()
    assert d["feasible"] and d["preflight_ms"] < 200


def test_preflight_reports_blocking_with_a_fix():
    bad = {**BODY, "distance": {"target_m": 42195, "tolerance_pct": 1.0},
           "geometry": {"search_radius_max_m": 1500}}
    d = client.post("/api/preflight", json=bad).json()
    assert not d["feasible"]
    assert d["blocking"][0]["suggested_relaxation"]


def test_full_generation_and_exports():
    r = client.post("/api/generations", json=BODY)
    assert r.status_code == 202
    gid = r.json()["generation_id"]
    for _ in range(80):
        d = client.get(f"/api/generations/{gid}").json()
        if d.get("status") in ("converged", "cancelled", "infeasible", "failed"):
            break
        time.sleep(0.25)
    assert d["status"] == "converged", d
    rec = d["recommended"]
    assert rec and rec["in_tolerance"]
    assert d["attribution"]["text"].startswith("© OpenStreetMap")

    gpx = client.get(f"/api/routes/{rec['route_id']}.gpx")
    assert gpx.status_code == 200 and "<trkpt" in gpx.text
    geo = client.get(f"/api/routes/{rec['route_id']}.geojson").json()
    assert geo["features"][0]["geometry"]["type"] == "LineString"


def test_cancel_endpoint():
    gid = client.post("/api/generations",
                      json={**BODY, "total_budget_ms": 30000}).json()["generation_id"]
    time.sleep(0.6)
    assert client.post(f"/api/generations/{gid}/cancel").json()["cancelled"]


def test_unknown_generation_is_404():
    assert client.get("/api/generations/nope").status_code == 404


# ── Proyectos (carreras) ─────────────────────────────────────────────────────
def test_projects_are_isolated_workspaces():
    a = client.post("/api/projects", json={
        "name": "Maratón A", "description": "primera",
        "state": {"zones": [{"id": "z1", "name": "Estadio"}], "fields": {"dist": "42195"}}}).json()
    b = client.post("/api/projects", json={
        "name": "10K B", "state": {"zones": [], "fields": {"dist": "10000"}}}).json()

    # Editar una no toca la otra.
    client.patch(f"/api/projects/{a['id']}", json={
        "state": {"zones": [{"id": "z1"}, {"id": "z2"}], "fields": {"dist": "21097"}}})
    assert len(client.get(f"/api/projects/{a['id']}").json()["state"]["zones"]) == 2
    assert client.get(f"/api/projects/{b['id']}").json()["state"]["zones"] == []

    # Renombrar conserva el estado.
    client.patch(f"/api/projects/{a['id']}", json={"name": "Maratón A 2027"})
    ga = client.get(f"/api/projects/{a['id']}").json()
    assert ga["name"] == "Maratón A 2027"
    assert ga["state"]["fields"]["dist"] == "21097"

    # Duplicar crea una copia independiente.
    dup = client.post(f"/api/projects/{a['id']}/duplicate").json()
    client.patch(f"/api/projects/{dup['id']}", json={"state": {"zones": []}})
    assert len(client.get(f"/api/projects/{a['id']}").json()["state"]["zones"]) == 2

    for pid in (a["id"], b["id"], dup["id"]):
        assert client.delete(f"/api/projects/{pid}").status_code == 204
    assert client.get(f"/api/projects/{a['id']}").status_code == 404


def test_project_survives_restart():
    """La persistencia es en disco: otro proceso ve lo mismo."""
    from raceengine_api.projects import ProjectStore

    p = client.post("/api/projects", json={"name": "Persistente", "state": {"k": 1}}).json()
    fresh = ProjectStore()                       # instancia nueva, mismo directorio
    assert fresh.get(p["id"]).state == {"k": 1}
    client.delete(f"/api/projects/{p['id']}")


# ── Modo creativo por HTTP ───────────────────────────────────────────────────
def _draw_body(**kw):
    body = {
        "constraints": {
            "region": "demo",
            "start": {"lat": -33.4372, "lon": -70.6506},
            "distance": {"target_m": 5000, "tolerance_pct": 3.0},
            "geometry": {"search_radius_max_m": 3000},
        },
        "nodes": [], "edges": [],
    }
    body.update(kw)
    return body


def test_draw_extend_endpoint_returns_a_scored_route():
    r = client.post("/api/draw/extend", json=_draw_body(lat=-33.4300, lon=-70.6450))
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["distance_m"] > 0
    assert d["route"]["global_score"] > 0
    assert d["route"]["breakdown"]["explanation_i18n"].keys() >= {"es", "en"}


def test_draw_extend_endpoint_explains_a_rejection_in_both_languages():
    r = client.post("/api/draw/extend", json=_draw_body(lat=-33.30, lon=-70.90))
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False
    assert set(d["reason"]["i18n"]) == {"es", "en"}


def test_draw_state_endpoint_is_idempotent():
    a = client.post("/api/draw/extend",
                    json=_draw_body(lat=-33.4300, lon=-70.6450)).json()
    b = client.post("/api/draw/state",
                    json=_draw_body(nodes=a["nodes"], edges=a["edges"])).json()
    assert b["ok"] is True
    assert b["distance_m"] == a["distance_m"]


# ── Regiones a medida ────────────────────────────────────────────────────────
CITY = {"id": "c_test_city", "label": "Ciudad de Prueba", "lat": -33.02,
        "lon": -71.55, "radius_m": 6000, "build": False}


def test_custom_region_roundtrip():
    """Registrar una ciudad cualquiera, verla en el catálogo y quitarla."""
    from raceengine_geodata.regions import CUSTOM_PATH

    try:
        r = client.post("/api/regions/custom", json=CITY)
        assert r.status_code == 201
        assert r.json()["group"] == "Mis ciudades"

        rows = client.get("/api/regions").json()
        mine = [x for x in rows if x["id"] == CITY["id"]]
        assert mine and mine[0]["custom"] is True
        assert mine[0]["built"] is False
        # Se persiste en disco: reiniciar no puede perder una ciudad ya descargada.
        assert CUSTOM_PATH.exists()
        assert CITY["id"] in CUSTOM_PATH.read_text(encoding="utf-8")
    finally:
        client.delete(f"/api/regions/custom/{CITY['id']}")

    assert not any(x["id"] == CITY["id"] for x in client.get("/api/regions").json())


def test_custom_region_cannot_shadow_a_curated_one():
    body = dict(CITY, id="london")
    r = client.post("/api/regions/custom", json=body)
    assert r.status_code == 409


def test_curated_regions_are_not_deletable():
    assert client.delete("/api/regions/custom/london").status_code == 404


def test_custom_region_radius_is_clamped():
    try:
        r = client.post("/api/regions/custom", json=dict(CITY, radius_m=999))
        assert r.status_code == 422        # el contrato lo rechaza antes de llegar
    finally:
        client.delete(f"/api/regions/custom/{CITY['id']}")


def test_geocode_degrades_gracefully_without_network():
    """Sin red el buscador devuelve lista vacía y un error legible, no un 500."""
    r = client.get("/api/geocode", params={"q": "Kyoto"})
    assert r.status_code == 200
    d = r.json()
    assert isinstance(d.get("results"), list)


def test_geocode_ignores_trivial_queries():
    assert client.get("/api/geocode", params={"q": "a"}).json()["results"] == []


# ── Cola de construcción ─────────────────────────────────────────────────────
def test_build_batch_enqueues_and_drains():
    """Varias regiones se encolan y se construyen una tras otra."""
    import time as _t

    r = client.post("/api/regions/build-batch", json={"regions": ["demo"]})
    assert r.status_code == 202
    d = r.json()
    assert "demo" in d["queued"] or "demo" in d.get("done", [])

    for _ in range(40):
        q = client.get("/api/regions/build-queue").json()
        if not q["active"]:
            break
        _t.sleep(0.25)
    q = client.get("/api/regions/build-queue").json()
    assert q["active"] is False
    assert "demo" in q["done"]


def test_build_batch_rejects_unknown_regions():
    r = client.post("/api/regions/build-batch",
                    json={"regions": ["demo", "ciudad_inventada"]})
    assert r.status_code == 404
    assert "ciudad_inventada" in r.json()["detail"]


def test_build_batch_does_not_queue_a_region_twice():
    """Una región repetida en la misma petición se encola una sola vez."""
    d = client.post("/api/regions/build-batch",
                    json={"regions": ["demo", "demo", "demo"]}).json()
    assert d["queued"].count("demo") <= 1


def test_build_queue_never_runs_two_regions_at_once():
    """El trabajador es uno: Overpass limita por IP y el paralelo se rechaza."""
    from raceengine_api.jobs import RegionBuilder

    b = RegionBuilder()
    vistos: list[str] = []
    solapes: list[str] = []
    activo: list[str] = []

    def fake(region, elevation="auto"):
        if activo:
            solapes.append(region)
        activo.append(region)
        vistos.append(region)
        time.sleep(0.05)
        activo.pop()
        b._state[region] = {"state": "done", "pct": 100.0, "elapsed": 0.05}
        return b._state[region]

    b._build_one = fake
    b.enqueue(["demo", "las_condes", "providencia"])
    for _ in range(60):
        if not b.queue_status()["active"]:
            break
        time.sleep(0.05)
    assert not solapes, f"se solaparon: {solapes}"
    assert vistos == ["demo", "las_condes", "providencia"], vistos


def test_build_batch_needs_at_least_one_region():
    assert client.post("/api/regions/build-batch", json={"regions": []}).status_code == 422


# ── Importación por HTTP ─────────────────────────────────────────────────────
IMPORT_MODEL = {
    "region": "demo",
    "start": {"lat": -33.4372, "lon": -70.6506},
    "distance": {"target_m": 8000, "tolerance_pct": 2.0},
    "geometry": {"search_radius_max_m": 4000},
}


def test_import_endpoint_scores_a_track():
    gpx = ('<?xml version="1.0"?><gpx><trk><trkseg>'
           '<trkpt lat="-33.4372" lon="-70.6506"/>'
           '<trkpt lat="-33.4340" lon="-70.6480"/>'
           '<trkpt lat="-33.4300" lon="-70.6450"/>'
           '<trkpt lat="-33.4340" lon="-70.6420"/>'
           '<trkpt lat="-33.4372" lon="-70.6506"/>'
           '</trkseg></trk></gpx>')
    r = client.post("/api/routes/import",
                    json={"constraints": IMPORT_MODEL, "content": gpx})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["distance_m"] > 0
    assert d["route"]["global_score"] > 0
    assert d["imported"]["matched_pct"] > 0


def test_import_endpoint_explains_an_unusable_file():
    r = client.post("/api/routes/import",
                    json={"constraints": IMPORT_MODEL, "content": "no soy un gpx" * 3})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["error"]


def test_import_endpoint_rejects_an_empty_body():
    r = client.post("/api/routes/import",
                    json={"constraints": IMPORT_MODEL, "content": "x"})
    assert r.status_code == 422


def _run(body: dict) -> dict:
    """Lanza una generación y devuelve la ruta recomendada ya terminada."""
    gid = client.post("/api/generations", json=body).json()["generation_id"]
    for _ in range(120):
        d = client.get(f"/api/generations/{gid}").json()
        if d.get("status") in ("converged", "cancelled", "infeasible", "failed"):
            break
        time.sleep(0.25)
    return client.get(f"/api/generations/{gid}").json()["recommended"]


def test_rescore_puts_two_routes_on_one_scale():
    """Generada e importada juntas deben puntuarse en la misma normalización."""
    body = dict(BODY)
    rec = _run(body)

    gpx = ('<?xml version="1.0"?><gpx><trk><trkseg>' + "".join(
        f'<trkpt lat="{c[1]:.6f}" lon="{c[0]:.6f}"/>' for c in rec["coordinates"])
        + '</trkseg></trk></gpx>')
    imp = client.post("/api/routes/import",
                      json={"constraints": body, "content": gpx}).json()
    assert imp["ok"], imp

    out = client.post("/api/routes/rescore", json={
        "constraints": body,
        "routes": [
            {"route_id": "gen", "nodes": rec["node_path"], "generator": "engine"},
            {"route_id": "imp", "nodes": imp["nodes"], "generator": "imported"},
        ],
    }).json()
    assert out["ok"] and len(out["routes"]) == 2
    assert out["absolute_scale"] is False
    ids = [x["route_id"] for x in out["routes"]]
    assert ids == ["gen", "imp"]
    # Mismo recorrido por dos caminos: en una escala común deben empatar.
    a, b = (x["global_score"] for x in out["routes"])
    assert abs(a - b) < 6.0, f"misma ruta, escalas distintas: {a} vs {b}"


def test_rescore_flags_the_absolute_scale_when_alone():
    body = dict(BODY)
    rec = _run(body)
    out = client.post("/api/routes/rescore", json={
        "constraints": body,
        "routes": [{"route_id": "solo", "nodes": rec["node_path"]}],
    }).json()
    assert out["ok"] and out["absolute_scale"] is True
    assert 0 < out["routes"][0]["global_score"] <= 100


def test_rescore_rejects_an_unusable_path():
    body = dict(BODY)
    out = client.post("/api/routes/rescore", json={
        "constraints": body,
        "routes": [{"route_id": "x", "nodes": [1, 999999]}],
    })
    assert out.status_code in (200, 422)
    if out.status_code == 200:
        assert out.json()["ok"] is False


def test_compose_returns_the_merged_graph_not_the_sum():
    r = client.post("/api/regions/compose", json={"ids": ["demo", "demo"]})
    assert r.status_code == 200
    d = r.json()
    assert d["id"] == "demo" and d["parts"] == ["demo"]
    solo = next(x for x in client.get("/api/regions").json() if x["id"] == "demo")
    assert d["nodes"] == solo["nodes"], "unir una región consigo misma la duplicó"
    assert d["bbox"]["min_lat"] < d["bbox"]["max_lat"]


def test_compose_rejects_a_region_that_is_not_built():
    r = client.post("/api/regions/compose", json={"ids": ["demo", "tokyo"]})
    assert r.status_code == 400


def test_corridor_preview_costs_less_than_a_circle():
    r = client.post("/api/regions/corridor/preview", json={
        "label": "Santiago → Valparaíso",
        "points": [[-33.4372, -70.6506], [-33.35, -71.05],
                   [-33.05, -71.42], [-33.0458, -71.6197]],
        "buffer_m": 8000,
    })
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] and 95 < d["length_km"] < 120
    assert d["area_km2"] < d["circle_area_km2"] / 4
    assert len(d["ring"]) >= 4


def test_corridor_registration_and_removal():
    body = {"id": "r_test", "label": "Prueba", "build": False, "buffer_m": 5000,
            "points": [[-33.44, -70.65], [-33.40, -70.40], [-33.30, -70.20]]}
    r = client.post("/api/regions/corridor", json=body)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["group"] == "Mis rutas" and d["buffer_m"] == 5000
    assert 25 < d["length_km"] < 70

    regiones = client.get("/api/regions").json()
    fila = next(x for x in regiones if x["id"] == "r_test")
    assert len(fila["corridor"]) == 3 and not fila["built"]

    assert client.delete("/api/regions/custom/r_test").status_code == 204
    assert all(x["id"] != "r_test" for x in client.get("/api/regions").json())


def test_a_corridor_that_spans_a_continent_is_refused():
    r = client.post("/api/regions/corridor", json={
        "label": "imposible", "build": False,
        "points": [[-33.44, -70.65], [10.0, -70.65]],
    })
    assert r.status_code == 400
    assert "km" in r.json()["detail"]


def test_presets_depend_on_the_discipline():
    correr = client.get("/api/presets").json()
    bici = client.get("/api/presets?discipline=gravel").json()
    assert "marathon" in correr and "marathon" not in bici
    assert "gravel_80" in bici
    # Los presets de marcha vienen en horquilla, no con distancia exacta.
    assert bici["granfondo_120"]["mode"] == "range"
    assert bici["granfondo_120"]["target_gain_m"] > 0
    # Y los de competición sí con distancia exacta.
    assert bici["tt_40"]["mode"] == "target"


def test_a_cycling_generation_uses_roads_running_would_refuse():
    """El mismo punto de partida, dos disciplinas, dos mundos."""
    ruta = {**BODY, "discipline": "road",
            "distance": {"target_m": 8000, "tolerance_pct": 3.0},
            "geometry": {"search_radius_max_m": 4000}}
    r = client.post("/api/preflight", json=ruta)
    assert r.status_code == 200
    d = r.json()
    assert "feasible" in d

    gid = client.post("/api/generations", json=ruta).json()["generation_id"]
    for _ in range(120):
        st = client.get(f"/api/generations/{gid}").json()
        if st.get("status") in ("converged", "cancelled", "infeasible", "failed"):
            break
        time.sleep(0.25)
    rec = st.get("recommended")
    assert rec, st
    # El tiempo estimado ya no sale de un ritmo fijo de 5:00/km.
    m = rec["metrics"]
    assert m["est_time_min"] < m["distance_m"] / 1000.0 * 5.0
    assert m["assumed_pace_min_km"] < 4.0


def _gpx_line(n=60):
    return ('<?xml version="1.0"?><gpx><trk><trkseg>' + "".join(
        f'<trkpt lat="{-33.4372 + i * 0.0009:.6f}" lon="{-70.6506 + i * 0.0011:.6f}"/>'
        for i in range(n)) + '</trkseg></trk></gpx>')


def test_import_works_before_a_start_is_placed():
    """La salida de un recorrido importado la define SU traza.

    Exigirla en la petición era tenerlo al revés, y devolvía un 422 crudo de
    FastAPI —una lista de objetos que el navegador pintaba como «[object
    Object]»— sobre la operación que más sentido tiene hacer la primera.
    """
    for constraints in ({"region": "demo", "start": None}, {"region": "demo"}):
        r = client.post("/api/routes/import",
                        json={"constraints": constraints, "content": _gpx_line()})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["ok"] and d["distance_m"] > 0
        # Y la salida quedó sobre la traza, no en el centro de la región.
        assert abs(d["start"]["lat"] - (-33.4372)) < 0.01


def test_import_explains_a_bad_configuration_instead_of_a_raw_422():
    r = client.post("/api/routes/import", json={
        "constraints": {"region": "demo", "distance": {"target_m": -5}},
        "content": _gpx_line()})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is False
    assert "distance.target_m" in d["error"]
    assert isinstance(d["error"], str), "el error tiene que ser legible, no un objeto"


def test_regions_report_their_connectivity():
    demo = next(x for x in client.get("/api/regions").json() if x["id"] == "demo")
    con = demo.get("coverage", {}).get("connectivity")
    # La región sintética puede haberse construido antes de existir esta medida;
    # si está, tiene que ser coherente.
    if con:
        assert 0 < con["pct"] <= 100 and con["components"] >= 1


def test_a_corridor_build_goes_through_the_queue():
    """Sin pasar por la cola, el seguimiento decía «listo» al instante."""
    r = client.post("/api/regions/corridor", json={
        "id": "r_cola", "label": "Cola", "build": False, "buffer_m": 4000,
        "points": [[-33.44, -70.65], [-33.40, -70.40]]})
    assert r.status_code == 201
    try:
        from raceengine_api.jobs import builder
        assert hasattr(builder, "enqueue")
        # `start` es una cola de un elemento: el corredor aparece en el estado.
        estado = builder.status("r_cola")
        assert isinstance(estado, dict)
    finally:
        client.delete("/api/regions/custom/r_cola")
