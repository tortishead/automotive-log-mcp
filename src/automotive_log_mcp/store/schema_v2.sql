-- Schema version 2 (M2): artifacts. Applied by store/db.py after schema.sql (version 1).
-- Version 1 defined the artifact tables but never wrote them, so they are recreated here.

DROP TABLE IF EXISTS artifact_fts;
DROP TABLE IF EXISTS artifact_sections;
DROP TABLE IF EXISTS artifacts;

-- Tombstones, ANR traces, Java crashes, dropbox entries. Citation art:<id>[/section][:L<line>]
-- (D6): <line> is 1-based over the whole artifact text (sections are contiguous), so
-- 'art:7/backtrace:L14' and 'art:7:L14' name the same line and the section is a check.
CREATE TABLE artifacts (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL,             -- origin source: holds the artifact's record
    record_id   INTEGER NOT NULL,             -- its record on the timeline (synthetic or the
                                              -- line it was parsed from, e.g. a crash-buffer crash)
    type        TEXT NOT NULL,                -- tombstone | anr | java_crash | dropbox
    name        TEXT NOT NULL,                -- display name: file[!member][#section][@n]
    path        TEXT NOT NULL,                -- original file
    sha256      TEXT NOT NULL,                -- of the original file
    member      TEXT,
    ts          INTEGER,                      -- ns, wall clock as written (like records.ts)
    ts_raw      TEXT,                         -- as written, if the artifact has its own time
    pid         INTEGER,
    tid         INTEGER,
    process     TEXT,                         -- redacted
    signature   TEXT,                         -- redacted; groups artifacts of the same crash
    headline    TEXT NOT NULL,                -- redacted; one line (the record's message)
    summary     TEXT NOT NULL,                -- redacted; a few lines
    fields      TEXT,                         -- JSON, redacted
    lines       INTEGER NOT NULL              -- lines of text over all sections
);
CREATE INDEX artifacts_type ON artifacts(type, signature);
CREATE INDEX artifacts_source ON artifacts(source_id);
CREATE INDEX artifacts_ts ON artifacts(ts);

CREATE TABLE artifact_sections (
    id          INTEGER PRIMARY KEY,
    artifact_id INTEGER NOT NULL,
    idx         INTEGER NOT NULL,             -- order, 0-based
    name        TEXT NOT NULL,                -- unique per artifact, [a-z0-9_.-]
    first_line  INTEGER NOT NULL,             -- 1-based line of the artifact where it starts
    lines       INTEGER NOT NULL,
    text        TEXT NOT NULL,                -- redacted, lines joined by '\n'
    UNIQUE (artifact_id, idx),
    UNIQUE (artifact_id, name)
);

CREATE VIRTUAL TABLE artifact_fts USING fts5(
    text, content='artifact_sections', content_rowid='id'
);
