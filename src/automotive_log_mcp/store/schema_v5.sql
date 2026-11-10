-- Schema version 5: the clock model (M5). Records keep ts/ts_src as written; the case clock (one
-- timeline over all sources) is computed per segment by core/align.py and read by core/clock.py:
--   case time = native + offset_ns + round(drift_ppm * 1e-6 * (native - start))
-- where native is ts (wall-clock segments) or ts_src (monotonic segments), for native >= start
-- (the first piece of a segment also covers earlier times). Rows are recomputed after every
-- import; clock_manual keeps what a user set with the align tool.

CREATE TABLE IF NOT EXISTS clock (
    source_id INTEGER NOT NULL,
    seg       INTEGER NOT NULL,
    start     INTEGER NOT NULL,              -- native time from which this piece applies
    offset_ns INTEGER NOT NULL,
    drift_ppm REAL NOT NULL DEFAULT 0,
    native    TEXT NOT NULL,                 -- wall | mono: which record time is mapped
    conf      TEXT NOT NULL,                 -- exact | manual | paired | marker | estimate | assumed
    method    TEXT NOT NULL,                 -- how it was found, for list_sources and align
    error_ns  INTEGER,                       -- bound on the error, null if unknown
    anchors   INTEGER NOT NULL DEFAULT 0,    -- pairs the piece was fitted to
    via       INTEGER,                       -- source the anchors pair with
    PRIMARY KEY (source_id, seg, start)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS clock_manual (
    id        INTEGER PRIMARY KEY,
    kind      TEXT NOT NULL,                 -- reference | offset | anchor
    source_id INTEGER NOT NULL,
    seg       INTEGER,                       -- offset: one segment, null for all
    offset_ns INTEGER,                       -- offset: case = native + offset; anchor: ref - at
    at_id     INTEGER,                       -- anchor: record ids that happened at the same moment
    ref_id    INTEGER,
    added_at  TEXT NOT NULL
);
