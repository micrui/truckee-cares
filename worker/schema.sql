-- Encrypted inbox. Plaintext never touches this database.
CREATE TABLE IF NOT EXISTS submissions (
  id          TEXT PRIMARY KEY,           -- confirmation code shown to the applicant, e.g. TCC-26-7K3QM
  season      TEXT NOT NULL,
  lang        TEXT NOT NULL DEFAULT 'en',
  created_at  TEXT NOT NULL,              -- ISO 8601 UTC, server clock
  ciphertext  TEXT NOT NULL,              -- age armored, encrypted to config.recipients
  status      TEXT NOT NULL DEFAULT 'new',-- new | fetched | accepted | declined | duplicate | out_of_area | superseded
  updated_at  TEXT,
  supersedes  TEXT                        -- id of the earlier submission this one replaces (an edit)
);
CREATE INDEX IF NOT EXISTS submissions_season ON submissions (season, created_at);
-- Existing databases: ALTER TABLE submissions ADD COLUMN supersedes TEXT;
