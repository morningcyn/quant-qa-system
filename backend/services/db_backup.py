import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from backend.config import DATA_DIR, DB_PATH
from backend.db.database import engine


BACKUP_DIR = DATA_DIR / "backups"
DEFAULT_KEEP_COUNT = 30
DEFAULT_KEEP_DAYS = 30


def create_backup(source_path: Path = DB_PATH, backup_dir: Path = BACKUP_DIR) -> Path:
    """Create a consistent SQLite backup without copying the live database file."""
    source_path = Path(source_path)
    backup_dir = Path(backup_dir)
    if not source_path.is_file():
        raise FileNotFoundError(f"Database file not found: {source_path}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    target_path = _next_backup_path(backup_dir)
    source = sqlite3.connect(str(source_path), timeout=30)
    target = sqlite3.connect(str(target_path), timeout=30)
    try:
        source.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise sqlite3.DatabaseError("Backup integrity check failed")
        target.commit()
    except Exception:
        target.close()
        target_path.unlink(missing_ok=True)
        source.close()
        raise
    else:
        target.close()
        source.close()
    return target_path


def _next_backup_path(backup_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    candidate = backup_dir / f"app-{stamp}.db"
    suffix = 1
    while candidate.exists():
        candidate = backup_dir / f"app-{stamp}-{suffix}.db"
        suffix += 1
    return candidate


def restore_backup(
    filename: str,
    source_path: Path = DB_PATH,
    backup_dir: Path = BACKUP_DIR,
) -> tuple[Path, Path]:
    """Restore a validated backup and return (restored_backup, safety_backup)."""
    source_path = Path(source_path)
    backup_dir = Path(backup_dir)
    backup_path = _resolve_backup_path(filename, backup_dir)
    _check_integrity(backup_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"Database file not found: {source_path}")

    safety_backup = create_backup(source_path, backup_dir)
    if source_path.resolve() == DB_PATH.resolve():
        engine.dispose()
    try:
        _copy_database(backup_path, source_path)
        _check_integrity(source_path)
    except Exception:
        try:
            _copy_database(safety_backup, source_path)
        except Exception as rollback_error:
            raise sqlite3.DatabaseError(
                f"Restore failed and automatic rollback failed: {rollback_error}"
            ) from rollback_error
        raise
    return backup_path, safety_backup


def _resolve_backup_path(filename: str, backup_dir: Path) -> Path:
    if not filename or Path(filename).name != filename or Path(filename).suffix.lower() != ".db":
        raise ValueError("Invalid backup filename")
    backup_root = backup_dir.resolve()
    backup_path = (backup_root / filename).resolve()
    if not backup_path.is_relative_to(backup_root):
        raise ValueError("Backup file must be inside the backup directory")
    if not backup_path.is_file():
        raise FileNotFoundError(f"Backup file not found: {filename}")
    return backup_path


def _check_integrity(path: Path) -> None:
    connection = sqlite3.connect(str(path), timeout=30)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise sqlite3.DatabaseError(f"Database integrity check failed: {path}")
    finally:
        connection.close()


def _copy_database(source_path: Path, target_path: Path) -> None:
    source = sqlite3.connect(str(source_path), timeout=30)
    target = sqlite3.connect(str(target_path), timeout=30)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()


def inspect_database(path: Path = DB_PATH) -> dict:
    """Return read-only SQLite integrity and foreign-key diagnostics."""
    path = Path(path)
    if not path.is_file():
        return {
            "ok": False,
            "error": "not_found",
            "integrity_check": None,
            "foreign_key_violations": [],
        }

    connection = sqlite3.connect(str(path), timeout=30)
    try:
        integrity_check = connection.execute("PRAGMA integrity_check").fetchone()[0]
        violations = [
            {
                "table": row[0],
                "rowid": row[1],
                "parent": row[2],
                "foreign_key_id": row[3],
            }
            for row in connection.execute("PRAGMA foreign_key_check")
        ]
        return {
            "ok": integrity_check == "ok" and not violations,
            "error": None,
            "integrity_check": integrity_check,
            "foreign_key_violations": violations,
        }
    except sqlite3.DatabaseError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "integrity_check": None,
            "foreign_key_violations": [],
        }
    finally:
        connection.close()


def list_backups(backup_dir: Path = BACKUP_DIR) -> list[dict]:
    """List backup files newest first, including their integrity status."""
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return []

    items = []
    for path in backup_dir.glob("*.db"):
        if not path.is_file():
            continue
        stat = path.stat()
        health = inspect_database(path)
        items.append(
            {
                "filename": path.name,
                "size": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(sep=" ", timespec="seconds"),
                "valid": health["ok"],
                "integrity_check": health["integrity_check"],
                "foreign_key_violations": health["foreign_key_violations"],
                "error": health["error"],
            }
        )
    return sorted(items, key=lambda item: item["modified_at"], reverse=True)


def plan_backup_cleanup(
    backup_dir: Path = BACKUP_DIR,
    keep_count: int = DEFAULT_KEEP_COUNT,
    keep_days: int = DEFAULT_KEEP_DAYS,
    now: datetime | None = None,
) -> dict:
    """Return a dry-run cleanup plan without deleting any backup files."""
    if keep_count < 1 or keep_days < 1:
        raise ValueError("Backup retention values must be positive")

    items = list_backups(backup_dir)
    valid_items = [item for item in items if item["valid"]]
    cutoff = (now or datetime.now()) - timedelta(days=keep_days)
    newest_filenames = {item["filename"] for item in valid_items[:keep_count]}
    candidates = []
    protected = []

    for item in valid_items:
        modified_at = datetime.fromisoformat(item["modified_at"])
        if item["filename"] in newest_filenames or modified_at >= cutoff:
            protected.append(item)
        else:
            candidates.append(item)

    return {
        "policy": {
            "keep_count": keep_count,
            "keep_days": keep_days,
        },
        "cutoff": cutoff.isoformat(sep=" ", timespec="seconds"),
        "total_count": len(items),
        "valid_count": len(valid_items),
        "invalid_count": len(items) - len(valid_items),
        "protected_count": len(protected),
        "deletable_count": len(candidates),
        "candidates": candidates,
    }


def cleanup_backups(
    backup_dir: Path = BACKUP_DIR,
    keep_count: int = DEFAULT_KEEP_COUNT,
    keep_days: int = DEFAULT_KEEP_DAYS,
    confirm: bool = False,
    now: datetime | None = None,
) -> dict:
    """Delete only valid backups selected by the retention policy."""
    if not confirm:
        raise ValueError("Backup cleanup requires explicit confirmation")

    plan = plan_backup_cleanup(backup_dir, keep_count, keep_days, now)
    deleted = []
    failed = []
    for item in plan["candidates"]:
        try:
            path = _resolve_backup_path(item["filename"], Path(backup_dir))
            path.unlink()
            deleted.append(item)
        except (FileNotFoundError, OSError) as exc:
            failed.append({"filename": item["filename"], "error": str(exc)})

    return {
        "policy": plan["policy"],
        "deleted": deleted,
        "failed": failed,
        "deleted_count": len(deleted),
        "failed_count": len(failed),
        "remaining": list_backups(backup_dir),
    }
