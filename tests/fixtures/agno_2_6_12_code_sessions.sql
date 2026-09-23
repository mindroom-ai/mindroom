-- Session database written by agno 2.6.12 (SqliteDb, session_table="code_sessions"), dumped with
-- `sqlite3 code.db .dump`. One agent session, three runs, metrics total_tokens 2/4/6, runs stored the
-- 2.x way: the JSON text of the run list inside a JSON column (encoded twice). Regenerate only with a
-- real agno 2.x install; hand-edited fixtures have drifted before (a TEXT column reads as empty).
PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
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
INSERT INTO code_sessions VALUES('session-1','agent','code',NULL,NULL,'@alice:example.test','"{\"session_state\": {}}"','null',NULL,NULL,'null','"[{\"run_id\": \"run-1\", \"agent_id\": \"code\", \"session_id\": \"session-1\", \"user_id\": \"@alice:example.test\", \"content_type\": \"str\", \"created_at\": 1700000001, \"status\": \"RUNNING\", \"metrics\": {\"input_tokens\": 1, \"output_tokens\": 1, \"total_tokens\": 2}, \"messages\": [{\"id\": \"73d7902d-762c-453f-b175-d147080b7552\", \"content\": \"q1\", \"from_history\": false, \"stop_after_tool_call\": false, \"role\": \"user\", \"created_at\": 1788430272}, {\"id\": \"d8456f0d-78b8-458f-b1aa-6719c09f2cb5\", \"content\": \"a1\", \"from_history\": false, \"stop_after_tool_call\": false, \"role\": \"assistant\", \"created_at\": 1788430272}]}, {\"run_id\": \"run-2\", \"agent_id\": \"code\", \"session_id\": \"session-1\", \"user_id\": \"@alice:example.test\", \"content_type\": \"str\", \"created_at\": 1700000002, \"status\": \"RUNNING\", \"metrics\": {\"input_tokens\": 2, \"output_tokens\": 2, \"total_tokens\": 4}, \"messages\": [{\"id\": \"2b51814b-7b8d-4308-a3b6-39e4309c44d5\", \"content\": \"q2\", \"from_history\": false, \"stop_after_tool_call\": false, \"role\": \"user\", \"created_at\": 1788430272}, {\"id\": \"0db3e4df-bcd5-4d39-87f5-e86da6224ca8\", \"content\": \"a2\", \"from_history\": false, \"stop_after_tool_call\": false, \"role\": \"assistant\", \"created_at\": 1788430272}]}, {\"run_id\": \"run-3\", \"agent_id\": \"code\", \"session_id\": \"session-1\", \"user_id\": \"@alice:example.test\", \"content_type\": \"str\", \"created_at\": 1700000003, \"status\": \"RUNNING\", \"metrics\": {\"input_tokens\": 3, \"output_tokens\": 3, \"total_tokens\": 6}, \"messages\": [{\"id\": \"52b27593-4b91-4cb4-a76e-e6a533c22579\", \"content\": \"q3\", \"from_history\": false, \"stop_after_tool_call\": false, \"role\": \"user\", \"created_at\": 1788430272}, {\"id\": \"a5c506cb-ddd8-4247-b3a8-1df1e8cd201d\", \"content\": \"a3\", \"from_history\": false, \"stop_after_tool_call\": false, \"role\": \"assistant\", \"created_at\": 1788430272}]}]"','null',1700000000,1700000000);
CREATE TABLE agno_schema_versions (
	table_name VARCHAR NOT NULL,
	version VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	updated_at VARCHAR,
	PRIMARY KEY (table_name)
);
INSERT INTO agno_schema_versions VALUES('code_sessions','2.5.6','2026-09-03T03:11:12.291654','2026-09-03T03:11:12.291654');
CREATE INDEX idx_code_sessions_session_type ON code_sessions (session_type);
CREATE INDEX idx_code_sessions_created_at ON code_sessions (created_at);
CREATE INDEX idx_agno_schema_versions_created_at ON agno_schema_versions (created_at);
COMMIT;
