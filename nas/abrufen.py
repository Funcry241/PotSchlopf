import argparse
from contextlib import closing
from datetime import datetime
from errno import ENOSPC
from http.client import HTTPSConnection, HTTPException
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
from time import monotonic, sleep, time
from urllib.parse import urlsplit

from archiv import enforce_database_limit, prepare_archive


PAGE_SIZE = 12
POLL_INTERVAL_SECONDS = 60
RETRY_INTERVALS = (60, 120, 300, 600)
RETRY_REMINDER_SECONDS = 30 * 60
LAST_SUCCESS_FILE = Path("/tmp/potschlopf-letzter-erfolg")
HEALTH_MAX_AGE_SECONDS = 15 * 60
MAX_RESPONSE_BYTES = 160 * 1024
MAX_SEQUENCE = 2**53 - 1
NOTE_ID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}")
SYNC_TABLE = """
CREATE TABLE IF NOT EXISTS abrufstand (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    dienst TEXT NOT NULL,
    folge INTEGER NOT NULL CHECK (folge >= 0)
)
"""


def settings():
    origin = urlsplit(os.environ.get("POTSCHLOPF_SERVICE_URL", ""))
    if (origin.scheme != "https" or not origin.hostname or origin.username is not None
            or origin.password is not None or origin.path not in ("", "/")
            or origin.query or origin.fragment):
        raise ValueError("POTSCHLOPF_SERVICE_URL muss eine HTTPS-Dienstadresse sein.")
    if origin.port == 0:
        raise ValueError("Der HTTPS-Port muss größer als 0 sein.")
    return origin


def text_feld(record, key, limit):
    value = record.get(key)
    if (not isinstance(value, str) or not value.strip()
            or len(value.encode("utf-8")) > limit
            or any(ord(char) < 32 and char not in "\t\r\n" or ord(char) == 127 for char in value)):
        raise ValueError(f"Ungültiges Textfeld {key} im Archivabruf.")
    return value


def validate_page(data, after):
    if not isinstance(data, dict):
        raise ValueError("Archivseite muss ein JSON-Objekt sein.")
    notes = data.get("notizen")
    cursor = data.get("naechster_stand")
    more = data.get("weitere")
    if not isinstance(notes, list) or len(notes) > PAGE_SIZE or type(more) is not bool:
        raise ValueError("Ungültige Archivseite.")
    if type(cursor) is not int or cursor < after or cursor > MAX_SEQUENCE:
        raise ValueError("Ungültiger nächster Abrufstand.")
    if more and len(notes) != PAGE_SIZE:
        raise ValueError("Unvollständige Archivseite.")

    previous = after
    validated = []
    for record in notes:
        if not isinstance(record, dict):
            raise ValueError("Ungültige Archivnotiz.")
        sequence = record.get("folge")
        if type(sequence) is not int or not previous < sequence <= MAX_SEQUENCE:
            raise ValueError("Ungültige Reihenfolge der Archivnotizen.")
        note_id = text_feld(record, "id", 36)
        if not NOTE_ID.fullmatch(note_id):
            raise ValueError("Ungültige Notiz-ID.")
        author = text_feld(record, "absender", 96)
        if any(char in author for char in "\r\n\t"):
            raise ValueError("Der Postfachname muss einzeilig sein.")
        created = text_feld(record, "erstellt_utc", 24)
        if not created.endswith("Z"):
            raise ValueError("UTC-Zeitstempel erwartet.")
        datetime.fromisoformat(created)
        note = text_feld(record, "inhalt", 4096)
        validated.append((note_id, author, created, note))
        previous = sequence

    if cursor != previous:
        raise ValueError("Abrufstand passt nicht zu den gelieferten Notizen.")
    return validated, cursor, more


def fetch_page(origin, after):
    with closing(HTTPSConnection(origin.hostname, origin.port, timeout=10)) as connection:
        connection.request(
            "GET",
            f"/notizen?nach={after}",
            headers={"Accept": "application/json"},
        )
        with closing(connection.getresponse()) as response:
            if response.status != 200:
                raise RuntimeError(f"Notizdienst antwortet mit HTTP {response.status}.")
            if response.headers.get_content_type() != "application/json":
                raise ValueError("Archivantwort muss JSON sein.")
            body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("Archivantwort zu groß.")
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, RecursionError) as error:
        raise ValueError("Die Antwort des Notizdienstes ist unlesbar.") from error
    return validate_page(data, after)


def read_cursor(database, source):
    record = database.execute("SELECT dienst, folge FROM abrufstand WHERE id = 1").fetchone()
    if record is None:
        return 0
    if record[0] != source:
        raise ValueError("Das Archiv enthält einen Abrufstand für einen anderen Dienst.")
    return record[1]


