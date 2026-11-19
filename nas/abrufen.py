import argparse
from contextlib import closing
from datetime import datetime
from http.client import HTTPSConnection, HTTPException
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
import threading
from time import monotonic
from urllib.parse import urlsplit

from archiv import enforce_database_limit, prepare_archive


PAGE_SIZE = 12
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
    return validate_page(json.loads(body.decode("utf-8")), after)


def read_cursor(database, source):
    record = database.execute("SELECT dienst, folge FROM abrufstand WHERE id = 1").fetchone()
    if record is None:
        return 0
    if record[0] != source:
        raise ValueError("Das Archiv enthält einen Abrufstand für einen anderen Dienst.")
    return record[1]


def save_page(database_path, source, after, page):
    notes, cursor, _ = page
    with closing(sqlite3.connect(database_path)) as database, database:
        enforce_database_limit(database)
        database.execute("BEGIN IMMEDIATE")
        database.execute(SYNC_TABLE)
        if read_cursor(database, source) != after:
            raise RuntimeError("Der Abrufstand hat sich während des Downloads geändert.")
        database.executemany(
            "INSERT INTO notizen (id, absender, erstellt_utc, inhalt) VALUES (?, ?, ?, ?)",
            notes,
        )
        database.execute(
            "INSERT INTO abrufstand (id, dienst, folge) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET folge = excluded.folge",
            (source, cursor),
        )
    return len(notes)


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


LEBENSZEICHEN = Path("/tmp/potschlopf-letzter-erfolg")
WARTEZEITEN = (60, 120, 300, 600)  # Sekunden: erst schnell, dann geduldiger
ERINNERUNG_SEKUNDEN = 30 * 60


def melde(text, fehler=False):
    print(text, file=sys.stderr if fehler else sys.stdout, flush=True)


def lebenszeichen():
    """Für die Zustandsanzeige im Container Manager. Ein Fehler hier stört den Abruf nicht."""
    try:
        LEBENSZEICHEN.write_text(datetime.now().isoformat())
    except OSError:
        pass


def warte_text(sekunden):
    minuten = max(1, round(sekunden / 60))
    return "einer Minute" if minuten == 1 else f"{minuten} Minuten"


def erklaere(error, warte):
    """Übersetzt einen Fehler in eine ruhige Meldung mit nächstem Schritt."""
    nochmal = f"Nächster Versuch in {warte_text(warte)}."
    text = str(error) or type(error).__name__
    if isinstance(error, sqlite3.Error):
        if "readonly" in text or "unable to open" in text:
            return ("Der Abruf darf gerade nicht in sein Archiv schreiben. Bitte das Projekt im Container Manager "
                    "einmal stoppen und wieder starten. Die vorhandenen Notizen bleiben unangetastet. " + nochmal)
        if "full" in text or "disk" in text:
            return ("Das Archiv ist voll (Grenze 64 MiB) oder der Speicher des NAS reicht nicht. "
                    "Bisher übernommene Notizen sind sicher. Bitte Otter Bescheid geben. " + nochmal)
        return (f"Das Archiv meldet ein Problem ({text}). Die vorhandenen Notizen bleiben unangetastet. "
                "Wenn das anhält, bitte diese Zeile an Otter schicken. " + nochmal)
    if isinstance(error, PermissionError):
        return ("Der Abruf darf gerade nicht in sein Archiv schreiben. Bitte das Projekt im Container Manager "
                "einmal stoppen und wieder starten. " + nochmal)
    if isinstance(error, (OSError, HTTPException)):
        if isinstance(error, OSError) and error.errno == 28:
            return "Auf dem NAS ist kein Platz mehr frei. Bisher übernommene Notizen sind sicher. " + nochmal
        return (f"Der Notizdienst ist gerade nicht erreichbar ({text}). Das ist meist nach kurzer Zeit "
                "vorbei. Falls es länger dauert: Hat das NAS Internet? " + nochmal)
    if isinstance(error, RuntimeError) and "HTTP" in text:
        return f"Der Notizdienst hat gerade ein Problem ({text}). Wir warten geduldig. " + nochmal
    if isinstance(error, ValueError) and "anderen Dienst" in text:
        return ("Dieses Archiv gehört zu einer anderen Dienstadresse. Damit nichts durcheinandergerät, "
                "übernimmt der Abruf nichts. Bitte Otter Bescheid geben. " + nochmal)
    if isinstance(error, ValueError):
        return (f"Der Notizdienst hat Daten geschickt, die nicht ins Archiv passen ({text}). "
                "Zur Sicherheit wurde davon nichts übernommen. Wenn das anhält, bitte Otter Bescheid geben. " + nochmal)
    return f"Der Abruf ist kurz gestolpert ({text}). Nichts ist verloren. " + nochmal


