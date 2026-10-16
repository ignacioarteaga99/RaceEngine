"""RaceEngine API — FastAPI (doc 11 §5).

Endpoints:
    GET  /api/regions                      regiones disponibles + cobertura de datos
    GET  /api/presets                      los seis presets de doc 04 §5
    POST /api/preflight                    factibilidad en < 100 ms (FR-3.5)
    POST /api/generations                  crea la generación (202)
    GET  /api/generations/{id}             estado + incumbente actual
    GET  /api/generations/{id}/stream      SSE con las mejoras progresivas
    POST /api/generations/{id}/cancel      cancelación cooperativa (NFR-1.4)
    GET  /api/routes/{route_id}.gpx        export GPX (FR-10.1)
    GET  /api/routes/{route_id}.geojson    export GeoJSON
    GET  /healthz
"""
from __future__ import annotations

import json
import math
import os
import queue
import time
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from raceengine_contracts import (
    CYCLING, CYCLING_PRESETS, OFFROAD_FOOT, PRESETS, TRAIL_PRESETS,
    ConstraintModel,
)
from raceengine_engine import ENGINE_VERSION, get_context, run_preflight, warm
from raceengine_engine.draw import draw_extend, draw_state
from raceengine_geodata import available_regions

from .jobs import builder, manager
from .projects import Project, ProjectIn, ProjectMeta, ProjectPatch, store

