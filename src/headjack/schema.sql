CREATE TABLE artifact (
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL,   -- skill | runbook | script | ticket
  name          TEXT NOT NULL,   -- cassandra-operator
  repo          TEXT,            -- dev.azure.com/.../k8s-k8ssandra
  path          TEXT,            -- docs/runbooks/cassandra-operations.md
  git_sha       TEXT,            -- pin: what we indexed
  version       TEXT,            -- semver for skills
  content_hash  TEXT NOT NULL,   -- sha256, integrity + change detection
  scope         TEXT NOT NULL,   -- personal | team | org
  previous_scope TEXT,           -- scope before a promotion (P2-08); NULL = never promoted
  owner         TEXT,
  reviewed_by   TEXT,            -- NULL = unreviewed, see §06
  systems       TEXT,            -- json: ["cassandra","k8ssandra"]
  description   TEXT,            -- frontmatter description, for the Tank mirror
  body          TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  signature     TEXT             -- ed25519:<keyid>:<base64sig> over body, NULL if unsigned (P4-01)
);

-- chunks are what we actually search; artifacts are what we ship
CREATE TABLE chunk (
  id           INTEGER PRIMARY KEY,
  artifact_id  INTEGER NOT NULL REFERENCES artifact(id) ON DELETE CASCADE,
  heading      TEXT,            -- "§C — SSTable explosion"
  text         TEXT NOT NULL,
  symptoms     TEXT             -- extracted literals, weighted higher
);

-- BM25 side. Note: symptoms column repeated to boost exact-term weight.
CREATE VIRTUAL TABLE chunk_fts USING fts5(
  text, heading, symptoms,
  content='chunk', content_rowid='id', tokenize='porter unicode61'
);

-- Dense side (sqlite-vec). 384 dims = all-MiniLM-L6-v2.
CREATE VIRTUAL TABLE chunk_vec USING vec0(
  chunk_id INTEGER PRIMARY KEY, embedding FLOAT[384]
);

-- chunk_fts is an external-content FTS5 table: it does not track INSERT/UPDATE/DELETE
-- on `chunk` by itself. These triggers keep it in sync so a direct `INSERT INTO chunk`
-- (which is all store.py ever does — no ORM layer) can't silently drift the index.
CREATE TRIGGER chunk_ai AFTER INSERT ON chunk BEGIN
  INSERT INTO chunk_fts(rowid, text, heading, symptoms)
  VALUES (new.id, new.text, new.heading, new.symptoms);
END;

CREATE TRIGGER chunk_ad AFTER DELETE ON chunk BEGIN
  INSERT INTO chunk_fts(chunk_fts, rowid, text, heading, symptoms)
  VALUES ('delete', old.id, old.text, old.heading, old.symptoms);
  -- vec0 virtual tables aren't FK-enforceable, so the artifact->chunk ON DELETE
  -- CASCADE above doesn't reach chunk_vec on its own. This trigger closes that gap
  -- for both a direct chunk delete and one reached via the cascade (SQLite fires
  -- AFTER DELETE triggers for cascaded rows too).
  DELETE FROM chunk_vec WHERE chunk_id = old.id;
END;

CREATE TRIGGER chunk_au AFTER UPDATE ON chunk BEGIN
  INSERT INTO chunk_fts(chunk_fts, rowid, text, heading, symptoms)
  VALUES ('delete', old.id, old.text, old.heading, old.symptoms);
  INSERT INTO chunk_fts(rowid, text, heading, symptoms)
  VALUES (new.id, new.text, new.heading, new.symptoms);
END;

-- P2-11: what gets shipped for a skill, as opposed to `artifact.body` (what gets
-- indexed - SKILL.md only). A runbook or script is a one-file bundle here too.
CREATE TABLE artifact_file (
  artifact_id  INTEGER NOT NULL REFERENCES artifact(id) ON DELETE CASCADE,
  path         TEXT NOT NULL,     -- POSIX, relative to the skill root
  content      BLOB NOT NULL,     -- exact bytes
  PRIMARY KEY (artifact_id, path)
);

CREATE INDEX IF NOT EXISTS idx_chunk_artifact ON chunk(artifact_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_identity
  ON artifact(kind, name, COALESCE(version, ''));

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL      -- schema_version, embedding_model, embedding_dim
);
