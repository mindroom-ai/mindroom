-- Triggers and automation for MindRoom SaaS platform
-- Handles automatic processes and data integrity

-- Function to automatically reset daily usage counters
CREATE OR REPLACE FUNCTION reset_daily_usage()
RETURNS void AS $$
BEGIN
    UPDATE subscriptions
    SET
        current_messages_today = 0,
        last_reset_at = CURRENT_DATE
    WHERE last_reset_at < CURRENT_DATE;
END;
$$ LANGUAGE plpgsql;

-- Function to calculate and update storage usage
CREATE OR REPLACE FUNCTION calculate_storage_usage(p_instance_id UUID)
RETURNS DECIMAL(10,3) AS $$
DECLARE
    v_total_storage DECIMAL(10,3);
BEGIN
    -- Calculate storage from latest metrics
    SELECT COALESCE(SUM(storage_used_mb) / 1024.0, 0.0)
    INTO v_total_storage
    FROM usage_metrics
    WHERE instance_id = p_instance_id
    AND date = CURRENT_DATE;

    -- Update subscription storage
    UPDATE subscriptions s
    SET current_storage_gb = v_total_storage
    FROM instances i
    WHERE i.subscription_id = s.id
    AND i.id = p_instance_id;

    RETURN v_total_storage;
END;
$$ LANGUAGE plpgsql;

-- Function to handle subscription tier changes
CREATE OR REPLACE FUNCTION handle_tier_change()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.tier != NEW.tier THEN
        -- Update limits based on new tier
        CASE NEW.tier
            WHEN 'free' THEN
                NEW.max_agents := 1;
                NEW.max_messages_per_day := 100;
                NEW.max_storage_gb := 1;
                NEW.max_platforms := 1;
                NEW.max_team_members := 1;
                NEW.features := '{
                    "custom_agents": false,
                    "api_access": false,
                    "priority_support": false,
                    "advanced_memory": false,
                    "voice_messages": false,
                    "file_uploads": false,
                    "team_collaboration": false
                }'::jsonb;

            WHEN 'starter' THEN
                NEW.max_agents := 3;
                NEW.max_messages_per_day := 1000;
                NEW.max_storage_gb := 5;
                NEW.max_platforms := 3;
                NEW.max_team_members := 1;
                NEW.features := '{
                    "custom_agents": false,
                    "api_access": true,
                    "priority_support": false,
                    "advanced_memory": true,
                    "voice_messages": true,
                    "file_uploads": true,
                    "team_collaboration": false
                }'::jsonb;

            WHEN 'professional' THEN
                NEW.max_agents := 10;
                NEW.max_messages_per_day := 10000;
                NEW.max_storage_gb := 50;
                NEW.max_platforms := 10;
                NEW.max_team_members := 5;
                NEW.features := '{
                    "custom_agents": true,
                    "api_access": true,
                    "priority_support": true,
                    "advanced_memory": true,
                    "voice_messages": true,
                    "file_uploads": true,
                    "team_collaboration": true
                }'::jsonb;

            WHEN 'enterprise' THEN
                NEW.max_agents := 999;
                NEW.max_messages_per_day := 999999;
                NEW.max_storage_gb := 999;
                NEW.max_platforms := 999;
                NEW.max_team_members := 999;
                NEW.features := '{
                    "custom_agents": true,
                    "api_access": true,
                    "priority_support": true,
                    "advanced_memory": true,
                    "voice_messages": true,
                    "file_uploads": true,
                    "team_collaboration": true
                }'::jsonb;
        END CASE;

        -- Log tier change
        INSERT INTO audit_logs (
            account_id,
            action,
            action_category,
            details
        )
        VALUES (
            NEW.account_id,
            'subscription_tier_changed',
            'billing',
            jsonb_build_object(
                'old_tier', OLD.tier,
                'new_tier', NEW.tier,
                'new_limits', jsonb_build_object(
                    'max_agents', NEW.max_agents,
                    'max_messages_per_day', NEW.max_messages_per_day,
                    'max_storage_gb', NEW.max_storage_gb
                )
            )
        );

        -- Update instance resource limits based on new tier
        UPDATE instances i
        SET
            memory_limit_mb = CASE NEW.tier
                WHEN 'free' THEN 512
                WHEN 'starter' THEN 1024
                WHEN 'professional' THEN 2048
                WHEN 'enterprise' THEN 4096
            END,
            cpu_limit = CASE NEW.tier
                WHEN 'free' THEN 0.5
                WHEN 'starter' THEN 1.0
                WHEN 'professional' THEN 2.0
                WHEN 'enterprise' THEN 4.0
            END,
            disk_limit_gb = NEW.max_storage_gb
        WHERE i.subscription_id = NEW.id;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger for tier changes