WEB_DIR = Path(__file__).resolve().parents[3] / "web"

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Pre-calienta el GraphContext de cada región al arrancar el worker.

    Es la diferencia entre un servicio persistente y el CLI del MVP, que
    reconstruía índices y grafo en cada ejecución (doc 10 §2, B9).
    """
    regions = [r.strip() for r in os.environ.get("RACEENGINE_WARM", "demo").split(",")
               if r.strip()]
    if regions:
        print(f"[api] precalentando regiones: {', '.join(regions)}")
        warm(regions)
    yield


app = FastAPI(
    title="RaceEngine API",
    version=ENGINE_VERSION,
    description="Motor de diseño de recorridos deportivos óptimos — Engine V2",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


# ── Estáticos ────────────────────────────────────────────────────────────────
VENDOR_DIR = WEB_DIR / "vendor"
VENDOR_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/vendor", StaticFiles(directory=VENDOR_DIR), name="vendor")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/m", include_in_schema=False)
@app.get("/mobile", include_in_schema=False)
def mobile() -> FileResponse:
    """Interfaz móvil (iPhone). Se genera con `python build_mobile.py`."""
    return FileResponse(WEB_DIR / "mobile.html")


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"status": "ok", "engine": ENGINE_VERSION}


# ── Metadatos ────────────────────────────────────────────────────────────────
@app.get("/api/regions")
def regions() -> list[dict]:
    out = []
    for r in available_regions():
        entry = dict(r)
        if r["built"]:
            try:
                ctx = get_context(r["id"])
                entry["coverage"] = ctx.graph.coverage
                entry["nodes"] = int(ctx.graph.n_nodes)
                entry["edges"] = int(ctx.graph.n_edges)
                entry["elevation"] = ctx.elevation_available()
                entry["version"] = ctx.version
            except Exception as exc:  # noqa: BLE001
                entry["error"] = str(exc)
        out.append(entry)
    return out


class ComposeIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=12)


@app.post("/api/regions/compose")
def compose_regions(body: ComposeIn) -> dict:
    """Une varias regiones ya construidas en un solo grafo de trabajo.

    Una comuna no es una unidad deportiva: un recorrido de Santiago cruza Las
    Condes, Providencia y Ñuñoa sin enterarse. Con una sola cargada, el motor no
    evita las otras por criterio — es que no existen para él, y el usuario no
    tiene forma de saberlo mirando el resultado.

    Devuelve las cifras del grafo YA unido, no la suma de las partes: en las
    costuras hay nodos compartidos, y decir «187.402 nodos» cuando son 181.9xx
    sería inventar precisión.
    """
    from raceengine_geodata import region_parts

    partes = region_parts("+".join(body.ids))
    rid = "+".join(partes)
    try:
        ctx = get_context(rid)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    g = ctx.graph
    lat, lon = float(g.lat.mean()), float(g.lon.mean())
    return {
        "id": rid, "parts": partes,
        "nodes": int(g.n_nodes), "edges": int(g.n_edges),
        "coverage": g.coverage, "elevation": ctx.elevation_available(),
        "center": {"lat": lat, "lon": lon},
        "bbox": {"min_lat": float(g.lat.min()), "max_lat": float(g.lat.max()),
                 "min_lon": float(g.lon.min()), "max_lon": float(g.lon.max())},
    }


@app.post("/api/regions/{region_id}/build", status_code=202)
def build_region_endpoint(region_id: str, elevation: str = "auto") -> dict:
    """Construye el grafo de una región en segundo plano (sin usar la terminal)."""
    from raceengine_geodata.regions import REGIONS

    if region_id not in REGIONS:
        raise HTTPException(status_code=404, detail=f"Región desconocida: {region_id}")
    return builder.start(region_id, elevation)


@app.get("/api/regions/{region_id}/build")
def build_status(region_id: str) -> dict:
    return builder.status(region_id)


class BuildBatchIn(BaseModel):
    regions: list[str] = Field(min_length=1, max_length=40)
    elevation: str = "auto"


@app.post("/api/regions/build-batch", status_code=202)
def build_batch(data: BuildBatchIn) -> dict:
    """Encola varias regiones y las construye una tras otra.

    La cola vive en el servidor, no en el navegador: descargar seis comunas son
    veinte minutos y no tiene sentido obligar a dejar la pestaña abierta.
    """
    from raceengine_geodata.regions import REGIONS

    desconocidas = [r for r in data.regions if r not in REGIONS]
    if desconocidas:
        raise HTTPException(status_code=404,
                            detail=f"Regiones desconocidas: {', '.join(desconocidas)}")
    return builder.enqueue(list(data.regions), data.elevation)


@app.get("/api/regions/build-queue")
def build_queue() -> dict:
    return builder.queue_status()


@app.get("/api/geocode")
def geocode(q: str, limit: int = 8, any_kind: bool = False) -> dict:
    """Busca un lugar por nombre. Cualquier sitio, no solo los del catálogo.

    `any_kind=true` levanta el filtro de clases: para crear una región se quiere
    un lugar habitado, pero para navegar hasta un cerro o un embalse no.
    """
    from raceengine_geodata.geocode import search

    try:
        return {"results": search(q, limit=limit, any_kind=any_kind)}
    except Exception as exc:  # noqa: BLE001 — sin red, la app sigue siendo usable
        return {"results": [], "error": str(exc)}


class CustomRegionIn(BaseModel):
    """Una ciudad elegida por el usuario, no del catálogo curado."""

    id: str | None = None
    label: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    radius_m: int = Field(default=8000, ge=1500, le=30000)
    elevation: str = "auto"
    build: bool = True


@app.post("/api/regions/custom", status_code=201)
def create_custom_region(data: CustomRegionIn) -> dict:
    """Registra la región y arranca su descarga. Idempotente por id."""
    from raceengine_geodata.geocode import slugify
    from raceengine_geodata.regions import REGIONS, add_custom_region

    rid = data.id or f"c_{slugify(data.label)}"
    if rid in REGIONS and REGIONS[rid].group != "Mis ciudades":
        raise HTTPException(
            status_code=409,
            detail=f"«{rid}» ya existe en el catálogo. Elígela en el desplegable.")
    spec = add_custom_region(rid, data.label, data.lat, data.lon, data.radius_m)
    out = {"id": spec.id, "label": spec.label, "lat": spec.lat, "lon": spec.lon,
           "radius_m": spec.radius_m, "group": spec.group, "custom": True}
    if data.build:
        out["job"] = builder.start(spec.id, data.elevation)
    return out


class CorridorIn(BaseModel):
    """Franja a lo largo de un trayecto, para rutas que no caben en una ciudad."""

    id: str = ""
    label: str
    points: list[tuple[float, float]] = Field(min_length=2, max_length=60)
    buffer_m: int = Field(default=6000, ge=1000, le=25_000)
    elevation: str = "auto"
    build: bool = True


@app.post("/api/regions/corridor", status_code=201)
def create_corridor_region(data: CorridorIn) -> dict:
    """Registra un corredor y arranca su descarga.

    Una ruta de bici de 150 km no cabe en «un círculo alrededor de un punto»: el
    círculo que la contuviera tendría 75 km de radio y cientos de megas de OSM,
    casi todos de sitios por los que la ruta no pasa. Aquí se declara el trayecto
    y se descarga sólo su franja, con recorte poligonal en el propio Overpass.
    """
    from raceengine_geodata.geocode import slugify
    from raceengine_geodata.regions import (
        CORRIDOR_GROUP, REGIONS, add_corridor_region, corridor_length_km,
    )

    rid = data.id or f"r_{slugify(data.label)}"
    if rid in REGIONS and REGIONS[rid].group != CORRIDOR_GROUP:
        raise HTTPException(
            status_code=409,
            detail=f"«{rid}» ya existe en el catálogo. Elige otro nombre.")
    try:
        spec = add_corridor_region(rid, data.label, data.points, data.buffer_m)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    out = {"id": spec.id, "label": spec.label, "lat": spec.lat, "lon": spec.lon,
           "radius_m": spec.radius_m, "group": spec.group, "custom": True,
           "corridor": [list(p) for p in spec.corridor], "buffer_m": spec.buffer_m,
           "length_km": round(corridor_length_km(spec.corridor), 1)}
    if data.build:
        out["job"] = builder.start(spec.id, data.elevation)
    return out


@app.post("/api/regions/corridor/preview")
def preview_corridor(data: CorridorIn) -> dict:
    """Geometría y tamaño de la franja SIN descargar nada.

    Sirve para que el usuario vea la franja sobre el mapa y sepa lo que va a
    costar antes de pedirla. Una descarga de veinte minutos no debería ser una
    sorpresa.
    """
    from raceengine_geodata.osm import corridor_area_km2, corridor_polygon
    from raceengine_geodata.regions import (
        MAX_CORRIDOR_AREA_KM2, MAX_CORRIDOR_KM, corridor_length_km,
    )

    ring = corridor_polygon([tuple(p) for p in data.points], data.buffer_m)
    largo = corridor_length_km(data.points)
    area = corridor_area_km2(ring)
    # Un círculo que contuviera el trayecto entero, para poder comparar.
    circulo = math.pi * (largo / 2.0 + data.buffer_m / 1000.0) ** 2
    return {
        "ring": [list(p) for p in ring],
        "length_km": round(largo, 1),
        "area_km2": round(area, 1),
        "circle_area_km2": round(circulo, 1),
        "max_length_km": MAX_CORRIDOR_KM,
        "max_area_km2": MAX_CORRIDOR_AREA_KM2,
        "ok": largo <= MAX_CORRIDOR_KM and area <= MAX_CORRIDOR_AREA_KM2,
    }


@app.delete("/api/regions/custom/{region_id}", status_code=204)
def delete_custom_region(region_id: str) -> Response:
    from raceengine_geodata.regions import remove_custom_region

    if not remove_custom_region(region_id):
        raise HTTPException(status_code=404,
                            detail="No es una región propia o no existe")
    return Response(status_code=204)


@app.get("/api/presets")
def presets(discipline: str = "running") -> dict:
    """Presets de la disciplina pedida.

    Se filtra por disciplina en vez de devolverlo todo junto porque un 10K y una
    marcha de 180 km no compiten por el mismo hueco de la interfaz: enseñarlos a
    la vez obliga al usuario a descartar la mitad de la lista en cada visita.
    """
    if discipline in CYCLING:
        return dict(CYCLING_PRESETS)
    if discipline in OFFROAD_FOOT:
        return dict(TRAIL_PRESETS)
    return dict(PRESETS)


# ── Preflight ────────────────────────────────────────────────────────────────
@app.post("/api/preflight")
def preflight(model: ConstraintModel) -> dict:
    t0 = time.perf_counter()
    try:
        ctx = get_context(model.region)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    report = run_preflight(ctx, model)
    return {
        "feasible": report.feasible,
        "blocking": [b.model_dump() for b in report.blocking],
        "warnings": list(report.warnings),
        "preflight_ms": report.preflight_ms,
        "total_ms": int((time.perf_counter() - t0) * 1000),
    }


# ── Generación ───────────────────────────────────────────────────────────────
@app.post("/api/generations", status_code=202)
def create_generation(model: ConstraintModel) -> dict:
    try:
        get_context(model.region)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job = manager.submit(model)
    return {
        "generation_id": job.id,
        "stream_url": f"/api/generations/{job.id}/stream",
        "status_url": f"/api/generations/{job.id}",
    }


@app.get("/api/generations/{gid}")
def get_generation(gid: str) -> dict:
    job = manager.get(gid)
    if not job:
        raise HTTPException(status_code=404, detail="Generación no encontrada")
    if job.error:
        return {"generation_id": gid, "status": "failed", "detail": job.error}
    if job.result is None:
        return {"generation_id": gid, "status": "running", "progress": 0.0}
    return job.result.model_dump(mode="json")


@app.post("/api/generations/{gid}/cancel")
def cancel_generation(gid: str) -> dict:
    if not manager.cancel(gid):
        raise HTTPException(status_code=404, detail="Generación no encontrada")
    return {"generation_id": gid, "cancelled": True}


@app.get("/api/generations/{gid}/stream")
def stream_generation(gid: str) -> StreamingResponse:
    job = manager.get(gid)
    if not job:
        raise HTTPException(status_code=404, detail="Generación no encontrada")

    def gen():
        yield "retry: 2000\n\n"
        last_ping = time.time()
        while True:
            try:
                event, payload = job.events.get(timeout=1.0)
            except queue.Empty:
                if job.finished.is_set():
                    break
                if time.time() - last_ping > 8:
                    last_ping = time.time()
                    yield ": ping\n\n"
                continue
            if event == "close":
                break
            yield f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield "event: close\ndata: {}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


# ── Modo creativo (trazado manual) ───────────────────────────────────────────
class DrawRequest(BaseModel):
    """Estado del trazado, que vive en el cliente; el servidor es sin estado.

    Que el cliente sea el dueño del estado hace que deshacer sea trivial (recorta
    su propia lista y vuelve a pedir evaluación) y que dos pestañas no se pisen.
    """

    constraints: ConstraintModel
    nodes: list[int] = Field(default_factory=list)
    edges: list[int] = Field(default_factory=list)
    lat: float | None = None
    lon: float | None = None
    close: bool = False


@app.post("/api/draw/extend")
def draw_extend_endpoint(req: DrawRequest) -> dict:
    """Añade un tramo al trazado manual, o explica por qué no se puede."""
    try:
        ctx = get_context(req.constraints.region)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return draw_extend(ctx, req.constraints, req.nodes, req.edges,
                       req.lat, req.lon, close=req.close)


class ImportRequest(BaseModel):
    """Traza a importar. `content` es el fichero en texto: GPX o GeoJSON.

    `constraints` viaja como diccionario y NO como `ConstraintModel`, y es a
    propósito. El modelo exige una salida, pero en una importación **la salida la
    define la traza**: pedirla en la petición es tenerlo al revés. Con el modelo
    tipado aquí, importar antes de pinchar nada devolvía un 422 crudo de FastAPI
    —una lista de objetos que el navegador pintaba como «[object Object]»— sobre
    la operación que precisamente tiene sentido hacer la primera.
    """

    constraints: dict
    content: str = Field(min_length=10, max_length=20_000_000)
    name: str = ""


@app.post("/api/routes/import")
def import_route(req: ImportRequest) -> dict:
    """Engancha un recorrido existente a la red y lo puntúa con el mismo motor."""
    from raceengine_engine.importer import match_track, parse_track

    try:
        ctx = get_context(str((req.constraints or {}).get("region") or "demo"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        pts = parse_track(req.content)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not pts:
        return {"ok": False, "error": "La traza no contiene puntos."}

    # La salida sale de la traza si no viene una utilizable. `match_track` la
    # reancla después sobre el primer nodo enganchado; esto sólo es para que el
    # modelo pueda construirse.
    datos = dict(req.constraints or {})
    if not isinstance(datos.get("start"), dict):
        datos["start"] = {"lat": pts[0][0], "lon": pts[0][1]}
    try:
        modelo = ConstraintModel(**datos)
    except ValidationError as exc:
        primero = exc.errors()[0] if exc.errors() else {}
        campo = ".".join(str(x) for x in primero.get("loc", ()))
        return {"ok": False,
                "error": f"Configuración no válida en «{campo}»: "
                         f"{primero.get('msg', 'revisa los ajustes')}."}
    return match_track(ctx, modelo, pts)


class RescoreRoute(BaseModel):
    route_id: str = ""
    nodes: list[int] = Field(default_factory=list, max_length=400_000)
    generator: str = ""


class RescoreRequest(BaseModel):
    constraints: ConstraintModel
    routes: list[RescoreRoute] = Field(min_length=1, max_length=12)


@app.post("/api/routes/rescore")
def rescore_routes(req: RescoreRequest) -> dict:
    """Puntúa varios recorridos en UNA sola normalización.

    Es la regla de doc 06 §4.2 llevada al caso en el que conviven una ruta
    generada y un recorrido importado: si se enseñan juntos, se puntúan juntos.
    Antes cada uno traía su escala y el «61 contra 74» no comparaba nada.
    """
    from raceengine_engine.compare import score_set

    try:
        ctx = get_context(req.constraints.region)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    rutas = score_set(ctx, req.constraints,
                      [r.model_dump() for r in req.routes])
    return {
        "ok": bool(rutas),
        "routes": [r.model_dump(mode="json") for r in rutas],
        # Con un solo recorrido la cota relativa colapsa y se usan referencias
        # sintéticas: la interfaz debe decir que la escala no es comparable.
        "absolute_scale": len(rutas) < 2,
    }


@app.post("/api/draw/state")
def draw_state_endpoint(req: DrawRequest) -> dict:
    """Reevalúa un trazado existente (tras deshacer, o al cambiar restricciones)."""
    try:
        ctx = get_context(req.constraints.region)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    out = draw_state(ctx, req.constraints, req.nodes, req.edges, closed=req.close)
    out["ok"] = True
    return out


# ── Proyectos (carreras) ─────────────────────────────────────────────────────
@app.get("/api/projects", response_model=list[ProjectMeta])
def list_projects() -> list[ProjectMeta]:
    return store.list()


@app.post("/api/projects", response_model=Project, status_code=201)
def create_project(data: ProjectIn) -> Project:
    return store.create(data)


@app.get("/api/projects/{pid}", response_model=Project)
def get_project(pid: str) -> Project:
    p = store.get(pid)
    if p is None:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    return p


@app.patch("/api/projects/{pid}", response_model=Project)
def update_project(pid: str, patch: ProjectPatch) -> Project:
    p = store.update(pid, patch)
    if p is None:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    return p


@app.post("/api/projects/{pid}/duplicate", response_model=Project, status_code=201)
def duplicate_project(pid: str, name: str | None = None) -> Project:
    p = store.duplicate(pid, name)
    if p is None:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    return p


@app.delete("/api/projects/{pid}", status_code=204)
def delete_project(pid: str) -> Response:
    if not store.delete(pid):
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")
    return Response(status_code=204)


# ── Exports ──────────────────────────────────────────────────────────────────
@app.get("/api/routes/{route_id}.geojson")
def route_geojson(route_id: str) -> dict:
    entry = manager.route(route_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Ruta no encontrada")
    r = entry["route"]
    return {
        "type": "FeatureCollection",
        "features": [r.geojson()],
        "attribution": "© OpenStreetMap contributors (ODbL)",
    }


@app.get("/api/routes/{route_id}.gpx")
def route_gpx(route_id: str) -> Response:
    entry = manager.route(route_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Ruta no encontrada")
    r = entry["route"]
    m = entry["model"]
    pts = "\n".join(
        f'      <trkpt lat="{lat:.6f}" lon="{lon:.6f}"><ele>{ele:.1f}</ele></trkpt>'
        for lon, lat, ele in r.coordinates
    )
    gpx = f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="RaceEngine {ENGINE_VERSION}"
     xmlns="http://www.topografix.com/GPX/1/1">
  <metadata>
    <name>RaceEngine {m.distance.target_m / 1000:.1f} km</name>
    <desc>Distancia {r.metrics.distance_m / 1000:.3f} km · score {r.global_score:.1f}/100 · """ \
        f"""© OpenStreetMap contributors (ODbL)</desc>
  </metadata>
  <trk>
    <name>RaceEngine {r.route_id}</name>
    <trkseg>
{pts}
    </trkseg>
  </trk>
</gpx>
"""
    return Response(
        content=gpx, media_type="application/gpx+xml",
        headers={"Content-Disposition": f'attachment; filename="raceengine_{route_id}.gpx"'},
    )
