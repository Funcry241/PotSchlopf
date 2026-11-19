CREATE TABLE notizen (
    folge INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    absender TEXT NOT NULL,
    inhalt TEXT NOT NULL CHECK (length(CAST(inhalt AS BLOB)) BETWEEN 1 AND 4096),
    erstellt_utc TEXT NOT NULL,
    erstellt_s INTEGER NOT NULL
);
--> statement-breakpoint
CREATE INDEX notizen_erstellt ON notizen (erstellt_s);
