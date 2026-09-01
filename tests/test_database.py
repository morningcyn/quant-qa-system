from sqlalchemy import create_engine, event

from backend.db.database import _configure_sqlite_connection


def test_sqlite_connection_pragmas_are_enabled(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pragmas.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    event.listen(engine, "connect", _configure_sqlite_connection)

    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"
    finally:
        engine.dispose()
