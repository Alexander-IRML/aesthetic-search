PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS artists (
    artist_id      TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    folder_name    TEXT NOT NULL UNIQUE,
    source_platform TEXT DEFAULT 'manual',
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
    is_sfw          INTEGER,
    demo_eligible   INTEGER NOT NULL DEFAULT 0
        CHECK (demo_eligible IN (0, 1)),
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

CREATE TABLE IF NOT EXISTS embeddings (
    artwork_id            TEXT PRIMARY KEY REFERENCES artworks(artwork_id),
    clip_vector           BLOB,
    clip_dim              INTEGER,
    dino_pooled           BLOB,
    dino_pooled_dim       INTEGER,
    dino_patches          BLOB,
    dino_patch_grid_size  INTEGER,
    dino_patch_dim        INTEGER,
    model_name_clip       TEXT,
    model_version_clip    TEXT,
    model_name_dino       TEXT,
    model_version_dino    TEXT,
    date_computed         TEXT DEFAULT CURRENT_TIMESTAMP
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

CREATE TABLE IF NOT EXISTS artwork_filter_decisions (
    decision_key       TEXT PRIMARY KEY,
    candidate_id       TEXT NOT NULL,
    author_did         TEXT,
    post_uri           TEXT,
    image_index        INTEGER,
    image_sha256       TEXT,
    decision           TEXT NOT NULL
        CHECK (decision IN ('accept', 'review', 'reject', 'error')),
    predicted_class    TEXT NOT NULL,
    accepted_for_main_corpus INTEGER NOT NULL,
    route              TEXT NOT NULL,
    final_score        REAL NOT NULL,
    confidence         REAL NOT NULL,
    reason_codes_json  TEXT NOT NULL,
    candidate_json     TEXT NOT NULL,
    evidence_json      TEXT NOT NULL,
    model_id           TEXT NOT NULL,
    model_revision     TEXT,
    config_version     TEXT NOT NULL,
    config_hash        TEXT NOT NULL,
    prompt_version     TEXT,
    classifier_version TEXT,
    software_version   TEXT NOT NULL,
    processed_at       TEXT NOT NULL,
    duration_ms        REAL NOT NULL,
    error_type         TEXT,
    error_message      TEXT,
    date_added         TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS artwork_filter_routes (
    route_key       TEXT PRIMARY KEY,
    decision_key    TEXT NOT NULL REFERENCES artwork_filter_decisions(decision_key),
    candidate_id    TEXT NOT NULL,
    target          TEXT NOT NULL CHECK (target IN ('corpus', 'review')),
    status          TEXT NOT NULL CHECK (status IN ('stored', 'duplicate', 'error')),
    local_path      TEXT,
    image_sha256    TEXT,
    perceptual_hash TEXT,
    width           INTEGER,
    height          INTEGER,
    artwork_id      TEXT REFERENCES artworks(artwork_id),
    error_type      TEXT,
    error_message   TEXT,
    routed_at       TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(decision_key)
);

CREATE TABLE IF NOT EXISTS artwork_objects (
    artwork_id      TEXT NOT NULL REFERENCES artworks(artwork_id),
    role            TEXT NOT NULL CHECK (role IN ('original')),
    object_key      TEXT NOT NULL,
    object_uri      TEXT NOT NULL,
    content_sha256  TEXT NOT NULL,
    byte_size       INTEGER NOT NULL,
    etag            TEXT,
    published_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    verified_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (artwork_id, role)
);

CREATE TABLE IF NOT EXISTS vector_index_points (
    collection_name TEXT NOT NULL,
    artwork_id      TEXT NOT NULL REFERENCES artworks(artwork_id),
    point_id        TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    indexed_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (collection_name, artwork_id),
    UNIQUE (collection_name, point_id)
);

CREATE INDEX IF NOT EXISTS idx_artworks_artist ON artworks(artist_id);
CREATE INDEX IF NOT EXISTS idx_artworks_file_hash ON artworks(file_hash);
CREATE INDEX IF NOT EXISTS idx_artworks_phash ON artworks(phash);
CREATE INDEX IF NOT EXISTS idx_artworks_review_status ON artworks(review_status);
CREATE INDEX IF NOT EXISTS idx_artworks_demo_policy
    ON artworks(demo_eligible, is_sfw, validated);
CREATE INDEX IF NOT EXISTS idx_embeddings_models
    ON embeddings(model_name_dino, model_version_dino, model_name_clip, model_version_clip);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id);
CREATE INDEX IF NOT EXISTS idx_filter_decisions_candidate
    ON artwork_filter_decisions(candidate_id, processed_at);
CREATE INDEX IF NOT EXISTS idx_filter_decisions_outcome
    ON artwork_filter_decisions(decision, predicted_class);
CREATE INDEX IF NOT EXISTS idx_filter_routes_candidate
    ON artwork_filter_routes(candidate_id, target, status);
CREATE INDEX IF NOT EXISTS idx_artwork_objects_hash
    ON artwork_objects(content_sha256);
CREATE INDEX IF NOT EXISTS idx_vector_index_points_artwork
    ON vector_index_points(artwork_id);
