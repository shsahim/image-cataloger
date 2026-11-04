import sqlite3

from image_cataloger.db import CatalogDB


def test_old_database_gains_new_columns(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE files (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, md5 TEXT,
        sha256 TEXT, width INTEGER, height INTEGER, status TEXT NOT NULL DEFAULT 'pending', summary TEXT,
        duplicate_of TEXT, error TEXT, model TEXT, input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0, updated_at REAL)""")
    conn.execute("INSERT INTO files(path, status, summary) VALUES('/a.jpg', 'ok', 'old row')")
    conn.commit()
    conn.close()
    db = CatalogDB(path)
    row = db.get("/a.jpg")
    assert row["kind"] == "image" and row["duration"] is None and row["summary"] == "old row"
    db.upsert_hashed("/v.mp4", 1, 1.0, "m", "s", 10, 10, "pending", kind="video", duration=2.5)
    assert db.get("/v.mp4")["kind"] == "video"
    db.close()
