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
INSERT INTO "code_sessions" VALUES('session-1','agent','code',NULL,NULL,'@alice:example.test','"{\"session_state\": {}}"','null',NULL,NULL,'null','"[{\"run_id\": \"run-b\", \"agent_id\": \"code\", \"content\": \"blob\", \"created_at\": 2, \"messages\": []}, {\"run_id\": \"run-a\", \"agent_id\": \"code\", \"content\": \"blob\", \"created_at\": 1, \"messages\": []}]"','null',1700000000,1700000000);
CREATE TABLE "code_sessions_runs" (run_id VARCHAR NOT NULL, session_id VARCHAR NOT NULL, run_type VARCHAR NOT NULL, agent_id VARCHAR, team_id VARCHAR, workflow_id VARCHAR, user_id VARCHAR, parent_run_id VARCHAR, status VARCHAR, run_index BIGINT, run_data JSON NOT NULL, created_at BIGINT NOT NULL, updated_at BIGINT, PRIMARY KEY (run_id), FOREIGN KEY(session_id) REFERENCES "code_sessions" (session_id) ON DELETE CASCADE);
INSERT INTO "code_sessions_runs" VALUES('run-c','session-1','agent','code',NULL,NULL,NULL,NULL,'COMPLETED',0,'{"run_id": "run-c", "agent_id": "code", "content": "table", "created_at": 3, "messages": []}',3,NULL);
INSERT INTO "code_sessions_runs" VALUES('run-a','session-1','agent','code',NULL,NULL,NULL,NULL,'COMPLETED',1,'{"run_id": "run-a", "agent_id": "code", "content": "table", "created_at": 1, "messages": []}',1,NULL);
CREATE INDEX idx_code_sessions_session_type ON code_sessions (session_type);
CREATE INDEX idx_code_sessions_created_at ON code_sessions (created_at);
CREATE INDEX idx_agno_schema_versions_created_at ON agno_schema_versions (created_at);
CREATE INDEX "idx_code_sessions_runs_session_id" ON "code_sessions_runs" (session_id);
CREATE INDEX "idx_code_sessions_runs_run_type" ON "code_sessions_runs" (run_type);
CREATE INDEX "idx_code_sessions_runs_agent_id" ON "code_sessions_runs" (agent_id);
CREATE INDEX "idx_code_sessions_runs_team_id" ON "code_sessions_runs" (team_id);
CREATE INDEX "idx_code_sessions_runs_workflow_id" ON "code_sessions_runs" (workflow_id);
CREATE INDEX "idx_code_sessions_runs_user_id" ON "code_sessions_runs" (user_id);
CREATE INDEX "idx_code_sessions_runs_status" ON "code_sessions_runs" (status);
CREATE INDEX "idx_code_sessions_runs_created_at" ON "code_sessions_runs" (created_at);
CREATE INDEX "idx_code_sessions_runs_session_id_run_index" ON "code_sessions_runs" (session_id, run_index);
COMMIT;
