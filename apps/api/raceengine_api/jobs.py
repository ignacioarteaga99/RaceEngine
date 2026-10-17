"""Gestor de generaciones en proceso.

En despliegue real esto es una cola (Redis + workers, doc 11 §1). Para el modo
local se usan hilos y colas en memoria: el contrato hacia la API es idéntico, de
modo que sustituir el backend no cambia los routers.
"""
from __future__ import annotations

import os
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field

from raceengine_contracts import ConstraintModel, GenerationResult
from raceengine_engine import GenerationHandle, generate

MAX_JOBS = 64


@dataclass
class Job:
    id: str
    model: ConstraintModel
    handle: GenerationHandle
    events: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=512))
    result: GenerationResult | None = None
    created_at: float = field(default_factory=time.time)
    finished: threading.Event = field(default_factory=threading.Event)
    error: str | None = None

    @property
    def status(self) -> str:
        if self.error:
            return "failed"
        return self.result.status if self.result else "running"


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._routes: dict[str, dict] = {}     # route_id → {"route": ..., "model": ...}
        self._lock = threading.Lock()

    # ── ciclo de vida ────────────────────────────────────────────────────────
    def submit(self, model: ConstraintModel) -> Job:
        jid = uuid.uuid4().hex
        job = Job(id=jid, model=model, handle=GenerationHandle(generation_id=jid))
        with self._lock:
            self._jobs[jid] = job
            if len(self._jobs) > MAX_JOBS:
                for old in sorted(self._jobs.values(), key=lambda j: j.created_at)[:8]:
                    if old.finished.is_set():
                        self._jobs.pop(old.id, None)

        def on_update(event: str, result: GenerationResult) -> None:
            job.result = result
            try:
                job.events.put_nowait((event, result.model_dump(mode="json")))
            except queue.Full:
                pass

        def run() -> None:
            try:
                res = generate(model, job.handle, on_update)
                job.result = res
                self._index_routes(res, model)
            except Exception as exc:  # noqa: BLE001
                import traceback
                job.error = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
                try:
                    job.events.put_nowait(("error", {"detail": job.error}))
                except queue.Full:
                    pass
            finally:
                job.finished.set()
                try:
                    job.events.put_nowait(("close", {}))
                except queue.Full:
                    pass

        threading.Thread(target=run, name=f"gen-{jid[:8]}", daemon=True).start()
        return job

    def _index_routes(self, res: GenerationResult, model: ConstraintModel) -> None:
        with self._lock:
            for r in ([res.recommended] if res.recommended else []) + res.alternatives:
                self._routes[r.route_id] = {"route": r, "model": model}

    def get(self, jid: str) -> Job | None:
        return self._jobs.get(jid)

    def cancel(self, jid: str) -> bool:
        job = self._jobs.get(jid)
        if not job:
            return False
        job.handle.cancel.set()
        return True

    def route(self, route_id: str) -> dict | None:
        return self._routes.get(route_id)


def _build_elsewhere_msg(spec) -> str:
    """Servidor pequeño (RACEENGINE_LOCAL_BUILD=0): construir aquí lo tumbaría."""
    extra = ""
    if spec.group in ("Mis ciudades", "Mis rutas") and not spec.corridor:
        extra = (f", label «{spec.label}», lat {spec.lat:.5f}, lon {spec.lon:.5f}, "
                 f"radius_m {spec.radius_m}")
    return ("Este servidor no tiene memoria para construir regiones (hace falta "
            "0,6–1,6 GB). Constrúyela en GitHub: Actions → «Construir región» → "
            f"Run workflow, con region «{spec.id}»{extra}. En unos minutos aparece "
            "aquí como construida.")


