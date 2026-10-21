-- Conversation database written by Agno 3.0.9 through MindRoom's storage path, dumped with iterdump and
-- normalized (see tests/fixtures/agno_parity/README.md). Regenerate with
-- `python -m tests.agno_parity.capture_storage`; never edit by hand.
BEGIN TRANSACTION;
CREATE TABLE agno_schema_versions (
	table_name VARCHAR NOT NULL,
	version VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	updated_at VARCHAR,
	PRIMARY KEY (table_name)
);
INSERT INTO "agno_schema_versions" VALUES('code_sessions','2.5.6','2026-09-03T03:11:12.291654','2026-09-03T03:11:12.291654');
INSERT INTO "agno_schema_versions" VALUES('code_sessions_runs','3.0.0','2027-01-15T08:34:00','2027-01-15T08:34:00');
CREATE TABLE code_sessions (
	session_id VARCHAR NOT NULL,
	session_type VARCHAR NOT NULL,
	agent_id VARCHAR,
	team_id VARCHAR,
	workflow_id VARCHAR,
	user_id VARCHAR,
	session_data JSON,
	agent_data JSON,
	team_data JSON,
	workflow_data JSON,
	metadata JSON,
	runs JSON,
	summary JSON,
	created_at BIGINT NOT NULL,
	updated_at BIGINT,
	PRIMARY KEY (session_id)
);
INSERT INTO "code_sessions" VALUES('session-1','agent','code',NULL,NULL,'@alice:example.test','"{\"session_state\": {}}"','null',NULL,NULL,'null',NULL,'null',1700000000,1700000000);
CREATE TABLE "code_sessions_removed_runs" (run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES "code_sessions"(session_id) ON DELETE CASCADE);
CREATE TABLE code_sessions_runs (
	run_id VARCHAR NOT NULL,
	session_id VARCHAR NOT NULL,
	run_type VARCHAR NOT NULL,
	agent_id VARCHAR,
	team_id VARCHAR,
	workflow_id VARCHAR,
	user_id VARCHAR,
	parent_run_id VARCHAR,
	status VARCHAR,
	run_index BIGINT,
	run_data JSON NOT NULL,
	created_at BIGINT NOT NULL,
	updated_at BIGINT,
	PRIMARY KEY (run_id),
	FOREIGN KEY(session_id) REFERENCES code_sessions (session_id) ON DELETE CASCADE
);
INSERT INTO "code_sessions_runs" VALUES('run-1','session-1','agent','code',NULL,NULL,'@alice:example.test',NULL,'RUNNING',0,'{"run_id": "run-1", "agent_id": "code", "session_id": "session-1", "user_id": "@alice:example.test", "content_type": "str", "created_at": 1700000001, "status": "RUNNING", "metrics": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}, "messages": [{"id": "00000000-0000-4000-8000-000000000077", "content": "q1", "from_history": false, "stop_after_tool_call": false, "role": "user", "created_at": 1788430272}, {"id": "00000000-0000-4000-8000-000000000078", "content": "a1", "from_history": false, "stop_after_tool_call": false, "role": "assistant", "created_at": 1788430272}]}',1700000001,1800002040);
INSERT INTO "code_sessions_runs" VALUES('run-2','session-1','agent','code',NULL,NULL,'@alice:example.test',NULL,'RUNNING',1,'{"run_id": "run-2", "agent_id": "code", "session_id": "session-1", "user_id": "@alice:example.test", "content_type": "str", "created_at": 1700000002, "status": "RUNNING", "metrics": {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4}, "messages": [{"id": "00000000-0000-4000-8000-000000000079", "content": "q2", "from_history": false, "stop_after_tool_call": false, "role": "user", "created_at": 1788430272}, {"id": "00000000-0000-4000-8000-00000000007a", "content": "a2", "from_history": false, "stop_after_tool_call": false, "role": "assistant", "created_at": 1788430272}]}',1700000002,1800002040);
INSERT INTO "code_sessions_runs" VALUES('run-3','session-1','agent','code',NULL,NULL,'@alice:example.test',NULL,'RUNNING',2,'{"run_id": "run-3", "agent_id": "code", "session_id": "session-1", "user_id": "@alice:example.test", "content_type": "str", "created_at": 1700000003, "status": "RUNNING", "metrics": {"input_tokens": 3, "output_tokens": 3, "total_tokens": 6}, "messages": [{"id": "00000000-0000-4000-8000-00000000007b", "content": "q3", "from_history": false, "stop_after_tool_call": false, "role": "user", "created_at": 1788430272}, {"id": "00000000-0000-4000-8000-00000000007c", "content": "a3", "from_history": false, "stop_after_tool_call": false, "role": "assistant", "created_at": 1788430272}]}',1700000003,1800002040);
CREATE TABLE "code_sessions_summaries" (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES "code_sessions"(session_id) ON DELETE CASCADE, scope_key TEXT NOT NULL, after_run_index INTEGER NOT NULL, summary TEXT, summary_model TEXT, event_ids TEXT NOT NULL DEFAULT '[]', seen_event_ids TEXT NOT NULL DEFAULT '[]', legacy INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, segment_run_id TEXT, segment TEXT);
CREATE TABLE "code_sessions_usage" (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES "code_sessions"(session_id) ON DELETE CASCADE, run_id TEXT, usage_data TEXT, UNIQUE(session_id, run_id));
INSERT INTO "code_sessions_usage" VALUES(1,'session-1','run-1','{"run_id": "run-1", "user_id": "@alice:example.test", "created_at": 1700000001, "metrics": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}, "requests": []}');
INSERT INTO "code_sessions_usage" VALUES(2,'session-1','run-2','{"run_id": "run-2", "user_id": "@alice:example.test", "created_at": 1700000002, "metrics": {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4}, "requests": []}');
INSERT INTO "code_sessions_usage" VALUES(3,'session-1','run-3','{"run_id": "run-3", "user_id": "@alice:example.test", "created_at": 1700000003, "metrics": {"input_tokens": 3, "output_tokens": 3, "total_tokens": 6}, "requests": []}');
CREATE INDEX "code_sessions_removed_runs_session" ON "code_sessions_removed_runs" (session_id);
CREATE INDEX "code_sessions_summaries_scope" ON "code_sessions_summaries" (session_id, scope_key, id);
CREATE INDEX idx_agno_schema_versions_created_at ON agno_schema_versions (created_at);
CREATE INDEX idx_code_sessions_created_at ON code_sessions (created_at);
CREATE INDEX idx_code_sessions_runs_agent_id ON code_sessions_runs (agent_id);
CREATE INDEX idx_code_sessions_runs_created_at ON code_sessions_runs (created_at);
CREATE INDEX idx_code_sessions_runs_run_type ON code_sessions_runs (run_type);
CREATE INDEX idx_code_sessions_runs_session_id ON code_sessions_runs (session_id);
CREATE INDEX idx_code_sessions_runs_session_id_run_index ON code_sessions_runs (session_id, run_index);
CREATE INDEX idx_code_sessions_runs_status ON code_sessions_runs (status);
CREATE INDEX idx_code_sessions_runs_team_id ON code_sessions_runs (team_id);
CREATE INDEX idx_code_sessions_runs_user_id ON code_sessions_runs (user_id);
CREATE INDEX idx_code_sessions_runs_workflow_id ON code_sessions_runs (workflow_id);
CREATE INDEX idx_code_sessions_session_type ON code_sessions (session_type);
COMMIT;
