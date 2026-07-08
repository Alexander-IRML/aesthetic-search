PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS artists (
    artist_id      TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    folder_name    TEXT NOT NULL UNIQUE,
    source_url     TEXT,
    notes          TEXT,
    date_added     TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artworks (
    artwork_id      TEXT PRIMARY KEY,
    artist_id       TEXT NOT NULL REFERENCES artists(artist_id),
    raw_path        TEXT NOT NULL,
    processed_path  TEXT,
    source_platform TEXT DEFAULT 'manual',
    source_id       TEXT,
    orig_width      INTEGER,
    orig_height     INTEGER,
    file_hash       TEXT,
    phash           TEXT,
    is_sfw          INTEGER DEFAULT 1,
    validated       INTEGER DEFAULT 0,
    review_status   TEXT NOT NULL DEFAULT 'unreviewed'
        CHECK (review_status IN (
            'unreviewed',
            'confirmed_unique',
            'confirmed_duplicate',
            'confirmed_variant'
        )),
    duplicate_of    TEXT REFERENCES artworks(artwork_id),

    scale_factor    REAL,
    pad_left        INTEGER DEFAULT 0,
    pad_top         INTEGER DEFAULT 0,
    pad_right       INTEGER DEFAULT 0,
    pad_bottom      INTEGER DEFAULT 0,
    crop_left       INTEGER DEFAULT 0,
    crop_top        INTEGER DEFAULT 0,
    crop_right      INTEGER DEFAULT 0,
    crop_bottom     INTEGER DEFAULT 0,

    date_added      TEXT DEFAULT CURRENT_TIMESTAMP,
    notes           TEXT,

    UNIQUE(source_platform, source_id)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    script_name      TEXT NOT NULL,
    started_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    finished_at      TEXT,
    images_processed INTEGER DEFAULT 0,
    images_skipped   INTEGER DEFAULT 0,
    errors_count     INTEGER DEFAULT 0,
    notes            TEXT
);

CREATE TABLE IF NOT EXISTS run_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER REFERENCES runs(run_id),
    level       TEXT NOT NULL CHECK (level IN ('info', 'warning', 'error')),
    event_type  TEXT NOT NULL,
    raw_path    TEXT,
    artwork_id  TEXT REFERENCES artworks(artwork_id),
    message     TEXT NOT NULL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_artworks_artist ON artworks(artist_id);
CREATE INDEX IF NOT EXISTS idx_artworks_file_hash ON artworks(file_hash);
CREATE INDEX IF NOT EXISTS idx_artworks_phash ON artworks(phash);
CREATE INDEX IF NOT EXISTS idx_artworks_review_status ON artworks(review_status);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id);