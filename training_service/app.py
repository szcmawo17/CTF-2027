"""Local CTF exercise: deliberate IDOR on public notes, no command execution."""
import hashlib
import hmac
import os
import re
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from flask import Flask, jsonify, request


def create_app(*, database, token, instance_id):
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("A dedicated checker token is required")
    instance = str(uuid.UUID(str(instance_id)))
    path = Path(database)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, value TEXT NOT NULL, expires_at REAL NOT NULL)")
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=2048, DEBUG=False)

    @app.before_request
    def authenticate():
        if request.path.startswith("/checker/") and request.path != "/checker/challenge":
            provided = request.headers.get("Authorization", "")
            if not hmac.compare_digest(provided.encode(), ("Bearer " + token).encode()):
                return jsonify(error="unauthorized"), 403

    def data():
        body = request.get_json(silent=True)
        return body if isinstance(body, dict) else {}

    @contextmanager
    def connect():
        db = sqlite3.connect(path, timeout=2)
        try:
            with db:
                # Short grace period for get_flag after the attack deadline.
                db.execute("DELETE FROM notes WHERE expires_at < ?", (time.time() - 120,))
                yield db
        finally:
            db.close()

    @app.post("/checker/challenge")
    def challenge():
        nonce = data().get("nonce", "")
        if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{64}", nonce):
            return jsonify(error="invalid"), 400
        proof = hmac.new(token.encode(), (instance + ":" + nonce).encode(), hashlib.sha256).hexdigest()
        return jsonify(instance=instance, proof=proof)

    @app.post("/checker/put")
    def put():
        body = data()
        record_id, value, expiry = body.get("id"), body.get("value"), body.get("expires_at")
        if (type(record_id) is not int or not 0 < record_id < 2**63
                or not isinstance(value, str) or not re.fullmatch(r"CTF\{[0-9a-f]{64}\}", value)
                or type(expiry) not in (int, float) or not time.time() < expiry <= time.time() + 3600):
            return jsonify(error="invalid"), 400
        with connect() as db:
            old = db.execute("SELECT value, expires_at FROM notes WHERE id=?", (record_id,)).fetchone()
            if old is not None and old != (value, expiry):
                return jsonify(error="conflict"), 409
            db.execute("INSERT OR IGNORE INTO notes VALUES (?, ?, ?)", (record_id, value, expiry))
        return jsonify(stored=True)

    @app.post("/checker/get")
    def get():
        record_id = data().get("id")
        if type(record_id) is not int or not 0 < record_id < 2**63:
            return jsonify(error="invalid"), 400
        with connect() as db:
            row = db.execute("SELECT value FROM notes WHERE id=?", (record_id,)).fetchone()
        return (jsonify(value=row[0]), 200) if row else (jsonify(error="missing"), 404)

    @app.post("/checker/health")
    def health():
        with connect() as db:
            db.execute("SELECT count(*) FROM notes").fetchone()
        return jsonify(healthy=True)

    @app.get("/notes")
    def notes():
        with connect() as db:
            rows = db.execute("SELECT id FROM notes WHERE expires_at > ? ORDER BY id LIMIT 256", (time.time(),)).fetchall()
        return jsonify(notes=[row[0] for row in rows])

    @app.get("/notes/<int:record_id>")
    def note(record_id):
        if not 0 < record_id < 2**63:
            return jsonify(error="missing"), 404
        # INTENTIONAL IDOR: no owner/session check. This is the exercise's exploit.
        with connect() as db:
            row = db.execute("SELECT value FROM notes WHERE id=? AND expires_at > ?", (record_id, time.time())).fetchone()
        return (jsonify(value=row[0]), 200) if row else (jsonify(error="missing"), 404)

    return app


def main():
    from waitress import serve
    app = create_app(database="/data/notes.sqlite3", token=os.environ["CHECKER_TOKEN"], instance_id=os.environ["CHECKER_INSTANCE"])
    serve(app, host="0.0.0.0", port=5000, threads=4, max_request_body_size=2048, channel_timeout=10)


if __name__ == "__main__":
    main()