CREATE TRIGGER subscription_tier_change
    BEFORE UPDATE OF tier ON subscriptions
    FOR EACH ROW
    EXECUTE FUNCTION handle_tier_change();

-- Function to check and enforce usage limits
CREATE OR REPLACE FUNCTION enforce_usage_limits()
RETURNS TRIGGER AS $$
DECLARE
    v_max_messages INTEGER;
    v_current_messages INTEGER;
BEGIN
    -- Check if we need to reset the counter
    IF OLD.last_reset_at < CURRENT_DATE THEN
        NEW.current_messages_today := 1;
        NEW.last_reset_at := CURRENT_DATE;
    END IF;

    -- Check message limits
    IF NEW.current_messages_today > NEW.max_messages_per_day THEN
        -- Mark instances as rate-limited
        UPDATE instances
        SET
            health_status = 'rate_limited',
            health_details = jsonb_build_object(
                'reason', 'daily_message_limit_exceeded',
                'limit', NEW.max_messages_per_day,
                'current', NEW.current_messages_today
            )
        WHERE subscription_id = NEW.id
        AND status = 'running';

        -- Log rate limit event
        INSERT INTO audit_logs (
            account_id,
            action,
            action_category,
            details,
            success
        )
        VALUES (
            NEW.account_id,
            'rate_limit_exceeded',
            'usage',
            jsonb_build_object(
                'type', 'messages',
                'limit', NEW.max_messages_per_day,
                'current', NEW.current_messages_today
            ),
            false
        );
    END IF;

    -- Check storage limits
    IF NEW.current_storage_gb > NEW.max_storage_gb THEN
        UPDATE instances
        SET
            health_status = 'storage_exceeded',
            health_details = jsonb_build_object(
                'reason', 'storage_limit_exceeded',
                'limit_gb', NEW.max_storage_gb,
                'current_gb', NEW.current_storage_gb
            )
        WHERE subscription_id = NEW.id;

        INSERT INTO audit_logs (
            account_id,
            action,
            action_category,
            details,
            success
        )
        VALUES (
            NEW.account_id,
            'storage_limit_exceeded',
            'usage',
            jsonb_build_object(
                'limit_gb', NEW.max_storage_gb,
                'current_gb', NEW.current_storage_gb
            ),
            false
        );
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger for usage limit enforcement
CREATE TRIGGER enforce_subscription_limits
    BEFORE UPDATE ON subscriptions
    FOR EACH ROW
    WHEN (OLD.current_messages_today IS DISTINCT FROM NEW.current_messages_today
          OR OLD.current_storage_gb IS DISTINCT FROM NEW.current_storage_gb)
    EXECUTE FUNCTION enforce_usage_limits();

-- Function to handle instance status changes
CREATE OR REPLACE FUNCTION handle_instance_status_change()
RETURNS TRIGGER AS $$
BEGIN
    -- Log status changes
    IF OLD.status != NEW.status THEN
        INSERT INTO audit_logs (
            account_id,
            instance_id,
            action,
            action_category,
            details
        )
        SELECT
            s.account_id,
            NEW.id,
            'instance_status_changed',
            'instance',
            jsonb_build_object(
                'old_status', OLD.status,
                'new_status', NEW.status,
                'subdomain', NEW.subdomain
            )
        FROM subscriptions s
        WHERE s.id = NEW.subscription_id;

        -- Update lifecycle timestamps
        CASE NEW.status
            WHEN 'running' THEN
                NEW.last_started_at := NOW();
                NEW.provisioned_at := COALESCE(NEW.provisioned_at, NOW());
            WHEN 'stopped' THEN
                NEW.last_stopped_at := NOW();
            WHEN 'deprovisioning' THEN
                NEW.deprovisioned_at := NOW();
        END CASE;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger for instance status changes
CREATE TRIGGER instance_status_change
    BEFORE UPDATE OF status ON instances
    FOR EACH ROW
    EXECUTE FUNCTION handle_instance_status_change();

