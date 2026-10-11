CREATE TABLE clients (
    client_id TEXT PRIMARY KEY,
    metadata TEXT NOT NULL,
    expires_at REAL NOT NULL,
    accounted_bytes INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE pending (
    state_hash TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    expires_at REAL NOT NULL,
    requester_id TEXT,
    authenticated_user_id TEXT,
    agent_name TEXT,
    csrf_hash TEXT,
    accounted_bytes INTEGER NOT NULL DEFAULT 0,
    account_id TEXT
);

CREATE TABLE grants (
    grant_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    expires_at REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    accounted_bytes INTEGER NOT NULL DEFAULT 0,
    requester_id TEXT NOT NULL DEFAULT '',
    requester_charge INTEGER NOT NULL DEFAULT 0,
    created_at REAL,
    last_used_at REAL,
    last_activity_at REAL,
    idle_expires_at REAL,
    account_id TEXT
);

CREATE TABLE capabilities (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    expires_at REAL NOT NULL,
    consumed INTEGER NOT NULL DEFAULT 0,
    accounted_bytes INTEGER NOT NULL DEFAULT 0,
    issued_at REAL,
    FOREIGN KEY (grant_id) REFERENCES grants(grant_id)
);

CREATE TABLE oauth_usage (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    bytes_used INTEGER NOT NULL,
    onboarding_bytes INTEGER NOT NULL
);

CREATE TABLE requester_usage (
    requester_id TEXT PRIMARY KEY,
    bytes_used INTEGER NOT NULL
);

CREATE INDEX capabilities_grant ON capabilities(grant_id);
CREATE INDEX grants_client ON grants(json_extract(payload, '$.client_id'));
CREATE INDEX pending_client ON pending(json_extract(payload, '$.client_id'));
CREATE INDEX pending_expiry ON pending(expires_at);
CREATE INDEX clients_expiry ON clients(expires_at);
CREATE INDEX grants_expiry ON grants(expires_at);
CREATE INDEX grants_revoked ON grants(grant_id) WHERE revoked = 1;
CREATE INDEX capabilities_code_expiry ON capabilities(expires_at) WHERE kind = 'code';
CREATE INDEX capabilities_code_consumed ON capabilities(grant_id) WHERE kind = 'code' AND consumed = 1;
CREATE INDEX capabilities_live ON capabilities(grant_id) WHERE consumed = 0 AND kind IN ('access', 'refresh');
CREATE INDEX capabilities_issuance ON capabilities(grant_id, issued_at) WHERE kind = 'access';
CREATE INDEX grants_idle_expiry ON grants(idle_expires_at);
CREATE INDEX grants_account ON grants(account_id);
CREATE INDEX pending_account ON pending(account_id);
CREATE INDEX capabilities_access_expiry ON capabilities(expires_at) WHERE kind = 'access';
CREATE INDEX grants_owner ON grants(
    requester_id,
    json_extract(payload, '$.authenticated_user_id'),
    json_extract(payload, '$.agent_name'),
    json_extract(payload, '$.resource')
);
CREATE INDEX pending_owner ON pending(requester_id, authenticated_user_id, agent_name);

CREATE TRIGGER clients_charge_insert AFTER INSERT ON clients
BEGIN
    UPDATE clients
    SET accounted_bytes = 1024
        + COALESCE(length(CAST(NEW.client_id AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.metadata AS BLOB)), 0)
    WHERE client_id = NEW.client_id;
END;

CREATE TRIGGER clients_charge_change AFTER UPDATE OF client_id, metadata ON clients
BEGIN
    UPDATE clients
    SET accounted_bytes = 1024
        + COALESCE(length(CAST(NEW.client_id AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.metadata AS BLOB)), 0)
    WHERE client_id = NEW.client_id;
END;

CREATE TRIGGER clients_usage_update AFTER UPDATE OF accounted_bytes ON clients
BEGIN
    UPDATE oauth_usage
    SET bytes_used = bytes_used + (NEW.accounted_bytes - OLD.accounted_bytes)
    WHERE singleton = 1;
    UPDATE oauth_usage
    SET onboarding_bytes = onboarding_bytes + (NEW.accounted_bytes - OLD.accounted_bytes)
    WHERE singleton = 1
      AND NOT EXISTS (
          SELECT 1 FROM grants WHERE json_extract(payload, '$.client_id') = OLD.client_id
      );
END;

CREATE TRIGGER clients_usage_delete AFTER DELETE ON clients
BEGIN
    UPDATE oauth_usage
    SET bytes_used = bytes_used - OLD.accounted_bytes
    WHERE singleton = 1;
    UPDATE oauth_usage
    SET onboarding_bytes = onboarding_bytes - OLD.accounted_bytes
    WHERE singleton = 1
      AND NOT EXISTS (
          SELECT 1 FROM grants WHERE json_extract(payload, '$.client_id') = OLD.client_id
      );
END;

CREATE TRIGGER pending_charge_insert AFTER INSERT ON pending
BEGIN
    UPDATE pending
    SET accounted_bytes = 1024
        + COALESCE(length(CAST(NEW.state_hash AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.payload AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.requester_id AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.authenticated_user_id AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.agent_name AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.csrf_hash AS BLOB)), 0)
    WHERE state_hash = NEW.state_hash;
END;

CREATE TRIGGER pending_charge_change
AFTER UPDATE OF state_hash, payload, requester_id, authenticated_user_id, agent_name, csrf_hash ON pending
BEGIN
    UPDATE pending
    SET accounted_bytes = 1024
        + COALESCE(length(CAST(NEW.state_hash AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.payload AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.requester_id AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.authenticated_user_id AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.agent_name AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.csrf_hash AS BLOB)), 0)
    WHERE state_hash = NEW.state_hash;
END;

CREATE TRIGGER pending_usage_update AFTER UPDATE OF accounted_bytes ON pending
BEGIN
    UPDATE oauth_usage
    SET bytes_used = bytes_used + (NEW.accounted_bytes - OLD.accounted_bytes)
    WHERE singleton = 1;
    UPDATE oauth_usage
    SET onboarding_bytes = onboarding_bytes + (NEW.accounted_bytes - OLD.accounted_bytes)
    WHERE singleton = 1;
END;

CREATE TRIGGER pending_usage_delete AFTER DELETE ON pending
BEGIN
    UPDATE oauth_usage
    SET bytes_used = bytes_used - OLD.accounted_bytes
    WHERE singleton = 1;
    UPDATE oauth_usage
    SET onboarding_bytes = onboarding_bytes - OLD.accounted_bytes
    WHERE singleton = 1;
END;

CREATE TRIGGER grants_charge_insert AFTER INSERT ON grants
BEGIN
    UPDATE grants
    SET accounted_bytes = 2048
        + COALESCE(length(CAST(NEW.grant_id AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.payload AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.requester_id AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.requester_id AS BLOB)), 0),
        requester_charge = 2048
        + COALESCE(length(CAST(NEW.grant_id AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.payload AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.requester_id AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.requester_id AS BLOB)), 0)
        + COALESCE((
            SELECT accounted_bytes FROM clients
            WHERE client_id = json_extract(NEW.payload, '$.client_id')
        ), 0)
    WHERE grant_id = NEW.grant_id;
END;

CREATE TRIGGER grants_charge_change AFTER UPDATE OF grant_id, payload, requester_id ON grants
BEGIN
    UPDATE grants
    SET accounted_bytes = 2048
        + COALESCE(length(CAST(NEW.grant_id AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.payload AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.requester_id AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.requester_id AS BLOB)), 0),
        requester_charge = 2048
        + COALESCE(length(CAST(NEW.grant_id AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.payload AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.requester_id AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.requester_id AS BLOB)), 0)
        + COALESCE((
            SELECT accounted_bytes FROM clients
            WHERE client_id = json_extract(NEW.payload, '$.client_id')
        ), 0)
    WHERE grant_id = NEW.grant_id;
END;

CREATE TRIGGER grants_usage_update AFTER UPDATE OF accounted_bytes, requester_charge ON grants
BEGIN
    UPDATE oauth_usage
    SET bytes_used = bytes_used + (NEW.accounted_bytes - OLD.accounted_bytes)
    WHERE singleton = 1;
    INSERT INTO requester_usage(requester_id, bytes_used)
    VALUES (OLD.requester_id, NEW.requester_charge - OLD.requester_charge)
    ON CONFLICT(requester_id)
    DO UPDATE SET bytes_used = bytes_used + excluded.bytes_used;
    DELETE FROM requester_usage
    WHERE requester_id = OLD.requester_id AND bytes_used = 0;
END;

CREATE TRIGGER grants_usage_delete AFTER DELETE ON grants
BEGIN
    UPDATE oauth_usage
    SET bytes_used = bytes_used - OLD.accounted_bytes
    WHERE singleton = 1;
    INSERT INTO requester_usage(requester_id, bytes_used)
    VALUES (OLD.requester_id, -OLD.requester_charge)
    ON CONFLICT(requester_id)
    DO UPDATE SET bytes_used = bytes_used + excluded.bytes_used;
    DELETE FROM requester_usage
    WHERE requester_id = OLD.requester_id AND bytes_used = 0;
END;

CREATE TRIGGER capabilities_charge_insert AFTER INSERT ON capabilities
BEGIN
    UPDATE capabilities
    SET accounted_bytes = 1024
        + COALESCE(length(CAST(NEW.token_hash AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.kind AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.grant_id AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.payload AS BLOB)), 0)
    WHERE token_hash = NEW.token_hash;
END;

CREATE TRIGGER capabilities_charge_change
AFTER UPDATE OF token_hash, kind, grant_id, payload ON capabilities
BEGIN
    UPDATE capabilities
    SET accounted_bytes = 1024
        + COALESCE(length(CAST(NEW.token_hash AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.kind AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.grant_id AS BLOB)), 0)
        + COALESCE(length(CAST(NEW.payload AS BLOB)), 0)
    WHERE token_hash = NEW.token_hash;
END;

CREATE TRIGGER capabilities_usage_update AFTER UPDATE OF accounted_bytes ON capabilities
BEGIN
    UPDATE oauth_usage
    SET bytes_used = bytes_used + (NEW.accounted_bytes - OLD.accounted_bytes)
    WHERE singleton = 1;
    INSERT INTO requester_usage(requester_id, bytes_used)
    VALUES (
        (SELECT requester_id FROM grants WHERE grant_id = OLD.grant_id),
        NEW.accounted_bytes - OLD.accounted_bytes
    )
    ON CONFLICT(requester_id)
    DO UPDATE SET bytes_used = bytes_used + excluded.bytes_used;
    DELETE FROM requester_usage
    WHERE requester_id = (SELECT requester_id FROM grants WHERE grant_id = OLD.grant_id)
      AND bytes_used = 0;
END;

CREATE TRIGGER capabilities_usage_delete AFTER DELETE ON capabilities
BEGIN
    UPDATE oauth_usage
    SET bytes_used = bytes_used - OLD.accounted_bytes
    WHERE singleton = 1;
    INSERT INTO requester_usage(requester_id, bytes_used)
    VALUES (
        (SELECT requester_id FROM grants WHERE grant_id = OLD.grant_id),
        -OLD.accounted_bytes
    )
    ON CONFLICT(requester_id)
    DO UPDATE SET bytes_used = bytes_used + excluded.bytes_used;
    DELETE FROM requester_usage
    WHERE requester_id = (SELECT requester_id FROM grants WHERE grant_id = OLD.grant_id)
      AND bytes_used = 0;
END;

CREATE TRIGGER grants_owner_immutable BEFORE UPDATE OF requester_id ON grants
WHEN OLD.requester_id != NEW.requester_id
BEGIN
    SELECT RAISE(ABORT, 'Grant requester is immutable');
END;

CREATE TRIGGER grants_pin_client AFTER INSERT ON grants
WHEN NOT EXISTS (
    SELECT 1 FROM grants
    WHERE json_extract(payload, '$.client_id') = json_extract(NEW.payload, '$.client_id')
      AND grant_id != NEW.grant_id
)
BEGIN
    UPDATE oauth_usage
    SET onboarding_bytes = onboarding_bytes - COALESCE((
        SELECT accounted_bytes FROM clients
        WHERE client_id = json_extract(NEW.payload, '$.client_id')
    ), 0)
    WHERE singleton = 1;
END;

CREATE TRIGGER grants_unpin_client AFTER DELETE ON grants
WHEN NOT EXISTS (
    SELECT 1 FROM grants
    WHERE json_extract(payload, '$.client_id') = json_extract(OLD.payload, '$.client_id')
)
BEGIN
    UPDATE oauth_usage
    SET onboarding_bytes = onboarding_bytes + COALESCE((
        SELECT accounted_bytes FROM clients
        WHERE client_id = json_extract(OLD.payload, '$.client_id')
    ), 0)
    WHERE singleton = 1;
END;

INSERT INTO oauth_usage VALUES (1, 1024, 0);
PRAGMA user_version = 1;