class RegionBuilder:
    """Construcción de regiones en segundo plano, desde la interfaz.

    Evita que el usuario tenga que abrir una segunda terminal mientras el
    servidor ocupa la primera.
    """

    def __init__(self) -> None:
        self._state: dict[str, dict] = {}
        self._lock = threading.Lock()
        # Cola FIFO con UN solo trabajador. Se construye de una en una a
        # propósito: Overpass limita por IP y lanzar cinco descargas en paralelo
        # es la forma más rápida de que las rechacen todas.
        self._queue: list[tuple[str, str]] = []
        self._worker: threading.Thread | None = None
        self._current: str | None = None

    def status(self, region: str) -> dict:
        return self._state.get(region, {"state": "idle"})

    def queue_status(self) -> dict:
        """Estado global de la cola, para pintar «3 de 7» en la interfaz."""
        with self._lock:
            pending = [r for r, _ in self._queue]
            current = self._current
        done, failed = [], []
        for rid, st in self._state.items():
            if st.get("state") == "done":
                done.append(rid)
            elif st.get("state") == "failed":
                failed.append(rid)
        return {
            "current": current,
            "current_state": self._state.get(current, {}) if current else {},
            "pending": pending,
            "done": done,
            "failed": failed,
            "active": bool(current or pending),
        }

    def enqueue(self, regions: list[str], elevation: str = "auto") -> dict:
        """Encola varias regiones. Idempotente: ignora las ya en curso o en cola."""
        with self._lock:
            ya = {r for r, _ in self._queue}
            if self._current:
                ya.add(self._current)
            nuevas = []
            for r in regions:
                if r in ya or self._state.get(r, {}).get("state") == "running":
                    continue
                ya.add(r)
                nuevas.append(r)
                self._queue.append((r, elevation))
                self._state[r] = {"state": "queued", "pct": 0.0,
                                  "message": "en cola", "log": []}
            self._ensure_worker()
        return {"queued": nuevas, **self.queue_status()}

    def _ensure_worker(self) -> None:
        """Arranca el trabajador si no lo está. Se llama con el cerrojo tomado."""
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._drain, name="build-queue",
                                        daemon=True)
        self._worker.start()

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._current = None
                    self._worker = None
                    return
                region, elevation = self._queue.pop(0)
                self._current = region
            self._build_one(region, elevation)

    def start(self, region: str, elevation: str = "auto") -> dict:
        """Compatibilidad: una sola región es una cola de un elemento."""
        self.enqueue([region], elevation)
        return self.status(region)

    def _build_one(self, region: str, elevation: str = "auto") -> dict:
        with self._lock:
            cur = self._state.get(region, {})
            if cur.get("state") == "running":
                return cur
            self._state[region] = {"state": "running", "log": [], "pct": 0.0,
                                   "message": "iniciando", "started": time.time()}

        def run() -> None:
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            started = self._state[region]["started"]

            def on_progress(pct: float, msg: str) -> None:
                st = self._state.get(region, {})
                if st.get("state") != "running":
                    return
                el = time.time() - started
                eta = (el / max(pct, 1e-6)) * (100 - pct) if pct > 3 else None
                st.update({
                    "pct": round(pct, 1), "message": msg,
                    "elapsed": round(el, 1),
                    "eta_s": round(eta) if eta and eta < 36000 else None,
                    "log": buf.getvalue().splitlines()[-20:],
                })

            try:
                from raceengine_geodata import build_region, remote
                from raceengine_geodata.regions import REGIONS, graph_path
                spec = REGIONS.get(region)
                with redirect_stdout(buf):
                    if remote.has(region) and not graph_path(region).exists():
                        # Ya construida en CI: descargar son segundos.
                        remote.fetch(region, graph_path(region), progress=on_progress)
                    elif (os.environ.get("RACEENGINE_LOCAL_BUILD", "1") == "0"
                          and spec is not None and not spec.synthetic):
                        raise RuntimeError(_build_elsewhere_msg(spec))
                    else:
                        build_region(region, elevation=elevation, progress=on_progress)
                self._state[region] = {
                    "state": "done", "pct": 100.0, "message": "listo",
                    "log": buf.getvalue().splitlines()[-40:],
                    "elapsed": round(time.time() - started, 1),
                }
            except Exception as exc:  # noqa: BLE001
                self._state[region] = {
                    "state": "failed", "pct": 0.0,
                    "error": f"{type(exc).__name__}: {exc}",
                    "log": buf.getvalue().splitlines()[-40:],
                }

        # Se ejecuta EN ESTE hilo: el trabajador de la cola es quien nos llama y
        # tiene que esperar a que termine antes de pasar a la siguiente región.
        run()
        return self._state[region]


manager = JobManager()
builder = RegionBuilder()
