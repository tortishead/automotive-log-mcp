-- Case DB schema, version 1. One file per case (D3, D7). Applied by store/db.py on open.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) WITHOUT ROWID;

-- One log stream. A bugreport expands into several sources that share path and sha256.
CREATE TABLE IF NOT EXISTS sources (
    id                INTEGER PRIMARY KEY,
    name              TEXT NOT NULL,           -- display name: file[!member][#section][:stream]
    format            TEXT NOT NULL,           -- connector name
    connector_version TEXT NOT NULL,
    redaction_hash    TEXT NOT NULL,
    path              TEXT NOT NULL,           -- original file (never copied or modified)
    sha256            TEXT NOT NULL,
    size              INTEGER NOT NULL,
    member            TEXT,                    -- archive member / section inside the original
    stream            TEXT,                    -- buffer or stream name (main, system, crash, kernel, ...)
    status            TEXT NOT NULL,           -- indexing | done | failed
    error             TEXT,
    records           INTEGER NOT NULL DEFAULT 0,
    lines             INTEGER NOT NULL DEFAULT 0,  -- physical lines read
    unparsed          INTEGER NOT NULL DEFAULT 0,  -- records with level NULL from unparseable lines
    ts_min            INTEGER,
    ts_max            INTEGER,
    ts_src_min        INTEGER,
    ts_src_max        INTEGER,
    fingerprint       TEXT,
    meta              TEXT,                    -- JSON, redacted
    added_at          TEXT NOT NULL,
    finished_at       TEXT
);
CREATE INDEX IF NOT EXISTS sources_file ON sources(sha256, path);

CREATE TABLE IF NOT EXISTS segments (
    source_id    INTEGER NOT NULL,
    seg          INTEGER NOT NULL,
    reason       TEXT NOT NULL,               -- start | boot | monotonic_reset | wall_jump_back
    first_seq    INTEGER NOT NULL,
    last_seq     INTEGER NOT NULL,
    count        INTEGER NOT NULL,
    ts_first     INTEGER,
    ts_last      INTEGER,
    ts_src_first INTEGER,
    ts_src_last  INTEGER,
    PRIMARY KEY (source_id, seg)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS templates (
    id    INTEGER PRIMARY KEY,
    text  TEXT NOT NULL UNIQUE,
    count INTEGER NOT NULL DEFAULT 0
);

-- Tags are interned: records keep tag_id (small) and tag filters resolve globs on this table.
CREATE TABLE IF NOT EXISTS tags (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

-- id = source_id << 40 | seq, so the citation log:<source>#<seq> is the rowid and
-- (source_id, seq) needs no separate index.
CREATE TABLE IF NOT EXISTS records (
    id          INTEGER PRIMARY KEY,
    ts          INTEGER,                      -- ns, wall clock as written (null if unknown)
    ts_src      INTEGER,                      -- ns in the source's own clock; null when equal to ts
    ts_raw      TEXT,                         -- as written; null when the source's ts_format
                                              -- reproduces it from ts/ts_src (see core/timeparse)
    level       TEXT,                         -- V D I W E F, null if unknown
    tag_id      INTEGER,                      -- tags.id
    pid         INTEGER,
    tid         INTEGER,
    msg         TEXT NOT NULL,                -- redacted
    template_id INTEGER,
    seg         INTEGER,
    attrs       TEXT,                         -- JSON, redacted; null when empty
    source_id   INTEGER GENERATED ALWAYS AS (id >> 40) VIRTUAL,
    seq         INTEGER GENERATED ALWAYS AS (id & 1099511627775) VIRTUAL
);
CREATE INDEX IF NOT EXISTS records_ts ON records(ts);
CREATE INDEX IF NOT EXISTS records_src_ts ON records(source_id, ts_src) WHERE ts_src IS NOT NULL;
CREATE INDEX IF NOT EXISTS records_tag ON records(tag_id);
CREATE INDEX IF NOT EXISTS records_pid ON records(pid);
CREATE INDEX IF NOT EXISTS records_template ON records(template_id);
-- Partial: queries for a minimum level of W or above add "level IN ('W','E','F')" to use it.
CREATE INDEX IF NOT EXISTS records_level ON records(level) WHERE level IN ('W', 'E', 'F');

-- Records with their tag text: what the tools read, and the FTS content.
CREATE VIEW IF NOT EXISTS records_v AS
    SELECT r.id, r.source_id, r.seq, r.ts, coalesce(r.ts_src, r.ts) AS ts_src, r.ts_raw, r.level,
           t.name AS tag, r.pid, r.tid, r.msg, r.template_id, r.seg, r.attrs
    FROM records r LEFT JOIN tags t ON t.id = r.tag_id;

CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(
    msg, tag, content='records_v', content_rowid='id', columnsize=0
);

-- Artifacts (filled from M2): tombstones, ANR traces, Java crashes, dropbox entries.
CREATE TABLE IF NOT EXISTS artifacts (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER,                      -- origin source of its synthetic record
    type        TEXT NOT NULL,
    name        TEXT NOT NULL,
    path        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    member      TEXT,
    ts          INTEGER,
    ts_raw      TEXT,
    pid         INTEGER,
    process     TEXT,
    signature   TEXT,
    summary     TEXT,
    fields      TEXT,                         -- JSON
    record_id   INTEGER                       -- the synthetic record
);
CREATE INDEX IF NOT EXISTS artifacts_type ON artifacts(type, signature);

CREATE TABLE IF NOT EXISTS artifact_sections (
    id          INTEGER PRIMARY KEY,
    artifact_id INTEGER NOT NULL,
    idx         INTEGER NOT NULL,
    name        TEXT NOT NULL,
    text        TEXT NOT NULL,                -- redacted
    UNIQUE (artifact_id, idx)
);

CREATE VIRTUAL TABLE IF NOT EXISTS artifact_fts USING fts5(
    text, content='artifact_sections', content_rowid='id'
);

-- Process list from bugreports (pid -> name), keyed by the original file.
CREATE TABLE IF NOT EXISTS processes (
    sha256 TEXT NOT NULL,
    pid    INTEGER NOT NULL,
    tid    INTEGER,
    ppid   INTEGER,
    user   TEXT,
    name   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS processes_pid ON processes(sha256, pid);
