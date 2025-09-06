-- Database functions for MindRoom SaaS platform
-- Provides utility functions for common operations

-- Function to get user's active instance
CREATE OR REPLACE FUNCTION get_user_instance(user_id UUID)
RETURNS TABLE (
    instance_id UUID,
    subdomain TEXT,
    frontend_url TEXT,
    backend_url TEXT,
    matrix_server_url TEXT,
    status instance_status,
    tier subscription_tier,
    config JSONB,
    features JSONB
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        i.id,
        i.subdomain,
        i.frontend_url,
        i.backend_url,
        i.matrix_server_url,
        i.status,
        s.tier,
        i.config,
        s.features
    FROM instances i
    JOIN subscriptions s ON i.subscription_id = s.id
    WHERE s.account_id = user_id
    AND s.status = 'active'
    AND i.status = 'running'
    ORDER BY i.created_at DESC
    LIMIT 1;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to get all user instances (including inactive)
CREATE OR REPLACE FUNCTION get_all_user_instances(user_id UUID)
RETURNS TABLE (
    instance_id UUID,
    subdomain TEXT,
    status instance_status,
    subscription_status subscription_status,
    tier subscription_tier,
    created_at TIMESTAMPTZ
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        i.id,
        i.subdomain,
        i.status,
        s.status,
        s.tier,
        i.created_at
    FROM instances i
    JOIN subscriptions s ON i.subscription_id = s.id
    WHERE s.account_id = user_id
    ORDER BY i.created_at DESC;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to track daily usage
CREATE OR REPLACE FUNCTION track_usage(
    p_instance_id UUID,
    p_agent_name TEXT DEFAULT NULL,
    p_tool_name TEXT DEFAULT NULL,
    p_platform TEXT DEFAULT NULL,
    p_message_type TEXT DEFAULT 'sent' -- 'sent' or 'received'
) RETURNS void AS $$
DECLARE
    v_subscription_id UUID;
    v_max_messages INTEGER;
    v_current_messages INTEGER;
BEGIN
    -- Get subscription info
    SELECT i.subscription_id INTO v_subscription_id
    FROM instances i
    WHERE i.id = p_instance_id;

    IF v_subscription_id IS NULL THEN
        RAISE EXCEPTION 'Instance not found: %', p_instance_id;
    END IF;

    -- Insert or update today's metrics
    INSERT INTO usage_metrics (
        instance_id,
        date,
        messages_sent,
        messages_received,
        agents_used,
        tools_used,
        platforms_active
    )
    VALUES (
        p_instance_id,
        CURRENT_DATE,
        CASE WHEN p_message_type = 'sent' THEN 1 ELSE 0 END,
        CASE WHEN p_message_type = 'received' THEN 1 ELSE 0 END,
        CASE WHEN p_agent_name IS NOT NULL
             THEN jsonb_build_object(p_agent_name, 1)
             ELSE '{}'::jsonb
        END,
        CASE WHEN p_tool_name IS NOT NULL
             THEN jsonb_build_object(p_tool_name, 1)
             ELSE '{}'::jsonb
        END,
        CASE WHEN p_platform IS NOT NULL
             THEN jsonb_build_object(p_platform, true)
             ELSE '{}'::jsonb
        END
    )
    ON CONFLICT (instance_id, date) DO UPDATE
    SET
        messages_sent = usage_metrics.messages_sent +
            CASE WHEN p_message_type = 'sent' THEN 1 ELSE 0 END,
        messages_received = usage_metrics.messages_received +
            CASE WHEN p_message_type = 'received' THEN 1 ELSE 0 END,
        agents_used = CASE
            WHEN p_agent_name IS NOT NULL THEN
                usage_metrics.agents_used ||
                jsonb_build_object(p_agent_name,
                    COALESCE((usage_metrics.agents_used->>p_agent_name)::int, 0) + 1)
            ELSE usage_metrics.agents_used
        END,
        tools_used = CASE
            WHEN p_tool_name IS NOT NULL THEN
                usage_metrics.tools_used ||
                jsonb_build_object(p_tool_name,
                    COALESCE((usage_metrics.tools_used->>p_tool_name)::int, 0) + 1)
            ELSE usage_metrics.tools_used
        END,
        platforms_active = CASE
            WHEN p_platform IS NOT NULL THEN
                usage_metrics.platforms_active ||
                jsonb_build_object(p_platform, true)
            ELSE usage_metrics.platforms_active
        END;

    -- Reset daily counter if needed
    UPDATE subscriptions
    SET
        last_reset_at = CASE
            WHEN last_reset_at < CURRENT_DATE THEN CURRENT_DATE
            ELSE last_reset_at
        END,
        current_messages_today = CASE
            WHEN last_reset_at < CURRENT_DATE THEN 1
            ELSE current_messages_today + 1
        END
    WHERE id = v_subscription_id;

    -- Check if user has exceeded daily limit
    SELECT max_messages_per_day, current_messages_today
    INTO v_max_messages, v_current_messages
    FROM subscriptions
    WHERE id = v_subscription_id;

    IF v_current_messages > v_max_messages THEN
        -- Log the over-limit event
        INSERT INTO audit_logs (
            account_id,
            instance_id,
            action,
            action_category,
            details
        )
        SELECT
            s.account_id,
            p_instance_id,
            'message_limit_exceeded',
            'usage',
            jsonb_build_object(
                'limit', v_max_messages,
                'current', v_current_messages,
                'date', CURRENT_DATE
            )
        FROM subscriptions s
        WHERE s.id = v_subscription_id;
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to check usage limits
CREATE OR REPLACE FUNCTION check_usage_limits(p_instance_id UUID)
RETURNS TABLE (
    is_within_limits BOOLEAN,
    messages_remaining INTEGER,
    daily_limit INTEGER,
    storage_used_gb DECIMAL(10,3),
    storage_limit_gb INTEGER
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        s.current_messages_today <= s.max_messages_per_day,
        GREATEST(0, s.max_messages_per_day - s.current_messages_today),
        s.max_messages_per_day,
        s.current_storage_gb,
        s.max_storage_gb
    FROM instances i
    JOIN subscriptions s ON i.subscription_id = s.id
    WHERE i.id = p_instance_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to provision a new instance
CREATE OR REPLACE FUNCTION provision_instance(
    p_subscription_id UUID,
    p_config JSONB DEFAULT NULL
) RETURNS UUID AS $$
DECLARE
    v_instance_id UUID;
    v_app_name TEXT;
    v_subdomain TEXT;
BEGIN
    -- Generate unique identifiers
    v_instance_id := gen_random_uuid();
    v_app_name := 'mindroom-' || substring(v_instance_id::text, 1, 8);
    v_subdomain := 'mr-' || substring(v_instance_id::text, 1, 8);

    -- Create instance record
    INSERT INTO instances (
        id,
        subscription_id,
        dokku_app_name,
        subdomain,
        status,
        config
    ) VALUES (
        v_instance_id,
        p_subscription_id,
        v_app_name,
        v_subdomain,
        'provisioning',
        COALESCE(p_config, '{
            "agents": {},
            "teams": {},
            "tools": {},
            "models": {},
            "rooms": []
        }'::jsonb)
    );

    -- Log the provisioning
    INSERT INTO audit_logs (
        account_id,
        instance_id,
        action,
        action_category,
        details
    )
    SELECT
        s.account_id,
        v_instance_id,
        'instance_provisioning_started',
        'instance',
        jsonb_build_object(
            'subscription_id', p_subscription_id,
            'app_name', v_app_name,
            'subdomain', v_subdomain
        )
    FROM subscriptions s
    WHERE s.id = p_subscription_id;

    RETURN v_instance_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to deprovision an instance
CREATE OR REPLACE FUNCTION deprovision_instance(
    p_instance_id UUID,
    p_reason TEXT DEFAULT NULL
) RETURNS void AS $$
BEGIN
    -- Update instance status
    UPDATE instances
    SET
        status = 'deprovisioning',
        updated_at = NOW()
    WHERE id = p_instance_id;

    -- Log the deprovisioning
    INSERT INTO audit_logs (
        account_id,
        instance_id,
        action,
        action_category,
        details
    )
    SELECT
        s.account_id,
        p_instance_id,
        'instance_deprovisioning_started',
        'instance',
        jsonb_build_object(
            'reason', COALESCE(p_reason, 'manual'),
            'timestamp', NOW()
        )
    FROM instances i
    JOIN subscriptions s ON i.subscription_id = s.id
    WHERE i.id = p_instance_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to update instance health
CREATE OR REPLACE FUNCTION update_instance_health(
    p_instance_id UUID,
    p_health_status TEXT,
    p_health_details JSONB DEFAULT NULL,
    p_error_message TEXT DEFAULT NULL
) RETURNS void AS $$
BEGIN
    UPDATE instances
    SET
        last_health_check = NOW(),
        health_status = p_health_status,
        health_details = COALESCE(p_health_details, '{}'::jsonb),
        error_message = p_error_message,
        updated_at = NOW()
    WHERE id = p_instance_id;

    -- If health is critical, log it
    IF p_health_status IN ('critical', 'failed') THEN
        INSERT INTO audit_logs (
            account_id,
            instance_id,
            action,
            action_category,
            details,
            success
        )
        SELECT
            s.account_id,
            p_instance_id,
            'instance_health_critical',
            'instance',
            jsonb_build_object(
                'status', p_health_status,
                'details', p_health_details,
                'error', p_error_message
            ),
            false
        FROM instances i
        JOIN subscriptions s ON i.subscription_id = s.id
        WHERE i.id = p_instance_id;
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to get usage statistics for billing
CREATE OR REPLACE FUNCTION get_billing_metrics(
    p_account_id UUID,
    p_start_date DATE,
    p_end_date DATE
) RETURNS TABLE (
    total_messages INTEGER,
    total_storage_gb DECIMAL(10,3),
    unique_agents INTEGER,
    unique_tools INTEGER,
    active_days INTEGER,
    average_daily_messages DECIMAL(10,2)
) AS $$
BEGIN
    RETURN QUERY
    WITH metrics AS (
        SELECT
            SUM(um.messages_sent + um.messages_received) as total_messages,
            COUNT(DISTINCT um.date) as active_days,
            COUNT(DISTINCT jsonb_object_keys(um.agents_used)) as unique_agents,
            COUNT(DISTINCT jsonb_object_keys(um.tools_used)) as unique_tools
        FROM usage_metrics um
        JOIN instances i ON um.instance_id = i.id
        JOIN subscriptions s ON i.subscription_id = s.id
        WHERE s.account_id = p_account_id
        AND um.date BETWEEN p_start_date AND p_end_date
    ),
    storage AS (
        SELECT MAX(current_storage_gb) as max_storage
        FROM subscriptions
        WHERE account_id = p_account_id
    )
    SELECT
        COALESCE(m.total_messages, 0)::INTEGER,
        COALESCE(st.max_storage, 0.0),
        COALESCE(m.unique_agents, 0)::INTEGER,
        COALESCE(m.unique_tools, 0)::INTEGER,
        COALESCE(m.active_days, 0)::INTEGER,
        CASE
            WHEN m.active_days > 0 THEN
                ROUND(m.total_messages::DECIMAL / m.active_days, 2)
            ELSE 0.0
        END
    FROM metrics m, storage st;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Function to handle Stripe webhook events
CREATE OR REPLACE FUNCTION handle_stripe_event(
    p_event_type TEXT,
    p_event_data JSONB
) RETURNS void AS $$
DECLARE
    v_account_id UUID;
    v_subscription_id UUID;
BEGIN
    CASE p_event_type
        WHEN 'customer.created' THEN
            -- Create or update account
            INSERT INTO accounts (email, stripe_customer_id)
            VALUES (
                p_event_data->>'email',
                p_event_data->>'id'
            )
            ON CONFLICT (stripe_customer_id) DO UPDATE
            SET email = EXCLUDED.email,
                updated_at = NOW();

        WHEN 'customer.subscription.created' THEN
            -- Get account ID
            SELECT id INTO v_account_id
            FROM accounts
            WHERE stripe_customer_id = p_event_data->'customer'->>'id';

            -- Create subscription
            INSERT INTO subscriptions (
                account_id,
                stripe_subscription_id,
                stripe_price_id,
                status,
                current_period_start,
                current_period_end
            )
            VALUES (
                v_account_id,
                p_event_data->>'id',
                p_event_data->'items'->'data'->0->'price'->>'id',
                (p_event_data->>'status')::subscription_status,
                to_timestamp((p_event_data->>'current_period_start')::bigint),
                to_timestamp((p_event_data->>'current_period_end')::bigint)
            );

        WHEN 'customer.subscription.updated' THEN
            -- Update subscription
            UPDATE subscriptions
            SET
                status = (p_event_data->>'status')::subscription_status,
                current_period_start = to_timestamp((p_event_data->>'current_period_start')::bigint),
                current_period_end = to_timestamp((p_event_data->>'current_period_end')::bigint),
                updated_at = NOW()
            WHERE stripe_subscription_id = p_event_data->>'id';

        WHEN 'customer.subscription.deleted' THEN
            -- Cancel subscription
            UPDATE subscriptions
            SET
                status = 'cancelled',
                cancelled_at = NOW(),
                updated_at = NOW()
            WHERE stripe_subscription_id = p_event_data->>'id';

            -- Get instances to deprovision
            FOR v_subscription_id IN
                SELECT id FROM subscriptions
                WHERE stripe_subscription_id = p_event_data->>'id'
            LOOP
                PERFORM deprovision_instance(
                    i.id,
                    'subscription_cancelled'
                )
                FROM instances i
                WHERE i.subscription_id = v_subscription_id;
            END LOOP;
    END CASE;

    -- Log the event
    INSERT INTO audit_logs (
        account_id,
        action,
        action_category,
        details
    )
    VALUES (
        v_account_id,
        p_event_type,
        'billing',
        p_event_data
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- Comments for documentation
COMMENT ON FUNCTION get_user_instance IS 'Get the active instance for a user';
COMMENT ON FUNCTION track_usage IS 'Track usage metrics for billing and monitoring';
COMMENT ON FUNCTION check_usage_limits IS 'Check if instance is within usage limits';
COMMENT ON FUNCTION provision_instance IS 'Provision a new MindRoom instance';
COMMENT ON FUNCTION deprovision_instance IS 'Deprovision an existing instance';
COMMENT ON FUNCTION update_instance_health IS 'Update instance health status';
COMMENT ON FUNCTION get_billing_metrics IS 'Get billing metrics for a date range';
COMMENT ON FUNCTION handle_stripe_event IS 'Handle incoming Stripe webhook events';
