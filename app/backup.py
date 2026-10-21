"""Copias de la base SQLite. En PostgreSQL el respaldo lo hace scripts/backup.sh."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy.engine import Engine

from app.db import DATA
from app.logic.common import BusinessError
from app.utils import BOGOTA

KEEP = 30


def sqlite_file(engine: Engine) -> Path | None:
    if engine.dialect.name != "sqlite":
        return None
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return Path(database)


def backup_dir() -> Path:
    folder = DATA / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def make_backup(engine: Engine) -> Path | None:
    source = sqlite_file(engine)
    if source is None or not source.exists():
        return None
    stamp = datetime.now(BOGOTA).strftime("%Y%m%d-%H%M%S")
    target = backup_dir() / f"kinkara-{stamp}.db"
    with sqlite3.connect(source) as origin, sqlite3.connect(target) as copy:
        origin.backup(copy)
    _prune()
    return target


def latest_backup() -> Path | None:
    files = sorted(backup_dir().glob("kinkara-*.db"))
    return files[-1] if files else None


def maybe_daily_backup(engine: Engine) -> Path | None:
    source = sqlite_file(engine)
    expected = (DATA / "kinkara.db").resolve()
    if source is None or not source.exists() or source.resolve() != expected:
        return None
    newest = latest_backup()
    if newest is not None and (datetime.now().timestamp() - newest.stat().st_mtime) < 20 * 3600:
        return None
    return make_backup(engine)


def list_backups() -> list[Path]:
    return sorted(backup_dir().glob("kinkara-*.db"), reverse=True)


def restore_into_session(db, backup: Path, phrase: str) -> None:
    """Copia una respaldo sobre la base abierta, sin borrar el archivo por debajo."""
    import sqlite3

    if phrase.strip() != "RESTAURAR":
        raise BusinessError("Escribe RESTAURAR para reemplazar los datos actuales.")
    if not backup.exists() or backup.parent.resolve() != backup_dir().resolve():
        raise BusinessError("Esa copia no está en la carpeta de respaldos.")
    raw = db.connection().connection
    if not hasattr(raw, "backup"):
        raise BusinessError("Esta pantalla restaura la base SQLite local. En PostgreSQL usa scripts/backup.sh.")
    make_backup(db.get_bind())
    origin = sqlite3.connect(backup)
    try:
        origin.backup(raw)
    finally:
        origin.close()
    db.commit()
    db.expire_all()


def restore_sqlite(engine: Engine, backup: Path, phrase: str) -> None:
    if phrase.strip() != "RESTAURAR":
        raise BusinessError("Escribe RESTAURAR para reemplazar los datos actuales.")
    source = sqlite_file(engine)
    if source is None:
        raise BusinessError("Esta pantalla restaura la base SQLite local. En PostgreSQL usa scripts/backup.sh.")
    if not backup.exists() or backup.parent.resolve() != backup_dir().resolve() or backup.suffix != ".db":
        raise BusinessError("Esa copia no está en la carpeta de respaldos.")
    make_backup(engine)
    engine.dispose()
    shutil.copyfile(backup, source)


def _prune() -> None:
    files = sorted(backup_dir().glob("kinkara-*.db"))
    for old in files[:-KEEP]:
        old.unlink(missing_ok=True)
