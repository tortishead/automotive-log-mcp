-- Schema version 6: the clock model of version 5 (case clock, alignment) is removed.

DROP TABLE IF EXISTS clock;
DROP TABLE IF EXISTS clock_manual;
DELETE FROM meta WHERE key LIKE 'clock_%';
