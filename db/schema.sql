CREATE TABLE IF NOT EXISTS scholarships (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  provider TEXT,
  official_url TEXT,
  application_url TEXT,
  source_type TEXT,
  amount_text TEXT,
  amount_min INTEGER,
  amount_max INTEGER,
  benefit_type TEXT,
  eligibility_text TEXT,
  education_level TEXT,
  courses TEXT,
  academic_req TEXT,
  income_limit INTEGER,
  age_min INTEGER,
  age_max INTEGER,
  gender TEXT,
  categories TEXT,
  domicile_states TEXT,
  institution_req TEXT,
  documents_required TEXT,
  selection_process TEXT,
  renewal_req TEXT,
  opening_date TEXT,
  closing_date TEXT,
  status TEXT DEFAULT 'ACTIVE',
  verification_label TEXT DEFAULT 'REVIEW REQUIRED',
  confidence REAL DEFAULT 0.0,
  score_breakdown TEXT,
  first_seen TEXT,
  last_seen TEXT,
  last_verified TEXT,
  content_hash TEXT
);

CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scholarship_id INTEGER REFERENCES scholarships(id),
  field TEXT NOT NULL,
  value TEXT,
  quote TEXT,
  source_url TEXT,
  snapshot_id INTEGER,
  extractor TEXT,
  grounded INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  http_status INTEGER,
  content_hash TEXT,
  text_path TEXT,
  run_id INTEGER
);

CREATE TABLE IF NOT EXISTS change_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scholarship_id INTEGER REFERENCES scholarships(id),
  field TEXT,
  old_value TEXT,
  new_value TEXT,
  detected_at TEXT,
  source_url TEXT,
  evidence_quote TEXT,
  change_type TEXT
);

CREATE TABLE IF NOT EXISTS crawl_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT,
  finished_at TEXT,
  discovered INTEGER DEFAULT 0,
  new INTEGER DEFAULT 0,
  changed INTEGER DEFAULT 0,
  expired INTEGER DEFAULT 0,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS candidates (
  url TEXT PRIMARY KEY,
  discovered_via TEXT,
  source_class TEXT,
  score REAL DEFAULT 0,
  status TEXT DEFAULT 'PENDING',
  run_id INTEGER
);
