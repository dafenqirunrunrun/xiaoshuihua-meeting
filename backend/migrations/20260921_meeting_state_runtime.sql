-- Additive migration for realtime Meeting State snapshots and health.
-- Safe to run repeatedly against the current SQLite deployment.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meeting_memory_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id VARCHAR(64) NOT NULL,
    from_segment_seq INTEGER NOT NULL,
    to_segment_seq INTEGER NOT NULL,
    window_start_ms INTEGER,
    window_end_ms INTEGER,
    state_json TEXT NOT NULL,
    model_name VARCHAR(128),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT fk_memory_snapshot_meeting
        FOREIGN KEY (meeting_id) REFERENCES meeting(id) ON DELETE CASCADE,
    CONSTRAINT uq_meeting_memory_snapshot_range
        UNIQUE (meeting_id, from_segment_seq, to_segment_seq)
);

CREATE INDEX IF NOT EXISTS ix_meeting_memory_snapshot_meeting_id
    ON meeting_memory_snapshot (meeting_id);

CREATE TABLE IF NOT EXISTS meeting_state_health (
    meeting_id VARCHAR(64) PRIMARY KEY,
    last_attempt_at DATETIME,
    last_success_at DATETIME,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    CONSTRAINT fk_meeting_state_health_meeting
        FOREIGN KEY (meeting_id) REFERENCES meeting(id) ON DELETE CASCADE
);