FEHLERARTEN = (OSError, ValueError, RuntimeError, HTTPException, sqlite3.Error)


def main():
    parser = argparse.ArgumentParser(description="PotSchlopf-Textnotizen über HTTPS ins NAS-Archiv übernehmen.")
    parser.add_argument("--ordner", type=Path, default=Path("/archiv"))
    parser.add_argument("--dauerhaft", action="store_true")
    args = parser.parse_args()
    database_path = args.ordner / "potschlopf.sqlite3"
    os.umask(0o077)

    anhalten = threading.Event()

    def bitte_anhalten(*_):
        anhalten.set()

    signal.signal(signal.SIGTERM, bitte_anhalten)
    signal.signal(signal.SIGINT, bitte_anhalten)

    melde("Hallo! Der PotSchlopf-Abruf startet.")

    # Start: Dienstadresse prüfen und Archiv öffnen. Im Dauerbetrieb geduldig wiederholen statt abzustürzen.
    versuch = 0
    while True:
        try:
            origin = settings()
            note_count = prepare_archive(database_path)
            break
        except FEHLERARTEN as error:
            if isinstance(error, ValueError) and any(k in str(error) for k in ("POTSCHLOPF_SERVICE_URL", "HTTPS-Port")):
                hinweis = ("Die Dienstadresse in docker-compose.yml ist nicht gültig. Bitte die Datei "
                           "unverändert aus dem Paket nehmen oder Otter fragen.")
            else:
                hinweis = None
            warte = WARTEZEITEN[min(versuch, len(WARTEZEITEN) - 1)]
            if not args.dauerhaft:
                melde(f"Archivabruf konnte nicht gestartet werden. {hinweis or erklaere(error, warte)}", fehler=True)
                return 1
            melde(f"Archivabruf konnte noch nicht starten. {hinweis or erklaere(error, warte)}", fehler=True)
            versuch += 1
            if anhalten.wait(warte):
                melde("Der PotSchlopf-Abruf hält an. Bis bald!")
                return 0

    unit = "Notiz" if note_count == 1 else "Notizen"
    melde(f"PotSchlopf-Archiv bereit. Es enthält {note_count} {unit}.")
    melde(f"Archivabruf gestartet. Quelle {origin.hostname}"
          + (", Abruf jede Minute." if args.dauerhaft else "."))

    first_success = True
    fehler_seit = None
    letzte_meldung = ("", 0.0)
    versuch = 0
    while True:
        try:
            imported = import_batch(database_path, origin)
        except FEHLERARTEN as error:
            if not args.dauerhaft:
                melde(f"Archivabruf unterbrochen. {erklaere(error, 0).rsplit(' Nächster', 1)[0]}", fehler=True)
                return 1
            warte = WARTEZEITEN[min(versuch, len(WARTEZEITEN) - 1)]
            versuch += 1
            meldung = erklaere(error, warte)
            jetzt = monotonic()
            fehler_seit = fehler_seit or jetzt
            art = type(error).__name__ + str(error)[:40]
            # Dieselbe Störung nicht jede Minute wiederholen, sondern höchstens alle 30 Minuten.
            if art != letzte_meldung[0] or jetzt - letzte_meldung[1] >= ERINNERUNG_SEKUNDEN:
                melde(f"Archivabruf unterbrochen. {meldung}", fehler=True)
                letzte_meldung = (art, jetzt)
        else:
            lebenszeichen()
            if fehler_seit is not None:
                dauer = warte_text(monotonic() - fehler_seit)
                melde(f"Alles wieder gut: Der Abruf klappt wieder (nach etwa {dauer} Pause). Nichts ist verloren gegangen.")
                fehler_seit, letzte_meldung, versuch = None, ("", 0.0), 0
            if imported or first_success or not args.dauerhaft:
                melde(f"Archivabruf erfolgreich. Übernommene Notizen {imported}.")
            first_success = False
            warte = 60
            if not args.dauerhaft:
                return 0
        if anhalten.wait(warte):
            melde("Der PotSchlopf-Abruf hält an. Alle übernommenen Notizen sind sicher gespeichert. Bis bald!")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
