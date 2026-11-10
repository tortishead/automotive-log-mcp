-- Schema version 3: the record index also holds the words inside CamelCase identifiers
-- (core/words.py), so 'Delegate' finds 'EsoCarDelegateDisplayAreaPolicyProvider'.
-- The content view calls camel_parts(), a function store/db.py registers on every connection.
-- Applied to an existing case DB, the index is rebuilt from the records (no re-import needed).

DROP TABLE IF EXISTS records_fts;
CREATE VIEW records_fts_v AS SELECT id, msg, tag, camel_parts(msg, tag) AS parts FROM records_v;
CREATE VIRTUAL TABLE records_fts USING fts5(
    msg, tag, parts, content='records_fts_v', content_rowid='id', columnsize=0
);
INSERT INTO records_fts(records_fts) VALUES ('rebuild');