def save_page(database_path, source, after, page):
    notes, cursor, _ = page
    inserted = 0
    with closing(sqlite3.connect(database_path)) as database, database:
        enforce_database_limit(database)
        database.execute("BEGIN IMMEDIATE")
        database.execute(SYNC_TABLE)
        if read_cursor(database, source) != after:
            raise RuntimeError("Der Abrufstand hat sich während des Downloads geändert.")
        for note in notes:
            result = database.execute(
                "INSERT INTO notizen (id, absender, erstellt_utc, inhalt) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO NOTHING",
                note,
            )
            if result.rowcount:
                inserted += 1
                continue
            stored_note = database.execute(
                "SELECT id, absender, erstellt_utc, inhalt FROM notizen WHERE id = ?",
                (note[0],),
            ).fetchone()
            if stored_note != note:
                raise sqlite3.IntegrityError("Eine Notiz-ID ist bereits mit anderem Inhalt im Archiv.")
        database.execute(
            "INSERT INTO abrufstand (id, dienst, folge) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET folge = excluded.folge",
            (source, cursor),
        )
    return inserted


def import_batch(database_path, origin):
    source = origin.netloc.lower()
    with closing(sqlite3.connect(database_path)) as database, database:
        enforce_database_limit(database)
        database.execute(SYNC_TABLE)
        cursor = read_cursor(database, source)
    imported = 0
    for _ in range(4):
        page = fetch_page(origin, cursor)
        imported += save_page(database_path, source, cursor, page)
        _, cursor, more = page
        if not more:
            break
    return imported


def health_status():
    try:
        age = time() - LAST_SUCCESS_FILE.stat().st_mtime
    except OSError:
        return 1
    return 0 if 0 <= age < HEALTH_MAX_AGE_SECONDS else 1


def log_success_time():
    try:
        LAST_SUCCESS_FILE.touch()
    except OSError:
        # Ältere Compose-Projekte haben kein beschreibbares /tmp.
        pass


def recovery_hint(error, archive_ready):
    if isinstance(error, sqlite3.Error):
        code = getattr(error, "sqlite_errorcode", 0) & 255
        if code == sqlite3.SQLITE_FULL:
            return "Prüfe den freien NAS-Speicher und das Archivlimit von 64 MiB."
        if code in (sqlite3.SQLITE_READONLY, sqlite3.SQLITE_CANTOPEN):
            return "Falls das anhält, prüfe das Archiv-Volume im Container Manager."
    if isinstance(error, OSError) and error.errno == ENOSPC:
        return "Prüfe den freien Speicherplatz auf dem NAS."
    if isinstance(error, PermissionError) or (isinstance(error, OSError) and not archive_ready):
        return "Falls das anhält, prüfe das Archiv-Volume im Container Manager."
    if isinstance(error, (OSError, HTTPException)):
        return "Falls das anhält, prüfe die Internetverbindung des NAS."
    return ""


def main():
    parser = argparse.ArgumentParser(description="PotSchlopf-Textnotizen über HTTPS ins NAS-Archiv übernehmen.")
    parser.add_argument("--ordner", type=Path, default=Path("/archiv"))
    parser.add_argument("--dauerhaft", action="store_true")
    args = parser.parse_args()
    database_path = args.ordner / "potschlopf.sqlite3"
    os.umask(0o077)

    origin = None
    archive_ready = False
    first_success = True
    last_problem = None
    last_report = 0.0
    failures = 0
    while True:
        delay = POLL_INTERVAL_SECONDS
        phase = "Prüfe die HTTPS-Dienstadresse in der Projektkonfiguration."
        try:
            if origin is None:
                origin = settings()
            if not archive_ready:
                phase = "Das Archiv ist gerade nicht verfügbar."
                count = prepare_archive(database_path)
                archive_ready = True
                if args.dauerhaft:
                    unit = "Notiz" if count == 1 else "Notizen"
                    print(f"PotSchlopf-Archiv bereit. Es enthält {count} {unit}.")
                    print(f"Der Notizabruf startet. Quelle {origin.hostname}.")
            phase = "Der Notizabruf ist gerade unterbrochen."
            imported = import_batch(database_path, origin)
        except (OSError, ValueError, RuntimeError, HTTPException, sqlite3.Error) as error:
            problem = f"{phase} ({error}) {recovery_hint(error, archive_ready)}".rstrip()
            if not args.dauerhaft:
                print(problem, file=sys.stderr)
                return 1
            delay = RETRY_INTERVALS[min(failures, len(RETRY_INTERVALS) - 1)]
            failures += 1
            now = monotonic()
            if problem != last_problem or now - last_report >= RETRY_REMINDER_SECONDS:
                print(f"PotSchlopf wartet. {problem} Nächster Versuch in {delay} Sekunden.", file=sys.stderr)
                last_report = now
            last_problem = problem
        else:
            if args.dauerhaft:
                log_success_time()
            if last_problem is not None:
                print("Alles wieder gut. Der Abruf funktioniert wieder.")
            if imported or first_success or last_problem is not None:
                print(f"Archivabruf erfolgreich. Übernommene Notizen {imported}.")
            first_success = False
            last_problem = None
            failures = 0
            if not args.dauerhaft:
                return 0
        sleep(delay)


def stop(_signal, _frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop)
    try:
        status = main()
    except KeyboardInterrupt:
        print("PotSchlopf hält an. Bis bald!")
        status = 0
    raise SystemExit(status)
