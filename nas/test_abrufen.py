from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from abrufen import import_batch, main, save_page, validate_page
from archiv import prepare_archive


def record(sequence=1):
    return {
        "id": f"00000000-0000-4000-8000-{sequence:012d}",
        "folge": sequence,
        "absender": "Maus",
        "inhalt": "Ein Text 🦦",
        "erstellt_utc": "2026-11-19T19:30:00.000Z",
    }


def page(*notes, cursor=1, more=False):
    return {"notizen": list(notes), "naechster_stand": cursor, "weitere": more}


class ArchiveTests(unittest.TestCase):
    def test_continuous_import_reports_empty_start_and_recovery(self):
        class Uhr:
            runden = 0

            def wait(self, sekunden):
                Uhr.runden += 1
                return Uhr.runden >= 4

            def set(self):
                pass

        stdout, stderr = StringIO(), StringIO()
        with (
            TemporaryDirectory() as folder,
            patch("sys.argv", ["abrufen.py", "--ordner", folder, "--dauerhaft"]),
            patch.dict(os.environ, {"POTSCHLOPF_SERVICE_URL": "https://potschlopf.example"}),
            patch("abrufen.os.umask"),
            patch("abrufen.signal.signal"),
            patch("abrufen.threading.Event", Uhr),
            patch("abrufen.LEBENSZEICHEN", Path(folder) / "ok"),
            patch("abrufen.import_batch", side_effect=[0, 0, OSError("Zeitüberschreitung"), 0]),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            self.assertEqual(main(), 0)

        ausgabe = stdout.getvalue()
        self.assertEqual(ausgabe.count("Archivabruf erfolgreich. Übernommene Notizen 0."), 1)
        self.assertIn("Alles wieder gut", ausgabe)
        fehler = stderr.getvalue().splitlines()
        self.assertEqual(len(fehler), 1)
        self.assertTrue(fehler[0].startswith("Archivabruf unterbrochen."))
        self.assertIn("Zeitüberschreitung", fehler[0])

    def test_continuous_import_waits_for_archive_to_be_available(self):
        stdout, stderr = StringIO(), StringIO()
        with TemporaryDirectory() as folder:
            archive = Path(folder) / "archiv"

            class Uhr:
                # Erst beim ersten Warten taucht der Archivordner auf (wie ein spät eingehängtes Volume).
                def wait(self, sekunden):
                    if archive.exists():
                        return True
                    archive.mkdir()
                    return False

                def set(self):
                    pass

            with (
                patch("sys.argv", ["abrufen.py", "--ordner", str(archive), "--dauerhaft"]),
                patch.dict(os.environ, {"POTSCHLOPF_SERVICE_URL": "https://potschlopf.example"}),
                patch("abrufen.os.umask"),
                patch("abrufen.signal.signal"),
                patch("abrufen.threading.Event", Uhr),
                patch("abrufen.LEBENSZEICHEN", Path(folder) / "ok"),
                patch("abrufen.fetch_page", return_value=([], 0, False)),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                self.assertEqual(main(), 0)

            self.assertEqual(prepare_archive(archive / "potschlopf.sqlite3"), 0)
        self.assertIn("Archivabruf konnte noch nicht starten.", stderr.getvalue())
        self.assertIn("Archivabruf erfolgreich. Übernommene Notizen 0.", stdout.getvalue())

    def test_rejects_malformed_payload_before_storage(self):
        for payload in (
            page(record(2), record(1), cursor=1),
            page({**record(), "inhalt": "ä" * 2049}),
            page({**record(), "folge": True}),
            page(record(), cursor=2),
            page(record(), more=True),
            page({**record(), "erstellt_utc": "kein Datum"}),
        ):
            with self.subTest(payload_type=type(payload)):
                with self.assertRaises(ValueError):
                    validate_page(payload, 0)

    def test_cursor_and_notes_commit_together_and_retries_are_consistent(self):
        with TemporaryDirectory() as folder:
            database = Path(folder) / "potschlopf.sqlite3"
            self.assertEqual(prepare_archive(database), 0)
            origin = urlsplit("https://potschlopf.example")
            with patch("abrufen.fetch_page", return_value=validate_page(page(record()), 0)) as fetch:
                self.assertEqual(import_batch(database, origin), 1)
                fetch.assert_called_once_with(origin, 0)
            with patch("abrufen.fetch_page", return_value=validate_page(page(cursor=1), 1)) as fetch:
                self.assertEqual(import_batch(database, origin), 0)
                fetch.assert_called_once_with(origin, 1)

            replayed = {**record(), "folge": 2}
            self.assertEqual(save_page(database, origin.netloc, 1, validate_page(page(replayed, cursor=2), 1)), 0)

            conflicting = {**record(), "folge": 4, "inhalt": "Abweichender Inhalt"}
            with self.assertRaises(sqlite3.IntegrityError):
                save_page(database, origin.netloc, 2, validate_page(page(record(3), conflicting, cursor=4), 2))

            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT inhalt FROM notizen").fetchall(), [("Ein Text 🦦",)])
                self.assertEqual(db.execute("SELECT folge FROM abrufstand").fetchone(), (2,))
            with self.assertRaises(ValueError):
                import_batch(database, urlsplit("https://anderer-dienst.example"))
            self.assertEqual(prepare_archive(database), 1)


class FreundlicheMeldungenTests(unittest.TestCase):
    def test_messages_name_cause_and_next_step(self):
        import socket
        from abrufen import erklaere
        faelle = {
            socket.gaierror(-2, "Name or service not known"): "nicht erreichbar",
            RuntimeError("Notizdienst antwortet mit HTTP 503."): "Problem",
            sqlite3.OperationalError("database or disk is full"): "voll",
            sqlite3.OperationalError("attempt to write a readonly database"): "stoppen und wieder starten",
            ValueError("Ungültige Archivseite."): "nichts übernommen",
            ValueError("Das Archiv enthält einen Abrufstand für einen anderen Dienst."): "anderen Dienstadresse",
        }
        for fehler, erwartet in faelle.items():
            with self.subTest(fehler=fehler):
                text = erklaere(fehler, 120)
                self.assertIn(erwartet, text)
                self.assertIn("Nächster Versuch in 2 Minuten.", text)


class GeduldigerAbrufTests(unittest.TestCase):
    def run_main(self, ergebnisse, runden):
        import io
        import abrufen

        class Uhr:
            def __init__(self):
                self.runden = 0

            def wait(self, sekunden):
                self.runden += 1
                return self.runden >= runden

            def set(self):
                pass

        aufrufe = iter(ergebnisse)

        def import_batch(*_):
            ergebnis = next(aufrufe)
            if isinstance(ergebnis, Exception):
                raise ergebnis
            return ergebnis

        ausgabe, fehler = io.StringIO(), io.StringIO()
        with TemporaryDirectory() as folder, \
                patch.dict("os.environ", {"POTSCHLOPF_SERVICE_URL": "https://potschlopf.example"}), \
                patch("sys.argv", ["abrufen.py", "--dauerhaft", "--ordner", folder]), \
                patch("abrufen.threading.Event", Uhr), \
                patch("abrufen.signal.signal"), \
                patch("abrufen.LEBENSZEICHEN", Path(folder) / "ok"), \
                patch("abrufen.import_batch", import_batch), \
                patch("sys.stdout", ausgabe), patch("sys.stderr", fehler):
            self.assertEqual(abrufen.main(), 0)
        return ausgabe.getvalue(), fehler.getvalue()

    def test_keeps_going_after_outage_and_reports_recovery_once(self):
        stoerung = OSError(101, "Network is unreachable")
        aus, err = self.run_main([0, stoerung, stoerung, stoerung, 2], runden=5)
        self.assertEqual(err.count("Archivabruf unterbrochen."), 1)
        self.assertIn("Alles wieder gut", aus)
        self.assertIn("Übernommene Notizen 2.", aus)
        self.assertIn("Bis bald!", aus)

    def test_bad_service_address_waits_instead_of_crashing(self):
        import io
        import abrufen

        class Uhr:
            def wait(self, sekunden):
                return True

            def set(self):
                pass

        fehler = io.StringIO()
        with patch.dict("os.environ", {"POTSCHLOPF_SERVICE_URL": "http://unsicher.example"}), \
                patch("sys.argv", ["abrufen.py", "--dauerhaft"]), \
                patch("abrufen.threading.Event", Uhr), patch("abrufen.signal.signal"), \
                patch("sys.stdout", io.StringIO()), patch("sys.stderr", fehler):
            self.assertEqual(abrufen.main(), 0)
        self.assertIn("Dienstadresse in docker-compose.yml", fehler.getvalue())


if __name__ == "__main__":
    unittest.main()
