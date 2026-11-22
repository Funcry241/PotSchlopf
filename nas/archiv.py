import argparse
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256
import os
from pathlib import Path
import re
import sqlite3
import sys
from uuid import uuid4


SCHEMA_VERSION = 1
MAX_DATABASE_BYTES = 64 * 1024 * 1024
NOTE_ID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
CREATE_NOTES = """
CREATE TABLE notizen (
    id TEXT PRIMARY KEY NOT NULL,
    absender TEXT NOT NULL,
    erstellt_utc TEXT NOT NULL,
    inhalt TEXT NOT NULL CHECK (length(CAST(inhalt AS BLOB)) BETWEEN 1 AND 16384)
)
"""
CREATE_SYNC_STATE = """
CREATE TABLE IF NOT EXISTS abrufstand (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    dienst TEXT NOT NULL,
    folge INTEGER NOT NULL CHECK (folge >= 0)
)
"""
CREATE_DELETIONS = """
CREATE TABLE IF NOT EXISTS geloescht (
    id_hash BLOB PRIMARY KEY NOT NULL
) WITHOUT ROWID
"""


def open_existing_archive(database_path):
    # Nur die Einrichtung darf eine neue Datenbank anlegen.
    return sqlite3.connect(database_path.resolve().as_uri() + "?mode=rw", uri=True)


def id_hash(note_id):
    return sha256(note_id.encode("ascii")).digest()


def delete_note(database_path: Path, note_id: str) -> bool:
    note_id = note_id.lower()
    if not NOTE_ID.fullmatch(note_id):
        raise ValueError("Bitte die vollständige Notiz-ID mit Bindestrichen eingeben.")
    with closing(open_existing_archive(database_path)) as database, database:
        enforce_database_limit(database)
        database.execute("PRAGMA secure_delete = ON")
        database.execute("BEGIN IMMEDIATE")
        if database.execute("PRAGMA user_version").fetchone() != (SCHEMA_VERSION,):
            raise RuntimeError("Die Datei ist kein unterstütztes PotSchlopf-Archiv.")
        deleted = database.execute("DELETE FROM notizen WHERE id = ?", (note_id,)).rowcount
        database.execute(CREATE_DELETIONS)
        database.execute("INSERT INTO geloescht (id_hash) VALUES (?) ON CONFLICT DO NOTHING",
                         (id_hash(note_id),))
        return bool(deleted)


def enforce_database_limit(database: sqlite3.Connection) -> None:
    page_size = database.execute("PRAGMA page_size").fetchone()[0]
    max_pages = MAX_DATABASE_BYTES // page_size
    applied_limit = database.execute(f"PRAGMA max_page_count = {max_pages}").fetchone()[0]
    if applied_limit != max_pages:
        raise RuntimeError("Die vorhandene Datenbank überschreitet das Limit von 64 MiB.")


def prepare_archive(database_path: Path) -> int:
    with closing(sqlite3.connect(database_path)) as database, database:
        enforce_database_limit(database)
        database.execute("BEGIN IMMEDIATE")
        version = database.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            existing_tables = database.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchone()
            if existing_tables:
                raise RuntimeError("Die Datenbank enthält ein unbekanntes Schema.")
            database.execute(CREATE_NOTES)
            database.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version != SCHEMA_VERSION:
            raise RuntimeError(f"Die Archivversion {version} wird von diesem Paket nicht unterstützt.")

        run_write_probe(database)
        database.execute(CREATE_SYNC_STATE)
        database.execute(CREATE_DELETIONS)

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
    database.execute("ROLLBACK TO probe")
    database.execute("RELEASE probe")


def main() -> int:
    parser = argparse.ArgumentParser(description="Privates PotSchlopf-Archiv einrichten und prüfen.")
    parser.add_argument("--ordner", type=Path, default=Path("/archiv"))
    parser.add_argument("--loeschen", metavar="NOTIZ-ID",
                        help="Eine Notiz aus dem lokalen Archiv entfernen und ihren erneuten Import sperren.")
    args = parser.parse_args()

    os.umask(0o077)
    database_path = args.ordner / "potschlopf.sqlite3"
    if args.loeschen is not None:
        try:
            deleted = delete_note(database_path, args.loeschen)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            print(f"Die Archivlöschung ist fehlgeschlagen. {error}", file=sys.stderr)
            return 1
        print("Notiz aus dem Archiv gelöscht." if deleted else "Diese Notiz ist im Archiv nicht vorhanden.")
        print("Erneuter Import dieser Notiz-ID gesperrt.")
        return 0
    try:
        note_count = prepare_archive(database_path)
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
