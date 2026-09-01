import sqlite3
import os
from datetime import datetime, timedelta

import pytest

from backend.services.db_backup import (
    cleanup_backups,
    create_backup,
    inspect_database,
    list_backups,
    plan_backup_cleanup,
    restore_backup,
)


def test_create_backup_preserves_database_contents(tmp_path):
    source_path = tmp_path / "source.db"
    backup_dir = tmp_path / "backups"
    with sqlite3.connect(source_path) as connection:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO records (value) VALUES (?)", ("保留数据",))

    backup_path = create_backup(source_path, backup_dir)

    assert backup_path.parent == backup_dir
    assert backup_path.name.startswith("app-")
    assert backup_path != source_path
    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("SELECT value FROM records").fetchone() == ("保留数据",)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_create_backup_rejects_missing_database(tmp_path):
    with pytest.raises(FileNotFoundError):
        create_backup(tmp_path / "missing.db", tmp_path / "backups")


def test_restore_backup_creates_safety_snapshot(tmp_path):
    source_path = tmp_path / "source.db"
    backup_dir = tmp_path / "backups"
    with sqlite3.connect(source_path) as connection:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO records (value) VALUES (?)", ("旧数据",))
    restore_path = create_backup(source_path, backup_dir)
    with sqlite3.connect(source_path) as connection:
        connection.execute("UPDATE records SET value = ?", ("当前数据",))

    restored_path, safety_path = restore_backup(restore_path.name, source_path, backup_dir)

    assert restored_path == restore_path
    assert safety_path.exists()
    with sqlite3.connect(source_path) as connection:
        assert connection.execute("SELECT value FROM records").fetchone() == ("旧数据",)
    with sqlite3.connect(safety_path) as connection:
        assert connection.execute("SELECT value FROM records").fetchone() == ("当前数据",)


def test_restore_backup_rejects_path_traversal(tmp_path):
    with pytest.raises(ValueError):
        restore_backup("..\\source.db", tmp_path / "source.db", tmp_path / "backups")


def test_inspect_database_reports_foreign_key_violations(tmp_path):
    path = tmp_path / "broken.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("CREATE TABLE parents (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE children (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parents(id))"
        )
        connection.execute("INSERT INTO children (parent_id) VALUES (999)")

    report = inspect_database(path)

    assert report["ok"] is False
    assert report["integrity_check"] == "ok"
    assert report["foreign_key_violations"][0]["table"] == "children"


def test_list_backups_marks_corrupt_file(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    valid_path = tmp_path / "source.db"
    with sqlite3.connect(valid_path) as connection:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")
    create_backup(valid_path, backup_dir)
    (backup_dir / "app-corrupt.db").write_bytes(b"not a sqlite database")

    items = list_backups(backup_dir)

    assert len(items) == 2
    corrupt = next(item for item in items if item["filename"] == "app-corrupt.db")
    assert corrupt["valid"] is False
    assert corrupt["error"]


def test_plan_backup_cleanup_keeps_recent_count_and_recent_days(tmp_path):
    source_path = tmp_path / "source.db"
    backup_dir = tmp_path / "backups"
    with sqlite3.connect(source_path) as connection:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")

    old_paths = [create_backup(source_path, backup_dir) for _ in range(31)]
    recent_path = create_backup(source_path, backup_dir)
    now = datetime.now()
    for path in old_paths:
        old_time = (now - timedelta(days=60)).timestamp()
        os.utime(path, (old_time, old_time))
    recent_time = (now - timedelta(days=1)).timestamp()
    os.utime(recent_path, (recent_time, recent_time))

    plan = plan_backup_cleanup(backup_dir, now=now)

    assert plan["deletable_count"] == 2
    assert {item["filename"] for item in plan["candidates"]} <= {
        path.name for path in old_paths
    }
    assert recent_path.name not in {item["filename"] for item in plan["candidates"]}


def test_cleanup_backups_requires_confirmation_and_preserves_invalid_files(tmp_path):
    source_path = tmp_path / "source.db"
    backup_dir = tmp_path / "backups"
    with sqlite3.connect(source_path) as connection:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")

    paths = [create_backup(source_path, backup_dir) for _ in range(32)]
    now = datetime.now()
    old_time = (now - timedelta(days=60)).timestamp()
    for path in paths[:2]:
        os.utime(path, (old_time, old_time))
    corrupt_path = backup_dir / "app-corrupt.db"
    corrupt_path.write_bytes(b"not a sqlite database")

    with pytest.raises(ValueError):
        cleanup_backups(backup_dir, confirm=False, now=now)

    result = cleanup_backups(backup_dir, confirm=True, now=now)

    assert result["deleted_count"] == 2
    assert not paths[0].exists()
    assert not paths[1].exists()
    assert corrupt_path.exists()
