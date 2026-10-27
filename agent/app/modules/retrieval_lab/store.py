"""SQLite persistence for isolated local retrieval experiments and human review.

No network, model call, automatic relevance label or production database is used.
Each retrieval run freezes its complete article/trace payload and version identity.
"""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .metrics import LABEL_GRADES, calculate_metrics, review_progress

SCHEMA_VERSION = 1
MAX_RUN_BYTES = 32 * 1024 * 1024


class FrozenRunConflict(ValueError):
    """An existing run or manifest identifier was reused for different content."""


class RevisionConflict(ValueError):
    def __init__(self, current):
        super().__init__("judgment revision has changed; reload before saving")
        self.current = current


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value, name, limit=256):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("{} must be a nonempty string of at most {} characters".format(name, limit))
    return value


def _empty_judgment():
    return {"label": None, "grade": None, "status": "unjudged", "reviewer": None,
            "notes": "", "revision": 0, "updated_at": None}


class RetrievalLabStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError("unsupported retrieval lab schema version")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS lab_manifest (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    payload TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lab_runs (
                    run_id TEXT PRIMARY KEY, query_id TEXT NOT NULL,
                    payload TEXT NOT NULL, sha256 TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lab_run_candidates (
                    run_id TEXT NOT NULL REFERENCES lab_runs(run_id), news_id TEXT NOT NULL,
                    rank INTEGER NOT NULL, PRIMARY KEY(run_id,news_id), UNIQUE(run_id,rank)
                );
                CREATE TABLE IF NOT EXISTS lab_judgments (
                    run_id TEXT NOT NULL, news_id TEXT NOT NULL,
                    label TEXT NOT NULL CHECK(label IN ('relevant','partial','irrelevant','unsure')),
                    reviewer TEXT NOT NULL, notes TEXT NOT NULL, revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(run_id,news_id),
                    FOREIGN KEY(run_id,news_id) REFERENCES lab_run_candidates(run_id,news_id)
                );
                CREATE TABLE IF NOT EXISTS lab_judgment_audit (
                    run_id TEXT NOT NULL, news_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    payload TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(run_id,news_id,revision),
                    FOREIGN KEY(run_id,news_id) REFERENCES lab_run_candidates(run_id,news_id)
                );
            """)
            connection.execute("PRAGMA user_version={}".format(SCHEMA_VERSION))

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(str(self.path), timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def set_manifest(self, manifest):
        if not isinstance(manifest, dict):
            raise ValueError("manifest must be an object")
        payload = _json(manifest)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT sha256 FROM lab_manifest WHERE singleton=1").fetchone()
            if previous and previous["sha256"] != digest:
                raise FrozenRunConflict("manifest is frozen; use a separate lab database for another corpus")
            connection.execute("INSERT OR IGNORE INTO lab_manifest VALUES (1,?,?,?)", (payload, digest, _now()))
        return self.get_manifest()

    @staticmethod
    def _manifest(row):
        if row is None:
            return None
        if hashlib.sha256(row["payload"].encode("utf-8")).hexdigest() != row["sha256"]:
            raise ValueError("frozen manifest integrity check failed")
        return json.loads(row["payload"])

    def get_manifest(self):
        with self._connection() as connection:
            row = connection.execute("SELECT payload,sha256 FROM lab_manifest WHERE singleton=1").fetchone()
        return self._manifest(row)

    def put_run(self, run):
        if not isinstance(run, dict):
            raise ValueError("run must be an object")
        frozen = json.loads(_json(run))
        for key in ("run_id", "query_id"):
            _text(frozen.get(key), key)
        _text(frozen.get("query"), "query", 10000)
        versions = frozen.get("versions")
        if not isinstance(versions, dict):
            raise ValueError("versions must include corpus, model and strategy")
        for key in ("corpus", "model", "strategy"):
            _text(versions.get(key), "versions." + key, 2048)
        candidates = frozen.get("candidates")
        if not isinstance(candidates, list) or len(candidates) > 5000:
            raise ValueError("candidates must be a list with at most 5000 entries")
        ids = set()
        for position, candidate in enumerate(candidates, 1):
            if not isinstance(candidate, dict):
                raise ValueError("each candidate must be an object")
            news_id = _text(candidate.get("news_id"), "news_id")
            if news_id in ids:
                raise ValueError("candidate news_id must be unique within a run")
            ids.add(news_id)
            if type(candidate.get("rank")) is not int or candidate["rank"] != position:
                raise ValueError("candidate ranks must be contiguous starting at 1 in list order")
            if "judgment" in candidate or "revision" in candidate:
                raise ValueError("retrieval snapshots cannot contain judgments; save human review separately")
        for key in ("metrics", "progress", "snapshot_sha256"):
            if key in frozen:
                raise ValueError("derived field {} cannot be part of a frozen run".format(key))
        payload = _json(frozen)
        if len(payload.encode("utf-8")) > MAX_RUN_BYTES:
            raise ValueError("run snapshot exceeds maximum size")
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT sha256 FROM lab_runs WHERE run_id=?", (frozen["run_id"],)).fetchone()
            if previous and previous["sha256"] != digest:
                raise FrozenRunConflict("run_id already identifies a different frozen retrieval result")
            if previous is None:
                connection.execute("INSERT INTO lab_runs VALUES (?,?,?,?,?)",
                                   (frozen["run_id"], frozen["query_id"], payload, digest, _now()))
                connection.executemany("INSERT INTO lab_run_candidates VALUES (?,?,?)",
                                       [(frozen["run_id"], c["news_id"], c["rank"]) for c in candidates])
        return self.get_run(frozen["run_id"])

    @staticmethod
    def _judgment(row):
        if row is None:
            return _empty_judgment()
        label = row["label"]
        return {"label": label, "grade": LABEL_GRADES[label],
                "status": "unable" if label == "unsure" else "judged",
                "reviewer": row["reviewer"], "notes": row["notes"],
                "revision": row["revision"], "updated_at": row["updated_at"]}

    def _get_run(self, connection, run_id):
        row = connection.execute("SELECT * FROM lab_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("unknown run: " + run_id)
        payload = row["payload"]
        if hashlib.sha256(payload.encode("utf-8")).hexdigest() != row["sha256"]:
            raise ValueError("frozen run integrity check failed")
        run = json.loads(payload)
        judgments = {item["news_id"]: item for item in connection.execute("SELECT * FROM lab_judgments WHERE run_id=?", (run_id,))}
        for candidate in run["candidates"]:
            candidate["judgment"] = self._judgment(judgments.get(candidate["news_id"]))
            candidate["revision"] = candidate["judgment"]["revision"]
        run["snapshot_sha256"] = row["sha256"]
        run.setdefault("created_at", row["created_at"])
        run["progress"] = review_progress(run["candidates"])
        run["metrics"] = calculate_metrics(run["candidates"])
        return run

    def get_run(self, run_id):
        with self._connection() as connection:
            connection.execute("BEGIN")
            return self._get_run(connection, run_id)

    def list_runs(self):
        with self._connection() as connection:
            connection.execute("BEGIN")
            rows = connection.execute("SELECT run_id FROM lab_runs ORDER BY created_at,run_id").fetchall()
            runs = [self._get_run(connection, row["run_id"]) for row in rows]
        return [{"run_id": run["run_id"], "query_id": run["query_id"], "text": run["query"],
                 "query": run["query"], "category": run.get("category"), "versions": run["versions"],
                 "progress": run["progress"], "created_at": run["created_at"]} for run in runs]

    def save_judgment(self, run_id, news_id, *, label, expected_revision, notes="", reviewer="local-human"):
        if not isinstance(label, str) or label not in LABEL_GRADES:
            raise ValueError("label must be relevant, partial, irrelevant or unsure")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        _text(reviewer, "reviewer", 120)
        if not isinstance(notes, str) or len(notes) > 10000:
            raise ValueError("notes must be a string of at most 10000 characters")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM lab_run_candidates WHERE run_id=? AND news_id=?", (run_id, news_id)).fetchone() is None:
                raise KeyError("news_id is not in this frozen run")
            row = connection.execute("SELECT * FROM lab_judgments WHERE run_id=? AND news_id=?", (run_id, news_id)).fetchone()
            current = self._judgment(row)
            if current["revision"] != expected_revision:
                raise RevisionConflict(current)
            revision = expected_revision + 1
            updated_at = _now()
            connection.execute("""INSERT INTO lab_judgments VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(run_id,news_id) DO UPDATE SET label=excluded.label,
                reviewer=excluded.reviewer, notes=excluded.notes, revision=excluded.revision,
                updated_at=excluded.updated_at""", (run_id, news_id, label, reviewer, notes, revision, updated_at))
            judgment = self._judgment({"label": label, "reviewer": reviewer, "notes": notes,
                                       "revision": revision, "updated_at": updated_at})
            connection.execute("INSERT INTO lab_judgment_audit VALUES (?,?,?,?,?)",
                               (run_id, news_id, revision, _json(judgment), updated_at))
            run = self._get_run(connection, run_id)
            return {"judgment": judgment, "progress": run["progress"], "metrics": run["metrics"]}

    def judgment_history(self, run_id, news_id):
        with self._connection() as connection:
            rows = connection.execute("SELECT payload FROM lab_judgment_audit WHERE run_id=? AND news_id=? ORDER BY revision", (run_id, news_id)).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def metrics(self, run_id, k=10):
        return calculate_metrics(self.get_run(run_id)["candidates"], k)

    def export_data(self):
        with self._connection() as connection:
            connection.execute("BEGIN")
            manifest = connection.execute("SELECT payload,sha256 FROM lab_manifest WHERE singleton=1").fetchone()
            rows = connection.execute("SELECT run_id FROM lab_runs ORDER BY created_at,run_id").fetchall()
            runs = [self._get_run(connection, row["run_id"]) for row in rows]
            audit = [dict(row) for row in connection.execute("SELECT run_id,news_id,revision,payload FROM lab_judgment_audit ORDER BY run_id,news_id,revision")]
        for item in audit:
            item["judgment"] = json.loads(item.pop("payload"))
        return {"schema": "retrieval-lab-human-review-v1", "schema_version": SCHEMA_VERSION,
                "exported_at": _now(), "review_scope": "single_human_review_not_promoted_gold",
                "manifest": self._manifest(manifest),
                "manifest_sha256": manifest["sha256"] if manifest else None,
                "runs": runs, "judgment_audit": audit}
