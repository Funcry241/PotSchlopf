import argparse
from contextlib import closing
from datetime import UTC, datetime
import os
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4


SCHEMA_VERSION = 2
# Obergrenze für die ganze Datei (Notizen und Zugriffsprotokoll zusammen).
MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_NOTE_BYTES = 16384

CREATE_NOTES = """
CREATE TABLE notizen (
    id TEXT PRIMARY KEY NOT NULL,
    absender TEXT NOT NULL,
    erstellt_utc TEXT NOT NULL,
    inhalt TEXT NOT NULL CHECK (length(CAST(inhalt AS BLOB)) BETWEEN 1 AND 16384)
)
"""

# Version 2: Notizen und Protokoll lassen sich nur noch ergänzen, nie ändern oder löschen.
MIGRATE_TO_2 = [
    """
    CREATE TRIGGER notizen_nur_ergaenzen_update BEFORE UPDATE ON notizen
    BEGIN SELECT RAISE(ABORT, 'Notizen lassen sich nicht ändern.'); END
    """,
    """
    CREATE TRIGGER notizen_nur_ergaenzen_delete BEFORE DELETE ON notizen
    BEGIN SELECT RAISE(ABORT, 'Notizen lassen sich nicht löschen.'); END
    """,
    """
    CREATE TABLE zugriffe (
        nr INTEGER PRIMARY KEY AUTOINCREMENT,
        zeit_utc TEXT NOT NULL,
        quelle TEXT NOT NULL,
        methode TEXT NOT NULL,
        pfad TEXT NOT NULL,
        status INTEGER NOT NULL,
        absender TEXT,
        notiz_id TEXT,
        programm TEXT
    )
    """,
    """
    CREATE TRIGGER zugriffe_nur_ergaenzen_update BEFORE UPDATE ON zugriffe
    BEGIN SELECT RAISE(ABORT, 'Das Protokoll lässt sich nicht ändern.'); END
    """,
    """
    CREATE TRIGGER zugriffe_nur_ergaenzen_delete BEFORE DELETE ON zugriffe
    BEGIN SELECT RAISE(ABORT, 'Das Protokoll lässt sich nicht löschen.'); END
    """,
]


def open_archive(database_path: Path) -> sqlite3.Connection:
    """Öffnet das Archiv mit Größenlimit. max_page_count gilt nur je Verbindung."""
    database = sqlite3.connect(database_path, isolation_level=None, check_same_thread=False)
    try:
        database.execute("PRAGMA busy_timeout = 5000")
        page_size = database.execute("PRAGMA page_size").fetchone()[0]
        max_pages = MAX_DATABASE_BYTES // page_size
        applied_limit = database.execute(f"PRAGMA max_page_count = {max_pages}").fetchone()[0]
        if applied_limit != max_pages:
            raise RuntimeError("Die vorhandene Datenbank überschreitet das Größenlimit.")
    except BaseException:
        database.close()
        raise
    return database


def prepare_archive(database_path: Path) -> int:
    with closing(open_archive(database_path)) as database:
        database.execute("BEGIN IMMEDIATE")
        try:
            version = database.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                existing_tables = database.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchone()
                if existing_tables:
                    raise RuntimeError("Die Datenbank enthält ein unbekanntes Schema.")
                database.execute(CREATE_NOTES)
                version = 1
            if version == 1:
                for statement in MIGRATE_TO_2:
                    database.execute(statement)
                version = 2
            if version != SCHEMA_VERSION:
                raise RuntimeError(f"Die Archivversion {version} wird von diesem Paket nicht unterstützt.")
            database.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

            run_write_probe(database)
        except BaseException:
            database.execute("ROLLBACK")
            raise
        database.execute("COMMIT")

    read_only_uri = database_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(read_only_uri, uri=True)) as database:
        if database.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError("SQLite meldet einen Fehler in der Archivdatei.")
        return database.execute("SELECT COUNT(*) FROM notizen").fetchone()[0]


def run_write_probe(database: sqlite3.Connection) -> None:
    note_id = uuid4().hex
    probe_text = "Schreib- und Leseprobe auf dem NAS."
    database.execute("SAVEPOINT probe")
    database.execute(
        "INSERT INTO notizen (id, absender, erstellt_utc, inhalt) VALUES (?, ?, ?, ?)",
        (note_id, "Einrichtung", datetime.now(UTC).isoformat(), probe_text),
    )
    result = database.execute("SELECT inhalt FROM notizen WHERE id = ?", (note_id,)).fetchone()
    if result != (probe_text,):
        raise RuntimeError("Die Schreib- und Leseprobe lieferte einen unerwarteten Text.")
    try:
        database.execute("DELETE FROM notizen WHERE id = ?", (note_id,))
    except sqlite3.IntegrityError:
        pass
    else:
        raise RuntimeError("Der Löschschutz für Notizen greift nicht.")
    database.execute("ROLLBACK TO probe")
    database.execute("RELEASE probe")


def main() -> int:
    parser = argparse.ArgumentParser(description="Privates PotSchlopf-Archiv einrichten und prüfen.")
    parser.add_argument("--ordner", type=Path, default=Path("/archiv"))
    args = parser.parse_args()

    os.umask(0o077)
    try:
        note_count = prepare_archive(args.ordner / "potschlopf.sqlite3")
    except (OSError, RuntimeError, sqlite3.Error) as error:
        print(f"Das Archiv konnte nicht eingerichtet werden. {error}", file=sys.stderr)
        return 1

    print("PotSchlopf-Archiv bereit.")
    print("Schreib- und Leseprobe erfolgreich.")
    unit = "Notiz" if note_count == 1 else "Notizen"
    print(f"Das Archiv enthält {note_count} {unit}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
