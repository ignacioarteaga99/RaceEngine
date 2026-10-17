"""Proyectos (carreras) — un espacio de trabajo aislado por evento.

Cada carrera es un proyecto independiente: su punto de salida, sus zonas
obligatorias y prohibidas, y sus ajustes de capas. Editar uno no toca los demás.

Persistencia: un JSON por proyecto en `data/projects/`. Deliberadamente simple —
sin base de datos, sin migraciones, sin servidor extra. Con `DATABASE_URL` se usa
Postgres (`PgProjectStore`) con el mismo contrato HTTP, para despliegues cuyo
disco no sobrevive a un reinicio.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

DATA_DIR = Path(os.environ.get(
    "RACEENGINE_DATA", Path(__file__).resolve().parents[3] / "data"))
PROJECT_DIR = DATA_DIR / "projects"
_SAFE = re.compile(r"^[A-Za-z0-9_-]{4,40}$")


class ProjectMeta(BaseModel):
    id: str
    name: str
    description: str = ""
    created_at: float
    updated_at: float


class Project(ProjectMeta):
    """El `state` es opaco para el servidor: es el estado de la interfaz.

    Mantenerlo opaco evita que cada campo nuevo de la UI obligue a migrar el
    esquema del backend. La validación real de las restricciones la hace el
    `ConstraintModel` cuando se genera, que es donde importa.
    """

    state: dict[str, Any] = Field(default_factory=dict)


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    state: dict[str, Any] = Field(default_factory=dict)


class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    state: dict[str, Any] | None = None


class ProjectStore:
    def __init__(self, directory: Path = PROJECT_DIR):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, pid: str) -> Path:
        if not _SAFE.match(pid):
            raise ValueError(f"identificador de proyecto inválido: {pid!r}")
        return self.dir / f"{pid}.json"

    # ── lectura ──────────────────────────────────────────────────────────────
    def list(self) -> list[ProjectMeta]:
        out: list[ProjectMeta] = []
        for f in self.dir.glob("*.json"):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                out.append(ProjectMeta(**{k: d[k] for k in
                                          ("id", "name", "description",
                                           "created_at", "updated_at")}))
            except Exception:  # noqa: BLE001
                continue      # un archivo corrupto no debe tumbar la lista
        return sorted(out, key=lambda p: -p.updated_at)

    def get(self, pid: str) -> Project | None:
        path = self._path(pid)
        if not path.exists():
            return None
        try:
            return Project(**json.loads(path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            return None

    # ── escritura ────────────────────────────────────────────────────────────
    def create(self, data: ProjectIn) -> Project:
        now = time.time()
        p = Project(id=uuid.uuid4().hex[:12], name=data.name.strip(),
                    description=data.description.strip(),
                    created_at=now, updated_at=now, state=data.state)
        self._write(p)
        return p

    def update(self, pid: str, patch: ProjectPatch) -> Project | None:
        with self._lock:
            p = self.get(pid)
            if p is None:
                return None
            if patch.name is not None:
                p.name = patch.name.strip()
            if patch.description is not None:
                p.description = patch.description.strip()
            if patch.state is not None:
                p.state = patch.state
            p.updated_at = time.time()
            self._write(p)
            return p

    def delete(self, pid: str) -> bool:
        path = self._path(pid)
        if not path.exists():
            return False
        path.unlink()
        return True

    def duplicate(self, pid: str, name: str | None = None) -> Project | None:
        src = self.get(pid)
        if src is None:
            return None
        return self.create(ProjectIn(
            name=(name or f"{src.name} (copia)")[:120],
            description=src.description, state=dict(src.state)))

    def _write(self, p: Project) -> None:
        # Escritura atómica: un corte a mitad no deja un JSON truncado.
        path = self._path(p.id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(p.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)


class PgProjectStore:
    """Mismo contrato que `ProjectStore`, en Postgres (p. ej. Neon, gratis).

    Para despliegues cuyo disco se borra al reiniciar (Render gratis). Se activa
    con `DATABASE_URL`; sin ella, las carreras siguen en `data/projects/`.
    El `state` va como JSONB: sigue siendo opaco para el servidor.
    """

    _COLS = "id, name, description, created_at, updated_at"

    def __init__(self, url: str):
        import psycopg
        from psycopg.rows import dict_row

        self._connect = lambda: psycopg.connect(url, row_factory=dict_row,
                                                connect_timeout=15)
        with self._connect() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id          text PRIMARY KEY,
                    name        text NOT NULL,
                    description text NOT NULL DEFAULT '',
                    created_at  double precision NOT NULL,
                    updated_at  double precision NOT NULL,
                    state       jsonb NOT NULL DEFAULT '{}'::jsonb
                )""")

    def list(self) -> list[ProjectMeta]:
        with self._connect() as c:
            rows = c.execute(
                f"SELECT {self._COLS} FROM projects ORDER BY updated_at DESC").fetchall()
        return [ProjectMeta(**r) for r in rows]

    def get(self, pid: str) -> Project | None:
        if not _SAFE.match(pid):
            raise ValueError(f"identificador de proyecto inválido: {pid!r}")
        with self._connect() as c:
            r = c.execute(f"SELECT {self._COLS}, state FROM projects WHERE id = %s",
                          (pid,)).fetchone()
        return Project(**r) if r else None

    def create(self, data: ProjectIn) -> Project:
        now = time.time()
        p = Project(id=uuid.uuid4().hex[:12], name=data.name.strip(),
                    description=data.description.strip(),
                    created_at=now, updated_at=now, state=data.state)
        self._write(p)
        return p

    def update(self, pid: str, patch: ProjectPatch) -> Project | None:
        p = self.get(pid)
        if p is None:
            return None
        if patch.name is not None:
            p.name = patch.name.strip()
        if patch.description is not None:
            p.description = patch.description.strip()
        if patch.state is not None:
            p.state = patch.state
        p.updated_at = time.time()
        self._write(p)
        return p

    def delete(self, pid: str) -> bool:
        if not _SAFE.match(pid):
            raise ValueError(f"identificador de proyecto inválido: {pid!r}")
        with self._connect() as c:
            return c.execute("DELETE FROM projects WHERE id = %s", (pid,)).rowcount > 0

    def duplicate(self, pid: str, name: str | None = None) -> Project | None:
        src = self.get(pid)
        if src is None:
            return None
        return self.create(ProjectIn(
            name=(name or f"{src.name} (copia)")[:120],
            description=src.description, state=dict(src.state)))

    def _write(self, p: Project) -> None:
        from psycopg.types.json import Jsonb

        with self._connect() as c:
            c.execute("""
                INSERT INTO projects (id, name, description, created_at, updated_at, state)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name, description = EXCLUDED.description,
                    updated_at = EXCLUDED.updated_at, state = EXCLUDED.state""",
                (p.id, p.name, p.description, p.created_at, p.updated_at,
                 Jsonb(p.state)))


def _make_store() -> ProjectStore | PgProjectStore:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        print("[projects] carreras en Postgres (DATABASE_URL)")
        return PgProjectStore(url)
    return ProjectStore()


store = _make_store()