-- Function to clean up expired data
CREATE OR REPLACE FUNCTION cleanup_expired_data()
RETURNS void AS $$
BEGIN
    -- Delete old audit logs (keep 90 days)
    DELETE FROM audit_logs
    WHERE created_at < NOW() - INTERVAL '90 days';

    -- Delete old usage metrics (keep 365 days)
    DELETE FROM usage_metrics
    WHERE date < CURRENT_DATE - INTERVAL '365 days';

    -- Delete expired backups
    DELETE FROM instance_backups
    WHERE expires_at < NOW()
    AND status = 'completed';

    -- Mark expired API keys as inactive
    UPDATE api_keys
    SET is_active = false
    WHERE expires_at < NOW()
    AND is_active = true;
END;
$$ LANGUAGE plpgsql;

-- Function to calculate uptime percentage
CREATE OR REPLACE FUNCTION calculate_uptime()
RETURNS void AS $$
BEGIN
    UPDATE instances
    SET uptime_percentage =
        CASE
            WHEN last_started_at IS NULL THEN 0
            WHEN status = 'running' THEN
                LEAST(100,
                    EXTRACT(EPOCH FROM (NOW() - COALESCE(last_stopped_at, last_started_at))) /
                    EXTRACT(EPOCH FROM (NOW() - last_started_at)) * 100
                )
            ELSE uptime_percentage
        END
    WHERE status IN ('running', 'stopped');
END;
$$ LANGUAGE plpgsql;

-- Function to auto-pause inactive instances
CREATE OR REPLACE FUNCTION auto_pause_inactive_instances()
RETURNS void AS $$
BEGIN
    UPDATE instances
    SET
        status = 'stopped',
        health_details = jsonb_build_object(
            'reason', 'auto_paused_inactive',
            'last_activity', last_health_check
        )
    WHERE status = 'running'
    AND last_health_check < NOW() - INTERVAL '7 days'
    AND subscription_id IN (
        SELECT id FROM subscriptions
        WHERE tier = 'free'
    );

    -- Log auto-pause events
    INSERT INTO audit_logs (
        account_id,
        instance_id,
        action,
        action_category,
        details
    )
    SELECT
        s.account_id,
        i.id,
        'instance_auto_paused',
        'instance',
        jsonb_build_object(
            'reason', 'inactivity',
            'last_activity', i.last_health_check
        )
    FROM instances i
    JOIN subscriptions s ON i.subscription_id = s.id
    WHERE i.status = 'stopped'
    AND i.health_details->>'reason' = 'auto_paused_inactive';
END;
$$ LANGUAGE plpgsql;

-- Create scheduled jobs (using pg_cron extension if available)
-- Note: This requires pg_cron extension to be enabled
-- If pg_cron is not available, these should be run from an external scheduler

-- Schedule daily reset of usage counters (runs at midnight UTC)
-- SELECT cron.schedule('reset-daily-usage', '0 0 * * *', 'SELECT reset_daily_usage();');

-- Schedule hourly uptime calculation
-- SELECT cron.schedule('calculate-uptime', '0 * * * *', 'SELECT calculate_uptime();');

-- Schedule daily cleanup of expired data (runs at 2 AM UTC)
-- SELECT cron.schedule('cleanup-expired', '0 2 * * *', 'SELECT cleanup_expired_data();');

-- Schedule auto-pause check every 6 hours
-- SELECT cron.schedule('auto-pause-instances', '0 */6 * * *', 'SELECT auto_pause_inactive_instances();');

-- Comments for documentation
COMMENT ON FUNCTION reset_daily_usage IS 'Reset daily usage counters at midnight';
COMMENT ON FUNCTION calculate_storage_usage IS 'Calculate and update storage usage for an instance';
COMMENT ON FUNCTION handle_tier_change IS 'Handle subscription tier changes and update limits';
COMMENT ON FUNCTION enforce_usage_limits IS 'Enforce usage limits and rate limiting';
COMMENT ON FUNCTION handle_instance_status_change IS 'Track instance status changes and lifecycle';
COMMENT ON FUNCTION cleanup_expired_data IS 'Clean up old data to save storage';
COMMENT ON FUNCTION calculate_uptime IS 'Calculate instance uptime percentage';
COMMENT ON FUNCTION auto_pause_inactive_instances IS 'Automatically pause inactive free tier instances';
