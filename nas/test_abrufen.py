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
        stdout, stderr = StringIO(), StringIO()
        with (
            TemporaryDirectory() as folder,
            patch("sys.argv", ["abrufen.py", "--ordner", folder, "--dauerhaft"]),
            patch.dict(os.environ, {"POTSCHLOPF_SERVICE_URL": "https://potschlopf.example"}),
            patch("abrufen.os.umask"),
            patch("abrufen.import_batch", side_effect=[0, 0, OSError("Zeitüberschreitung"),
                                                     OSError("Zeitüberschreitung"), 0]),
            patch("abrufen.sleep", side_effect=[None, None, None, None, KeyboardInterrupt]),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            with self.assertRaises(KeyboardInterrupt):
                main()

        success = "Archivabruf erfolgreich. Übernommene Notizen 0."
        self.assertEqual(stdout.getvalue().splitlines(), ["PotSchlopf-Archiv bereit.", success, success])
        self.assertEqual(stderr.getvalue().splitlines(), [
            "PotSchlopf wartet. Der Notizabruf ist gerade unterbrochen. "
            "(Zeitüberschreitung) Nächster Versuch in 60 Sekunden.",
        ])

    def test_continuous_import_waits_for_archive_to_be_available(self):
        stdout, stderr = StringIO(), StringIO()
        with TemporaryDirectory() as folder:
            archive = Path(folder) / "archiv"

            def wait_for_mount(_seconds):
                if archive.exists():
                    raise KeyboardInterrupt
                archive.mkdir()

            with (
                patch("sys.argv", ["abrufen.py", "--ordner", str(archive), "--dauerhaft"]),
                patch.dict(os.environ, {"POTSCHLOPF_SERVICE_URL": "https://potschlopf.example"}),
                patch("abrufen.os.umask"),
                patch("abrufen.fetch_page", return_value=([], 0, False)),
                patch("abrufen.sleep", side_effect=wait_for_mount),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    main()

            self.assertEqual(prepare_archive(archive / "potschlopf.sqlite3"), 0)
        self.assertIn("Das Archiv ist gerade nicht verfügbar.", stderr.getvalue())
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


if __name__ == "__main__":
    unittest.main()
