from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from abrufen import import_batch, save_page, validate_page
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

            duplicate = {**record(2), "id": record()["id"]}
            with self.assertRaises(sqlite3.IntegrityError):
                save_page(database, origin.netloc, 1, validate_page(page(record(2), {**duplicate, "folge": 3}, cursor=3), 1))

            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT inhalt FROM notizen").fetchall(), [("Ein Text 🦦",)])
                self.assertEqual(db.execute("SELECT folge FROM abrufstand").fetchone(), (1,))
            with self.assertRaises(ValueError):
                import_batch(database, urlsplit("https://anderer-dienst.example"))
            self.assertEqual(prepare_archive(database), 1)


if __name__ == "__main__":
    unittest.main()
