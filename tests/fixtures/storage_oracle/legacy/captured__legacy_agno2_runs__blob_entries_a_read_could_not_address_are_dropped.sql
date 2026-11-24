BEGIN TRANSACTION;
CREATE TABLE agno_schema_versions (
	table_name VARCHAR NOT NULL,
	version VARCHAR NOT NULL,
	created_at VARCHAR NOT NULL,
	updated_at VARCHAR,
	PRIMARY KEY (table_name)
);
INSERT INTO "agno_schema_versions" VALUES('code_sessions','2.5.6','2026-09-03T03:11:12.291654','2026-09-03T03:11:12.291654');
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
INSERT INTO "code_sessions" VALUES('session-1','agent','code',NULL,NULL,'@alice:example.test','"{\"session_state\": {}}"','null',NULL,NULL,'null','"[{\"run_id\": 5, \"agent_id\": \"code\"}, {\"agent_id\": \"code\"}, \"junk\", {\"run_id\": \"ok\", \"agent_id\": \"code\", \"content\": \"blob\", \"created_at\": 1, \"messages\": []}, {\"run_id\": \"ok\", \"agent_id\": \"code\", \"content\": \"dup\", \"created_at\": 2, \"messages\": []}]"','null',1700000000,1700000000);
CREATE INDEX idx_code_sessions_session_type ON code_sessions (session_type);
CREATE INDEX idx_code_sessions_created_at ON code_sessions (created_at);
CREATE INDEX idx_agno_schema_versions_created_at ON agno_schema_versions (created_at);
COMMIT;
