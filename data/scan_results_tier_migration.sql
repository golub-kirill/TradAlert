-- Add the live data-freshness columns (tier + review_reason) to an EXISTING
-- scan_results table created before they were introduced. A fresh DB built from
-- data/scan_schema.sql already has both, so run this ONLY against a pre-tier table.
--
-- The columns must be added together: review_reason is positioned AFTER tier, so
-- tier has to exist first. An earlier version added review_reason alone (AFTER a
-- tier column that did not yet exist) and failed with ERROR 1054 (unknown column
-- 'tier'); every subsequent scan_results INSERT then failed because db.py binds
-- %(tier)s, silently leaving the live journal empty.
--
-- Owner-applied: verify against a schema dump of prod before running. If `tier`
-- already exists (a partial prior apply), drop the first ADD COLUMN line and keep
-- only the review_reason line.
ALTER TABLE scan_results
    ADD COLUMN tier ENUM('LIVE','NEEDS_REVIEW') NOT NULL DEFAULT 'LIVE' AFTER signal_kind,
    ADD COLUMN review_reason VARCHAR(255) NULL AFTER tier;
