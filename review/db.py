"""SQLite family database. One file, plain schema, every table documented here."""
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from .paths import DB_PATH, DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS families (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  display_name TEXT NOT NULL,          -- "Garcia, Maria" from the latest application
  first_season TEXT, last_season TEXT,
  seasons_served INTEGER NOT NULL DEFAULT 0,   -- continuity signal, updated when an app is accepted
  trust INTEGER NOT NULL DEFAULT 0,    -- +1 per accepted season, -1 per confirmed problem
  notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS applications (
  id TEXT PRIMARY KEY,                 -- confirmation code for web, JF-<submission id> for imports
  season TEXT NOT NULL,
  source TEXT NOT NULL,                -- web | jotform | manual
  submitted_at TEXT NOT NULL,
  lang TEXT NOT NULL DEFAULT 'en',
  family_id TEXT REFERENCES families(id),
  status TEXT NOT NULL DEFAULT 'new',  -- new | matched | accepted | declined | duplicate | out_of_area | hold
  payload TEXT NOT NULL,               -- plaintext JSON, schema version in payload.version
  norm TEXT NOT NULL,                  -- normalized keys used for matching (JSON)
  server_status TEXT,                  -- last status pushed to the worker
  updated_at TEXT,
  supersedes TEXT,                     -- id of the earlier application this one replaces (an edit)
  note TEXT NOT NULL DEFAULT '',       -- short public note for the applicant, shown on the status page (no applicant data)
  server_note TEXT                     -- last note pushed to the worker
);
CREATE INDEX IF NOT EXISTS applications_season ON applications(season, submitted_at);
CREATE TABLE IF NOT EXISTS candidates (
  id TEXT PRIMARY KEY,
  app_a TEXT NOT NULL REFERENCES applications(id),
  app_b TEXT NOT NULL REFERENCES applications(id),
  score REAL NOT NULL,
  reasons TEXT NOT NULL,               -- JSON list of strings
  verdict TEXT,                        -- same | different | unsure
  decided_by TEXT,                     -- rule | llm | human
  judge TEXT,                          -- JSON: the judge's full answer
  decided_at TEXT,
  UNIQUE(app_a, app_b)
);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                  -- review_match | resolve_duplicate | verify_address | contact_applicant | review_notes
  app_id TEXT REFERENCES applications(id),
  candidate_id TEXT REFERENCES candidates(id),
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open', -- open | done | dismissed
  created_at TEXT NOT NULL,
  resolved_at TEXT, resolution TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL, kind TEXT NOT NULL, ref TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def connect(path=DB_PATH):
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(applications)")}
    if "supersedes" not in cols:
        con.execute("ALTER TABLE applications ADD COLUMN supersedes TEXT")
    if "note" not in cols:
        con.execute("ALTER TABLE applications ADD COLUMN note TEXT NOT NULL DEFAULT ''")
        con.execute("ALTER TABLE applications ADD COLUMN server_note TEXT")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def get_meta(con, key, default=None):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_meta(con, key, value):
    con.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def log(con, kind, ref="", detail=""):
    con.execute("INSERT INTO events(at,kind,ref,detail) VALUES(?,?,?,?)", (now(), kind, ref, detail))


def app_row(r):
    d = dict(r)
    d["payload"] = json.loads(d["payload"])
    d["norm"] = json.loads(d["norm"])
    return d


def add_task(con, kind, title, app_id=None, candidate_id=None, detail=""):
    """One open task per (kind, app, candidate). A task that points at nothing
    (decrypt errors, preview rows that could not be read) is identified by its
    kind, title and detail instead, so one such task per subject, not one per kind."""
    if app_id is None and candidate_id is None:
        exists = con.execute("SELECT id FROM tasks WHERE kind=? AND title=? AND detail=? AND status='open'", (kind, title, detail)).fetchone()
    else:
        exists = con.execute("SELECT id FROM tasks WHERE kind=? AND IFNULL(app_id,'')=IFNULL(?,'') AND IFNULL(candidate_id,'')=IFNULL(?,'') AND status='open'",
                             (kind, app_id, candidate_id)).fetchone()
    if exists:
        return exists["id"]
    tid = new_id("task")
    con.execute("INSERT INTO tasks(id,kind,app_id,candidate_id,title,detail,status,created_at) VALUES(?,?,?,?,?,?,'open',?)",
                (tid, kind, app_id, candidate_id, title, detail, now()))
    return tid
