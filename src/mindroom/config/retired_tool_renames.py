"""Pure rename tables for retired per-operation functions of consolidated toolkits."""

from __future__ import annotations

# LEGACY_COMPAT: Retired individual functions of consolidated toolkits.
# Legacy format: scheduler, searxng, memory, todo, Wave 2 native toolkits (calculator, duckduckgo, googlesearch,
#   serper, youtube), Wave 3b native toolkits (gmail, google_calendar, github, google_drive, google_sheets,
#   homeassistant), Wave 4 native toolkits (exa, trafilatura, brightdata, oxylabs, browserbase, spider), Wave 5 native
#   toolkits (file_generation, visualization, unsplash), Wave 6 native toolkits (csv, duckdb, sql, postgres, redshift,
#   neo4j, yfinance, openbb, financial_datasets_api, openweather, docker, e2b, daytona), Wave 7 comms toolkits (email,
#   twilio, discord, telegram, whatsapp, webex, zoom, x), Wave 8 native toolkits (linear, notion, todoist, clickup,
#   trello, bitbucket, cal_com, shopify, spotify, reddit, google_maps, brandfetch, mem0, zep), Microsoft 365 toolkits
#   (outlook_mail, outlook_calendar, microsoft_teams, microsoft_files, microsoft_sharepoint, microsoft_onenote,
#   microsoft_todo, microsoft_meetings, microsoft_people), skills, attachments, workflow, thread_tags, Google Docs /
#   Google Tasks (google_docs, google_tasks, including prefixed secondary Google Workspace plugin copies for all Google
#   Workspace toolkits), Google BigQuery (google_bigquery), Hacker News (hackernews), and runtime/session/thread
#   toolkits including script, terminal, claude_agent, thread_model, thread_snooze, thread_resolution, and usage_stats
#   previously exposed individual per-operation function names (and second-generation split names such as
#   thread_model_read/thread_model_write, knowledge_graph_read/knowledge_graph_write, serper_search, and scrape_webpage)
#   that could appear in tool_approval.rules[].match globs or include_tools/exclude_tools overrides.
# Last legacy release: v2026.11.610 (Waves 3b, 5-8: v2026.11.611-v2026.11.625; skills/attachments/M365: v2026.11.657;
#   workflow/thread_tags: v2026.11.658; google_docs/google_tasks & runtime/session/thread CRUD: v2026.11.659;
#   Wave 2/4: v2026.11.663; 7-toolkit collapse including thread_snooze, thread_resolution, hackernews, google_bigquery,
#   and second-generation thread_model/knowledge_graph/serper splits shipped in v2026.11.665-v2026.11.668: v2026.11.668);
#   replacement: unreleased; consolidated into action-dispatched and risk-scoped functions.
# Handling: When the config enables one of these toolkits (or a prefixed Google Workspace clone, or unconditionally
#   for generated skills), allow configs with an explicit exact rule deciding the replacement function, and otherwise
#   reject tool_approval.rules when a retired strict-wildcard toolkit function (skills, attachments, Microsoft 365,
#   workflow, thread_tags, Wave 2/4, script, terminal, claude_agent, thread_model, thread_snooze, thread_resolution,
#   hackernews, google_bigquery, usage_stats) is decided by a wildcard
#   approval script or any retired function's effective decision differs from the replacement's effective decision
#   (regardless of other toolkits); also reject include_tools or exclude_tools naming a retired function on a
#   consolidated toolkit entry (both authored overrides and stored dashboard settings), directing operators to the
#   replacement function alongside per-action `enable_*` settings (where available) or `tool_approval.rules` so
#   permissions are not broadened.
# Coverage:
#   tests/test_tool_approval_grants.py::test_retired_consolidated_approval_rules_fail_closed,
#   tests/test_tool_approval_grants.py::test_retired_consolidated_approval_rules_fail_closed_across_agents_sharing_name,
#   tests/test_tool_approval_grants.py::test_secondary_google_workspace_retired_approval_rules_fail_closed,
#   tests/test_tool_approval_grants.py::test_microsoft_365_retired_approval_rules_fail_closed,
#   tests/test_tool_approval_grants.py::test_outlook_mail_send_and_reply_migration_rules_fail_closed,
#   tests/test_tool_approval_grants.py::test_wildcard_approval_script_rule_fails_closed_for_retired_attachment_and_skill_names,
#   tests/test_tool_approval_grants.py::test_wildcard_approval_script_rule_fails_closed_for_wave2_and_wave4_toolkits,
#   tests/test_tool_approval_grants.py::test_wildcard_approval_script_rule_fails_closed_for_retired_workflow_and_thread_tags_names,
#   tests/test_tool_approval_grants.py::test_wildcard_approval_script_rule_fails_closed_for_retired_microsoft_365_names,
#   tests/test_tool_approval_grants.py::test_retired_attachments_include_exclude_overrides_fail_config_load,
#   tests/test_tool_approval_grants.py::test_runtime_crud_action_scoped_grants_and_fail_closed_migration,
#   tests/api/test_plugin_room_agents.py::test_retired_stored_claude_agent_filters_fail_closed_and_block_workspace_write_grant,
#   tests/api/test_plugin_room_agents.py::test_retired_stored_tool_filters_fail_closed_across_consolidated_toolkits,
#   tests/test_wave6_data_tools.py::test_wave6_batch1_action_scoped_grants_and_fail_closed_migration,
#   tests/test_wave6_db_tools.py::test_wave6_batch2_action_scoped_grants_and_fail_closed_migration,
#   tests/test_wave6_finance_weather_tools.py::test_wave6_batch3_action_scoped_grants_and_fail_closed_migration,
#   tests/test_wave6_sandbox_tools.py::test_wave6_batch4_action_scoped_grants_and_fail_closed_migration,
#   tests/test_wave2_search_tools.py::test_wave2_action_scoped_grants_and_fail_closed_migration,
#   tests/test_brightdata_tool.py::test_wave4_action_scoped_grants_and_fail_closed_migration.
_CONSOLIDATED_TOOLKIT_RENAMES: dict[str, dict[str, str]] = {
    "scheduler": {
        "schedule": "scheduler",
        "edit_schedule": "scheduler",
        "list_schedules": "scheduler",
        "cancel_schedule": "scheduler",
    },
    "searxng": {
        "search_web": "searxng_search",
        "image_search": "searxng_search",
        "it_search": "searxng_search",
        "map_search": "searxng_search",
        "music_search": "searxng_search",
        "news_search": "searxng_search",
        "science_search": "searxng_search",
        "video_search": "searxng_search",
    },
    "memory": {
        "add_memory": "memory",
        "search_memories": "memory",
        "list_memories": "memory",
        "get_memory": "memory",
        "update_memory": "memory",
        "delete_memory": "memory",
    },
    "calculator": {
        "add": "calculate",
        "subtract": "calculate",
        "multiply": "calculate",
        "divide": "calculate",
        "exponentiate": "calculate",
        "factorial": "calculate",
        "is_prime": "calculate",
        "square_root": "calculate",
    },
    "duckduckgo": {
        "web_search": "duckduckgo_search",
        "search_news": "duckduckgo_search",
    },
    "googlesearch": {
        "web_search": "google_search",
        "search_news": "google_search",
    },
    "serper": {
        "search_web": "serper",
        "search_news": "serper",
        "search_scholar": "serper",
        "serper_search": "serper",
        "scrape_webpage": "serper",
    },
    "hackernews": {
        "get_top_hackernews_stories": "hackernews",
        "get_user_details": "hackernews",
    },
    "google_bigquery": {
        "list_tables": "google_bigquery_inspect",
        "describe_table": "google_bigquery_inspect",
    },
    "youtube": {
        "get_youtube_video_captions": "youtube_video",
        "get_youtube_video_data": "youtube_video",
        "get_video_timestamps": "youtube_video",
    },
    "todo": {
        "plan": "todo",
        "add_todo": "todo",
        "list_todos": "todo",
        "update_todo": "todo",
        "apply_template": "todo",
        "list_templates": "todo",
    },
    # LEGACY_COMPAT: Retired individual functions of the triggers (external_trigger_manager) toolkit.
    # Legacy format: triggers / external_trigger_manager exposed 8 separate functions (watch_room, watch_thread,
    #   subscribe_event, create_trigger, list_triggers, disable_trigger, delete_trigger, rotate_trigger_key) and
    #   later a separate watch_pr function, which could appear in tool_approval.rules[].match globs or
    #   include_tools/exclude_tools overrides.
    # Last legacy release: v2026.11.480 (original 8) and v2026.11.633 (watch_pr); replacement: consolidated into
    #   `triggers`, then split into `triggers_read` and `triggers_write`.
    # Handling: Reject retired trigger function names in include_tools/exclude_tools and fail closed in
    #   tool_approval.rules when a retired function's effective decision differs from its replacement function.
    # Coverage: tests/test_triggers_tool.py, tests/test_tool_approval_grants.py.
    "triggers": {
        "watch_room": "triggers_write",
        "watch_thread": "triggers_write",
        "watch_pr": "triggers_write",
        "subscribe_event": "triggers_write",
        "create_trigger": "triggers_write",
        "list_triggers": "triggers_read",
        "disable_trigger": "triggers_write",
        "delete_trigger": "triggers_write",
        "rotate_trigger_key": "triggers_write",
    },
    "workflow": {
        "start_workflow": "workflow_write",
        "get_workflow": "workflow_read",
        "cancel_workflow": "workflow_write",
        "resume_workflow": "workflow_write",
        "list_workflow_runs": "workflow_read",
        "save_workflow": "workflow_write",
    },
    "thread_tags": {
        "tag_thread": "thread_tags",
        "untag_thread": "thread_tags",
        "list_thread_tags": "thread_tags",
    },
    "gmail": {
        "get_latest_emails": "gmail_read",
        "get_emails_from_user": "gmail_read",
        "get_unread_emails": "gmail_read",
        "get_starred_emails": "gmail_read",
        "get_emails_by_context": "gmail_read",
        "get_emails_by_date": "gmail_read",
        "get_emails_by_thread": "gmail_read",
        "search_emails": "gmail_read",
        "get_message": "gmail_read",
        "get_thread": "gmail_read",
        "search_threads": "gmail_read",
        "list_custom_labels": "gmail_read",
        "get_draft": "gmail_read",
        "list_drafts": "gmail_read",
        "create_draft_email": "gmail_draft",
        "update_draft": "gmail_draft",
        "send_email": "gmail_send",
        "send_email_reply": "gmail_send",
        "send_email_to_self": "gmail_send_to_self",
        "mark_email_as_read": "gmail_manage",
        "mark_email_as_unread": "gmail_manage",
        "star_email": "gmail_manage",
        "unstar_email": "gmail_manage",
        "apply_label": "gmail_manage",
        "remove_label": "gmail_manage",
        "delete_custom_label": "gmail_manage",
    },
    "google_calendar": {
        "list_calendars": "google_calendar_read",
        "list_events": "google_calendar_read",
        "fetch_all_events": "google_calendar_read",
        "get_event": "google_calendar_read",
        "search_events": "google_calendar_read",
        "get_event_attendees": "google_calendar_read",
        "check_availability": "google_calendar_read",
        "find_available_slots": "google_calendar_read",
        "create_event": "google_calendar_write",
        "update_event": "google_calendar_write",
        "delete_event": "google_calendar_write",
        "quick_add_event": "google_calendar_write",
        "respond_to_event": "google_calendar_write",
        "move_event": "google_calendar_write",
    },
    "github": {
        "list_repositories": "github_read_repo",
        "search_repositories": "github_read_repo",
        "get_repository": "github_read_repo",
        "get_repository_with_stats": "github_read_repo",
        "get_repository_languages": "github_read_repo",
        "get_repository_stars": "github_read_repo",
        "list_branches": "github_read_repo",
        "get_file_content": "github_read_repo",
        "get_directory_content": "github_read_repo",
        "get_branch_content": "github_read_repo",
        "search_code": "github_read_repo",
        "list_issues": "github_read_issues_prs",
        "get_issue": "github_read_issues_prs",
        "list_issue_comments": "github_read_issues_prs",
        "get_pull_requests": "github_read_issues_prs",
        "get_pull_request": "github_read_issues_prs",
        "get_pull_request_changes": "github_read_issues_prs",
        "get_pull_request_comments": "github_read_issues_prs",
        "get_pull_request_with_details": "github_read_issues_prs",
        "get_pull_request_count": "github_read_issues_prs",
        "search_issues_and_prs": "github_read_issues_prs",
        "create_issue": "github_write_issues",
        "edit_issue": "github_write_issues",
        "comment_on_issue": "github_write_issues",
        "close_issue": "github_write_issues",
        "reopen_issue": "github_write_issues",
        "assign_issue": "github_write_issues",
        "label_issue": "github_write_issues",
        "create_pull_request": "github_write_pr",
        "create_pull_request_comment": "github_write_pr",
        "edit_pull_request_comment": "github_write_pr",
        "create_review_request": "github_write_pr",
        "create_repository": "github_write_repo",
        "delete_repository": "github_write_repo",
        "create_branch": "github_write_repo",
        "set_default_branch": "github_write_repo",
        "create_file": "github_write_repo",
        "update_file": "github_write_repo",
        "delete_file": "github_write_repo",
    },
    "google_drive": {
        "google_drive_list_files": "google_drive_read",
        "google_drive_search_files": "google_drive_read",
        "google_drive_read_file": "google_drive_read",
        "google_drive_download_file": "google_drive_read",
        "google_drive_upload_file": "google_drive_write",
        "google_drive_update_file": "google_drive_write",
        "google_drive_create_folder": "google_drive_write",
        "google_drive_move_file": "google_drive_write",
        "google_drive_trash_file": "google_drive_write",
    },
    "google_docs": {
        "google_docs_get_document": "google_docs_read",
        "google_docs_create_document": "google_docs_write",
        "google_docs_insert_text": "google_docs_write",
        "google_docs_replace_text": "google_docs_write",
        "google_docs_batch_update": "google_docs_write",
    },
    "google_sheets": {
        "read_sheet": "google_sheets_read",
        "create_sheet": "google_sheets_write",
        "update_sheet": "google_sheets_write",
        "batch_update_sheet": "google_sheets_write",
        "create_duplicate_sheet": "google_sheets_write",
    },
    "google_tasks": {
        "google_tasks_list_task_lists": "google_tasks_read",
        "google_tasks_list_tasks": "google_tasks_read",
        "google_tasks_create_task": "google_tasks_write",
        "google_tasks_update_task": "google_tasks_write",
        "google_tasks_delete_task": "google_tasks_write",
    },
    "homeassistant": {
        "list_entities": "homeassistant_read",
        "get_entity_state": "homeassistant_read",
        "turn_on": "homeassistant_control",
        "turn_off": "homeassistant_control",
        "toggle": "homeassistant_control",
        "set_brightness": "homeassistant_control",
        "set_color": "homeassistant_control",
        "set_temperature": "homeassistant_control",
        "activate_scene": "homeassistant_control",
        "trigger_automation": "homeassistant_control",
        "call_service": "homeassistant_control",
    },
    "csv": {
        "list_csv_files": "csv_inspect",
        "get_columns": "csv_inspect",
        "read_csv_file": "csv_inspect",
    },
    "duckdb": {
        "show_tables": "duckdb_inspect",
        "describe_table": "duckdb_inspect",
        "summarize_table": "duckdb_inspect",
        "inspect_query": "duckdb_inspect",
        "full_text_search": "duckdb_inspect",
        "create_table_from_path": "duckdb_manage",
        "load_local_path_to_table": "duckdb_manage",
        "load_local_csv_to_table": "duckdb_manage",
        "load_s3_path_to_table": "duckdb_manage",
        "load_s3_csv_to_table": "duckdb_manage",
        "export_table_to_path": "duckdb_manage",
        "create_fts_index": "duckdb_manage",
        "get_table_name_from_path": "duckdb_manage",
    },
    "sql": {
        "list_tables": "sql_inspect",
        "describe_table": "sql_inspect",
    },
    "postgres": {
        "show_tables": "postgres_inspect",
        "describe_table": "postgres_inspect",
        "summarize_table": "postgres_inspect",
        "inspect_query": "postgres_inspect",
    },
    "redshift": {
        "show_tables": "redshift_inspect",
        "describe_table": "redshift_inspect",
        "summarize_table": "redshift_inspect",
        "inspect_query": "redshift_inspect",
    },
    "neo4j": {
        "list_labels": "neo4j_inspect",
        "list_relationship_types": "neo4j_inspect",
        "get_schema": "neo4j_inspect",
    },
    "yfinance": {
        "get_current_stock_price": "yfinance_get",
        "get_company_info": "yfinance_get",
        "get_stock_fundamentals": "yfinance_get",
        "get_income_statements": "yfinance_get",
        "get_key_financial_ratios": "yfinance_get",
        "get_analyst_recommendations": "yfinance_get",
        "get_company_news": "yfinance_get",
        "get_technical_indicators": "yfinance_get",
        "get_historical_stock_prices": "yfinance_get",
    },
    "openbb": {
        "get_stock_price": "openbb_get",
        "search_company_symbol": "openbb_get",
        "get_company_news": "openbb_get",
        "get_company_profile": "openbb_get",
        "get_price_targets": "openbb_get",
    },
    "financial_datasets_api": {
        "get_income_statements": "financial_datasets_financials",
        "get_balance_sheets": "financial_datasets_financials",
        "get_cash_flow_statements": "financial_datasets_financials",
        "get_segmented_financials": "financial_datasets_financials",
        "get_financial_metrics": "financial_datasets_financials",
        "get_company_info": "financial_datasets_financials",
        "get_earnings": "financial_datasets_financials",
        "get_insider_trades": "financial_datasets_financials",
        "get_institutional_ownership": "financial_datasets_financials",
        "get_sec_filings": "financial_datasets_financials",
        "get_stock_prices": "financial_datasets_market",
        "get_crypto_prices": "financial_datasets_market",
        "get_news": "financial_datasets_market",
        "search_tickers": "financial_datasets_market",
    },
    "openweather": {
        "get_current_weather": "openweather_get",
        "get_forecast": "openweather_get",
        "get_air_pollution": "openweather_get",
        "geocode_location": "openweather_get",
    },
    "docker": {
        "list_containers": "docker_inspect",
        "inspect_container": "docker_inspect",
        "get_container_logs": "docker_inspect",
        "list_images": "docker_inspect",
        "inspect_image": "docker_inspect",
        "list_volumes": "docker_inspect",
        "inspect_volume": "docker_inspect",
        "list_networks": "docker_inspect",
        "inspect_network": "docker_inspect",
        "run_container": "docker_container",
        "exec_in_container": "docker_container",
        "start_container": "docker_container",
        "stop_container": "docker_container",
        "remove_container": "docker_container",
        "pull_image": "docker_resource",
        "build_image": "docker_resource",
        "tag_image": "docker_resource",
        "remove_image": "docker_resource",
        "create_volume": "docker_resource",
        "remove_volume": "docker_resource",
        "create_network": "docker_resource",
        "remove_network": "docker_resource",
        "connect_container_to_network": "docker_resource",
        "disconnect_container_from_network": "docker_resource",
    },
    "e2b": {
        "run_python_code": "e2b_run",
        "run_command": "e2b_run",
        "stream_command": "e2b_run",
        "run_background_command": "e2b_run",
        "kill_background_command": "e2b_run",
        "run_server": "e2b_run",
        "list_files": "e2b_inspect",
        "read_file_content": "e2b_inspect",
        "watch_directory": "e2b_inspect",
        "get_sandbox_status": "e2b_inspect",
        "list_running_sandboxes": "e2b_inspect",
        "get_public_url": "e2b_inspect",
        "write_file_content": "e2b_files",
        "upload_file": "e2b_files",
        "download_file_from_sandbox": "e2b_files",
        "download_png_result": "e2b_files",
        "download_chart_data": "e2b_files",
        "set_sandbox_timeout": "e2b_lifecycle",
        "shutdown_sandbox": "e2b_lifecycle",
    },
    "daytona": {
        "run_code": "daytona_run",
        "run_shell_command": "daytona_run",
        "read_file": "daytona_inspect",
        "list_files": "daytona_inspect",
        "create_file": "daytona_files",
        "delete_file": "daytona_files",
        "change_directory": "daytona_files",
    },
    "exa": {
        "search_exa": "exa_lookup",
        "get_contents": "exa_lookup",
        "find_similar": "exa_lookup",
        "exa_answer": "exa_lookup",
    },
    "trafilatura": {
        "extract_text": "trafilatura_extract",
        "extract_metadata_only": "trafilatura_extract",
        "extract_batch": "trafilatura_extract",
    },
    "brightdata": {
        "scrape_as_markdown": "brightdata",
        "get_screenshot": "brightdata",
        "search_engine": "brightdata",
        "web_data_feed": "brightdata",
    },
    "oxylabs": {
        "search_google": "oxylabs",
        "get_amazon_product": "oxylabs",
        "search_amazon_products": "oxylabs",
        "scrape_website": "oxylabs",
    },
    "browserbase": {
        "navigate_to": "browserbase",
        "get_page_content": "browserbase",
        "close_session": "browserbase",
    },
    "spider": {
        "search_web": "spider",
        "scrape": "spider",
        "crawl": "spider",
    },
    "file_generation": {
        "generate_json_file": "generate_file",
        "generate_csv_file": "generate_file",
        "generate_pdf_file": "generate_file",
        "generate_docx_file": "generate_file",
        "generate_text_file": "generate_file",
        "generate_html_file": "generate_file",
        "generate_code_file": "generate_file",
    },
    "visualization": {
        "create_bar_chart": "create_chart",
        "create_line_chart": "create_chart",
        "create_pie_chart": "create_chart",
        "create_scatter_plot": "create_chart",
        "create_histogram": "create_chart",
    },
    "unsplash": {
        "search_photos": "unsplash",
        "get_photo": "unsplash",
        "get_random_photo": "unsplash",
        "download_photo": "unsplash",
    },
    "email": {"email_user": "smtp_send_email"},
    "twilio": {
        "send_sms": "twilio_send",
        "get_call_details": "twilio_read",
        "list_messages": "twilio_read",
    },
    "discord": {
        "get_channel_messages": "discord_read",
        "get_channel_info": "discord_read",
        "list_channels": "discord_read",
        "send_message": "discord_send",
        "delete_message": "discord_manage",
    },
    "telegram": {
        "get_chat": "telegram_read",
        "get_file": "telegram_read",
        "send_message": "telegram_send",
        "send_photo": "telegram_send",
        "send_document": "telegram_send",
        "send_video": "telegram_send",
        "send_audio": "telegram_send",
        "send_animation": "telegram_send",
        "send_sticker": "telegram_send",
        "edit_message": "telegram_manage",
        "delete_message": "telegram_manage",
        "react_with_emoji": "telegram_manage",
        "pin_message": "telegram_manage",
    },
    "whatsapp": {
        "send_text_message": "whatsapp_send",
        "send_template_message": "whatsapp_send",
        "send_reply_buttons": "whatsapp_send",
        "send_list_message": "whatsapp_send",
        "send_image": "whatsapp_send",
        "send_document": "whatsapp_send",
        "send_location": "whatsapp_send",
        "send_reaction": "whatsapp_send",
    },
    "webex": {"list_rooms": "webex_read", "send_message": "webex_send"},
    "zoom": {
        "get_meeting": "zoom_read",
        "get_upcoming_meetings": "zoom_read",
        "list_meetings": "zoom_read",
        "get_meeting_recordings": "zoom_read",
        "schedule_meeting": "zoom_manage",
        "delete_meeting": "zoom_manage",
    },
    "x": {
        "get_user_info": "x_read",
        "get_home_timeline": "x_read",
        "search_posts": "x_read",
        "create_post": "x_write",
        "reply_to_post": "x_write",
        "send_dm": "x_write",
    },
    "linear": {
        "get_user_details": "linear_read",
        "get_teams_details": "linear_read",
        "get_issue_details": "linear_read",
        "get_user_assigned_issues": "linear_read",
        "get_workflow_issues": "linear_read",
        "get_high_priority_issues": "linear_read",
        "create_issue": "linear_write",
        "update_issue": "linear_write",
    },
    "notion": {
        "search_pages": "notion_search",
        "create_page": "notion_write",
        "update_page": "notion_write",
    },
    "todoist": {
        "get_task": "todoist_read",
        "get_active_tasks": "todoist_read",
        "get_projects": "todoist_read",
        "create_task": "todoist_write",
        "update_task": "todoist_write",
        "close_task": "todoist_write",
        "delete_task": "todoist_write",
    },
    "clickup": {
        "list_spaces": "clickup_read",
        "list_lists": "clickup_read",
        "list_tasks": "clickup_read",
        "get_task": "clickup_read",
        "create_task": "clickup_write",
        "update_task": "clickup_write",
        "delete_task": "clickup_write",
    },
    "trello": {
        "list_boards": "trello_read",
        "get_board_lists": "trello_read",
        "get_cards": "trello_read",
        "create_board": "trello_write",
        "create_list": "trello_write",
        "create_card": "trello_write",
        "move_card": "trello_write",
    },
    "bitbucket": {
        "list_repositories": "bitbucket_read",
        "get_repository_details": "bitbucket_read",
        "list_repository_commits": "bitbucket_read",
        "list_all_pull_requests": "bitbucket_read",
        "get_pull_request_details": "bitbucket_read",
        "get_pull_request_changes": "bitbucket_read",
        "list_issues": "bitbucket_read",
        "create_repository": "bitbucket_create_repository",
    },
    "cal_com": {
        "get_available_slots": "cal_com_read",
        "get_upcoming_bookings": "cal_com_read",
        "create_booking": "cal_com_write",
        "reschedule_booking": "cal_com_write",
        "cancel_booking": "cal_com_write",
    },
    "shopify": {
        "get_shop_info": "shopify_store",
        "get_products": "shopify_store",
        "get_orders": "shopify_store",
        "get_inventory_levels": "shopify_store",
        "get_low_stock_products": "shopify_store",
        "get_top_selling_products": "shopify_analytics",
        "get_products_bought_together": "shopify_analytics",
        "get_sales_by_date_range": "shopify_analytics",
        "get_order_analytics": "shopify_analytics",
        "get_product_sales_breakdown": "shopify_analytics",
        "get_customer_order_history": "shopify_analytics",
        "get_sales_trends": "shopify_analytics",
        "get_average_order_value": "shopify_analytics",
        "get_repeat_customers": "shopify_analytics",
    },
    "spotify": {
        "search_tracks": "spotify_search",
        "search_playlists": "spotify_search",
        "search_artists": "spotify_search",
        "search_albums": "spotify_search",
        "get_track_recommendations": "spotify_search",
        "get_artist_top_tracks": "spotify_search",
        "get_album_tracks": "spotify_search",
        "get_current_user": "spotify_library",
        "get_currently_playing": "spotify_library",
        "get_user_playlists": "spotify_library",
        "get_playlist": "spotify_library",
        "get_my_top_tracks": "spotify_library",
        "get_my_top_artists": "spotify_library",
        "create_playlist": "spotify_playlist_write",
        "add_tracks_to_playlist": "spotify_playlist_write",
        "update_playlist_details": "spotify_playlist_write",
        "remove_tracks_from_playlist": "spotify_playlist_write",
        "play_track": "spotify_play",
    },
    "reddit": {
        "get_user_info": "reddit_read",
        "get_top_posts": "reddit_read",
        "get_subreddit_info": "reddit_read",
        "get_trending_subreddits": "reddit_read",
        "get_subreddit_stats": "reddit_read",
        "create_post": "reddit_write",
        "reply_to_post": "reddit_write",
        "reply_to_comment": "reddit_write",
    },
    "google_maps": {
        "search_places": "google_maps_places",
        "geocode_address": "google_maps_places",
        "reverse_geocode": "google_maps_places",
        "validate_address": "google_maps_places",
        "get_elevation": "google_maps_places",
        "get_timezone": "google_maps_places",
        "get_directions": "google_maps_routes",
        "get_distance_matrix": "google_maps_routes",
    },
    "brandfetch": {
        "search_by_identifier": "brandfetch_lookup",
        "asearch_by_identifier": "brandfetch_lookup",
        "search_by_brand": "brandfetch_lookup",
        "asearch_by_brand": "brandfetch_lookup",
    },
    "mem0": {
        "search_memory": "mem0_read",
        "get_all_memories": "mem0_read",
        "add_memory": "mem0_add",
        "delete_all_memories": "mem0_delete_all",
    },
    "microsoft_teams": {
        "microsoft_teams_list_teams": "microsoft_teams_read",
        "microsoft_teams_list_channels": "microsoft_teams_read",
        "microsoft_teams_list_channel_messages": "microsoft_teams_read",
        "microsoft_teams_list_channel_message_replies": "microsoft_teams_read",
        "microsoft_teams_list_chats": "microsoft_teams_read",
        "microsoft_teams_list_chat_messages": "microsoft_teams_read",
        "microsoft_teams_get_presence": "microsoft_teams_read",
        "microsoft_teams_send_channel_message": "microsoft_teams_send",
        "microsoft_teams_reply_to_channel_message": "microsoft_teams_send",
        "microsoft_teams_send_chat_message": "microsoft_teams_send",
        "microsoft_teams_start_chat": "microsoft_teams_send",
    },
    "microsoft_files": {
        "microsoft_files_search": "microsoft_files_read",
        "microsoft_files_list_folder": "microsoft_files_read",
        "microsoft_files_get_file": "microsoft_files_read",
        "microsoft_files_download_file": "microsoft_files_read",
        "microsoft_files_excel_list_worksheets": "microsoft_files_read",
        "microsoft_files_excel_read_range": "microsoft_files_read",
        "microsoft_files_replace_file": "microsoft_files_write",
        "microsoft_files_upload_file": "microsoft_files_write",
        "microsoft_files_excel_update_range": "microsoft_files_write",
    },
    "microsoft_sharepoint": {
        "microsoft_sharepoint_search_sites": "microsoft_sharepoint_read",
        "microsoft_sharepoint_get_site": "microsoft_sharepoint_read",
        "microsoft_sharepoint_list_lists": "microsoft_sharepoint_read",
        "microsoft_sharepoint_get_list": "microsoft_sharepoint_read",
        "microsoft_sharepoint_list_items": "microsoft_sharepoint_read",
        "microsoft_sharepoint_list_pages": "microsoft_sharepoint_read",
        "microsoft_sharepoint_get_page": "microsoft_sharepoint_read",
        "microsoft_sharepoint_create_item": "microsoft_sharepoint_write",
        "microsoft_sharepoint_update_item": "microsoft_sharepoint_write",
    },
    "microsoft_onenote": {
        "microsoft_onenote_list_notebooks": "microsoft_onenote_read",
        "microsoft_onenote_list_sections": "microsoft_onenote_read",
        "microsoft_onenote_list_pages": "microsoft_onenote_read",
        "microsoft_onenote_get_page": "microsoft_onenote_read",
        "microsoft_onenote_create_page": "microsoft_onenote_write",
        "microsoft_onenote_append_to_page": "microsoft_onenote_write",
    },
    "microsoft_todo": {
        "microsoft_todo_list_lists": "microsoft_todo_read",
        "microsoft_todo_list_tasks": "microsoft_todo_read",
        "microsoft_todo_create_task": "microsoft_todo_write",
        "microsoft_todo_update_task": "microsoft_todo_write",
        "microsoft_todo_delete_task": "microsoft_todo_write",
    },
    "microsoft_meetings": {
        "microsoft_meetings_find": "microsoft_meetings_read",
        "microsoft_meetings_list_transcripts": "microsoft_meetings_read",
        "microsoft_meetings_get_transcript": "microsoft_meetings_read",
    },
    "microsoft_people": {
        "microsoft_people_search": "microsoft_people_read",
        "microsoft_people_get_profile": "microsoft_people_read",
        "microsoft_people_list_direct_reports": "microsoft_people_read",
    },
    "outlook_mail": {
        "outlook_mail_search": "outlook_mail_read",
        "outlook_mail_get_message": "outlook_mail_read",
        "outlook_mail_download_attachment": "outlook_mail_read",
        "outlook_mail_get_automatic_replies": "outlook_mail_read",
        "outlook_mail_send": "outlook_mail_send_message",
        "outlook_mail_reply": "outlook_mail_send_message",
        "outlook_mail_mark_read": "outlook_mail_manage",
        "outlook_mail_move": "outlook_mail_manage",
        "outlook_mail_set_automatic_replies": "outlook_mail_manage",
    },
    "outlook_calendar": {
        "outlook_calendar_list_calendars": "outlook_calendar_read",
        "outlook_calendar_list_events": "outlook_calendar_read",
        "outlook_calendar_get_event": "outlook_calendar_read",
        "outlook_calendar_get_free_busy": "outlook_calendar_read",
        "outlook_calendar_create_event": "outlook_calendar_write",
        "outlook_calendar_update_event": "outlook_calendar_write",
        "outlook_calendar_delete_event": "outlook_calendar_write",
        "outlook_calendar_respond_to_event": "outlook_calendar_write",
    },
    "zep": {
        "get_zep_memory": "zep_read",
        "search_zep_memory": "zep_read",
        "add_zep_message": "zep_add_message",
    },
    "skills": {
        "get_skill_instructions": "skill",
        "get_skill_reference": "skill",
        "get_skill_script": "skill",
    },
    "attachments": {
        "list_attachments": "attachment",
        "get_attachment": "attachment",
        "register_attachment": "attachment",
        "inline_image": "attachment",
    },
    "atlassian": {
        "jira_search_issues": "jira_read",
        "jira_get_issue": "jira_read",
        "jira_create_issue": "jira_write",
        "jira_update_issue": "jira_write",
        "jira_add_comment": "jira_write",
        "jira_transition_issue": "jira_write",
        "confluence_search": "confluence_read",
        "confluence_get_page": "confluence_read",
        "confluence_list_attachments": "confluence_read",
        "confluence_download_attachment": "confluence_read",
        "confluence_create_page": "confluence_write",
        "confluence_update_page": "confluence_write",
        "confluence_add_comment": "confluence_write",
    },
    "slack": {
        "slack_inspect": "slack_read",
        "slack_search": "slack_read",
        "slack_recent": "slack_read",
        "slack_download": "slack_read",
        "slack_post": "slack_send",
        "slack_upload": "slack_send",
        "slack_react": "slack_manage",
        "slack_edit": "slack_manage",
        "slack_delete": "slack_manage",
    },
    # LEGACY_COMPAT: Retired individual functions of MindRoom-native identity, access, and graph toolkits.
    # Legacy format: tool_approval.rules[].match (exact, glob, or wildcard script) or include_tools/exclude_tools
    #   naming any of the 28 retired functions across shared_directories, contacts, knowledge_graph (including retired
    #   second-generation knowledge_graph_read and knowledge_graph_write), agent_manager, and email_invite (including
    #   the renamed single-verb reads shared_directories_read, contacts_read, and email_invite_read; agent_settings
    #   remains the kept zero-arg read).
    # Last legacy release: v2026.11.662 (second-generation knowledge_graph_read/knowledge_graph_write shipped in
    #   v2026.11.665-v2026.11.668: v2026.11.668); replacement: unreleased; consolidated into action-dispatched functions
    #   (unified `knowledge_graph` and read/write splits for the others) with ToolMetadata.action_scoped_grants.
    # Handling: Reject conflicting tool_approval.rules (including wildcard scripts without an explicit rule for the
    #   replacement) when the toolkit is enabled (including via personal_agents templates) and reject retired names
    #   in include_tools/exclude_tools at config load.
    # Coverage: tests/test_tool_approval_grants.py::test_identity_access_graph_action_scoped_grants_and_fail_closed_migration,
    #   tests/test_tool_approval_grants.py::test_template_only_personal_agents_fail_closed_for_retired_approval_rules,
    #   tests/test_tool_approval_grants.py::test_retired_identity_access_graph_include_exclude_overrides_fail_config_load.
    "shared_directories": {
        "list_shared_directories": "shared_directories_read",
        "create_shared_directory": "shared_directories_write",
        "invite_to_shared_directory": "shared_directories_write",
        "remove_from_shared_directory": "shared_directories_write",
        "delete_shared_directory": "shared_directories_write",
        "accept_shared_directory": "shared_directories_write",
        "decline_shared_directory": "shared_directories_write",
    },
    "contacts": {
        "list_contacts": "contacts_read",
        "request_contact": "contacts_write",
        "accept_contact": "contacts_write",
        "decline_contact": "contacts_write",
        "update_contact": "contacts_write",
        "remove_contact": "contacts_write",
    },
    "knowledge_graph": {
        "search_graph": "knowledge_graph",
        "graph_entity": "knowledge_graph",
        "delete_graph_fact": "knowledge_graph",
        "delete_graph_entity": "knowledge_graph",
        "merge_graph_entities": "knowledge_graph",
        "update_graph_entity_summary": "knowledge_graph",
        "knowledge_graph_read": "knowledge_graph",
        "knowledge_graph_write": "knowledge_graph",
    },
    "agent_manager": {
        "create_agent": "agent_manager_write",
        "update_agent": "agent_manager_write",
        "set_avatar": "agent_manager_write",
        "delete_agent": "agent_manager_write",
    },
    "email_invite": {
        "list_email_invites": "email_invite_read",
        "invite_by_email": "email_invite_write",
        "revoke_email_invite": "email_invite_write",
    },
    "script": {
        "start_script": "script_write",
        "cancel_script": "script_write",
        "get_script": "script_read",
        "list_scripts": "script_read",
        "get_script_resource_profiles": "script_read",
    },
    "terminal": {
        "terminal_open": "terminal_write",
        "terminal_run": "terminal_write",
        "terminal_send": "terminal_write",
        "terminal_list": "terminal_read",
    },
    "claude_agent": {
        "claude_session_status": "claude_agent_read",
        "claude_agent_status": "claude_agent_read",
        "claude_start_session": "claude_agent_write",
        "claude_send": "claude_agent_write",
        "claude_interrupt": "claude_agent_write",
        "claude_end_session": "claude_agent_write",
    },
    "thread_model": {
        "list_models": "thread_model",
        "get_thread_model": "thread_model",
        "switch_thread_model": "thread_model",
        "reset_thread_model": "thread_model",
        "thread_model_read": "thread_model",
        "thread_model_write": "thread_model",
    },
    "thread_snooze": {
        "snooze_thread": "thread_snooze",
        "unsnooze_thread": "thread_snooze",
    },
    "thread_resolution": {
        "resolve_thread": "thread_resolution",
        "reopen_thread": "thread_resolution",
    },
    "usage_stats": {
        "get_my_usage": "usage_stats",
        "get_my_private_usage": "usage_stats",
        "get_all_usage": "usage_stats",
    },
}

# LEGACY_COMPAT: Bare integration toolkit function names normalized to single-underscore `<toolkit>_<verb>`.
# Legacy format: tool_approval.rules[].match (exact, glob, or wildcard script) or include_tools/exclude_tools
#   naming bare function names of the 42 non-allowlisted integration toolkits (such as `read_url`, `run_query`,
#   `run_sql_query`, `send_email`, `generate_image`, `search_wikipedia`, etc.).
# Last legacy release: v2026.11.674; replacement: unreleased; every function is prefixed with `<toolkit>_` so the
#   built-in catalog has zero duplicate wire names and explicit toolkit scoping.
# Handling: Checked per enabled toolkit so overlapping retired names (like `run_query` or `read_url`) never fire
#   for unenabled toolkits, while naming every enabled replacement when multiple enabled toolkits share a retired
#   name; reject conflicting tool_approval.rules (including wildcard scripts without an explicit rule for the
#   replacement) and reject retired names in include_tools/exclude_tools with in-place replacement hints.
# Coverage:
#   tests/test_tool_approval_grants.py::test_normalized_integration_tool_names_fail_closed_and_stay_toolkit_scoped,
#   tests/test_tool_approval_grants.py::test_shared_retired_bare_name_reports_all_enabled_replacements.
_NORMALIZED_INTEGRATION_RENAMES: dict[str, dict[str, str]] = {
    "agentql": {
        "scrape_website": "agentql_scrape_website",
        "custom_scrape_website": "agentql_custom_scrape_website",
    },
    "airflow": {
        "save_dag_file": "airflow_save_dag_file",
        "read_dag_file": "airflow_read_dag_file",
    },
    "arxiv": {
        "search_arxiv_and_return_articles": "arxiv_search",
        "read_arxiv_papers": "arxiv_read_papers",
    },
    "aws_lambda": {
        "list_functions": "aws_lambda_list_functions",
        "invoke_function": "aws_lambda_invoke_function",
    },
    "aws_ses": {
        "send_email": "aws_ses_send_email",
    },
    "browserbase": {
        "screenshot": "browserbase_screenshot",
    },
    "cartesia": {
        "text_to_speech": "cartesia_text_to_speech",
        "list_voices": "cartesia_list_voices",
        "localize_voice": "cartesia_localize_voice",
    },
    "crawl4ai": {
        "crawl": "crawl4ai_crawl",
    },
    "csv": {
        "query_csv_file": "csv_query",
    },
    "custom_api": {
        "make_request": "custom_api_request",
    },
    "duckdb": {
        "run_query": "duckdb_run_query",
    },
    "eleven_labs": {
        "get_voices": "eleven_labs_get_voices",
        "generate_sound_effect": "eleven_labs_generate_sound_effect",
        "text_to_speech": "eleven_labs_text_to_speech",
    },
    "exa": {
        "research": "exa_research",
    },
    "fal": {
        "generate_media": "fal_generate_media",
        "image_to_image": "fal_image_to_image",
    },
    "firecrawl": {
        "scrape_website": "firecrawl_scrape_website",
        "crawl_website": "firecrawl_crawl_website",
        "map_website": "firecrawl_map_website",
        "search_web": "firecrawl_search_web",
    },
    "gemini": {
        "generate_image": "gemini_generate_image",
        "generate_video": "gemini_generate_video",
    },
    "giphy": {
        "search_gifs": "giphy_search_gifs",
    },
    "google_bigquery": {
        "run_sql_query": "google_bigquery_run_sql_query",
    },
    "google_scholar": {
        "search_google_scholar": "google_scholar_search",
    },
    "groq": {
        "transcribe_audio": "groq_transcribe_audio",
        "translate_audio": "groq_translate_audio",
        "generate_speech": "groq_generate_speech",
    },
    "jina": {
        "read_url": "jina_read_url",
        "search_query": "jina_search_query",
    },
    "linkup": {
        "web_search_with_linkup": "linkup_search",
    },
    "lumalabs": {
        "generate_video": "lumalabs_generate_video",
        "image_to_video": "lumalabs_image_to_video",
    },
    "modelslabs": {
        "generate_media": "modelslabs_generate_media",
    },
    "moviepy_video_tools": {
        "extract_audio": "moviepy_video_tools_extract_audio",
        "create_srt": "moviepy_video_tools_create_srt",
        "embed_captions": "moviepy_video_tools_embed_captions",
    },
    "neo4j": {
        "run_cypher_query": "neo4j_run_cypher_query",
    },
    "newspaper": {
        "read_article": "newspaper_read_article",
    },
    "openai": {
        "transcribe_audio": "openai_transcribe_audio",
        "generate_image": "openai_generate_image",
        "generate_speech": "openai_generate_speech",
    },
    "pandas": {
        "create_pandas_dataframe": "pandas_create_dataframe",
        "run_dataframe_operation": "pandas_run_operation",
    },
    "postgres": {
        "run_query": "postgres_run_query",
        "export_table_to_path": "postgres_export_table_to_path",
    },
    "pubmed": {
        "search_pubmed": "pubmed_search",
    },
    "redshift": {
        "run_query": "redshift_run_query",
        "export_table_to_path": "redshift_export_table_to_path",
    },
    "replicate": {
        "generate_media": "replicate_generate_media",
    },
    "resend": {
        "send_email": "resend_send_email",
    },
    "scrapegraph": {
        "smartscraper": "scrapegraph_smartscraper",
        "markdownify": "scrapegraph_markdownify",
        "searchscraper": "scrapegraph_searchscraper",
        "crawl": "scrapegraph_crawl",
        "scrape": "scrapegraph_scrape",
    },
    "serpapi": {
        "search_google": "serpapi_search_google",
        "search_youtube": "serpapi_search_youtube",
    },
    "sql": {
        "run_sql_query": "sql_run_query",
    },
    "tavily": {
        "web_search_using_tavily": "tavily_search",
        "web_search_with_tavily": "tavily_search_context",
        "extract_url_content": "tavily_extract",
    },
    "trafilatura": {
        "html_to_text": "trafilatura_html_to_text",
        "crawl_website": "trafilatura_crawl_website",
    },
    "website": {
        "read_url": "website_read_url",
    },
    "wikipedia": {
        "search_wikipedia": "wikipedia_search",
    },
    "zendesk": {
        "search_zendesk": "zendesk_search",
    },
}


def _merge_toolkit_renames(
    base: dict[str, dict[str, str]],
    extra: dict[str, dict[str, str]],
) -> dict[str, dict[str, str]]:
    merged = {toolkit: dict(renames) for toolkit, renames in base.items()}
    for toolkit, renames in extra.items():
        merged.setdefault(toolkit, {}).update(renames)
    return merged


RETIRED_CONSOLIDATED_FUNCTIONS: dict[str, dict[str, str]] = _merge_toolkit_renames(
    _CONSOLIDATED_TOOLKIT_RENAMES,
    _NORMALIZED_INTEGRATION_RENAMES,
)

# LEGACY_COMPAT: Mega-tools split 1-to-N into separate read and write/control functions.
# Legacy format: `triggers`, `desktop`, `workflow`, and `matrix_api` were previously single multi-action functions,
#   and `browser_control` previously served both read and control actions (read actions moved to `browser_read` while
#   `browser_control` remains active for control actions).
# Last legacy release: v2026.11.680; replacement: v2026.11.681; split into read vs. write/control functions.
# Handling: Reject `triggers`, `desktop`, `workflow`, and `matrix_api` in `include_tools`/`exclude_tools` (both authored
#   overrides and stored dashboard settings) and fail closed in `tool_approval.rules` unless rules explicitly cover every
#   replacement function; for `browser`, validate `browser_control` -> `browser_read` in `tool_approval.rules` only
#   (skipping non-script `browser_control` rules whose `arguments.action` cannot match any moved read action) while
#   keeping `browser_control` valid in `include_tools`/`exclude_tools`.
# Coverage:
#   tests/test_triggers_tool.py, tests/test_desktop_tool.py, tests/test_browser_tool.py,
#   tests/test_workflow_tool.py, tests/test_matrix_api_tool.py, tests/test_tool_approval_grants.py.
RETIRED_SPLIT_FUNCTIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "triggers": {"triggers": ("triggers_read", "triggers_write")},
    "desktop": {"desktop": ("desktop_read", "desktop_control")},
    "workflow": {"workflow": ("workflow_read", "workflow_write")},
    "matrix_api": {"matrix_api": ("matrix_api_read", "matrix_api_write")},
}

SPLIT_APPROVAL_ONLY_FUNCTIONS: dict[str, dict[str, dict[str, frozenset[str]]]] = {
    "browser": {
        "browser_control": {
            "browser_read": frozenset(
                {"actions", "console", "help", "profiles", "screenshot", "snapshot", "status", "tabs"},
            ),
        },
    },
}

STRICT_WILDCARD_SCRIPT_TOOLKITS: frozenset[str] = frozenset(
    {
        "skills",
        "attachments",
        "atlassian",
        "slack",
        "shared_directories",
        "contacts",
        "knowledge_graph",
        "agent_manager",
        "email_invite",
        "google_docs",
        "twilio",
        "calculator",
        "duckduckgo",
        "googlesearch",
        "serper",
        "youtube",
        "exa",
        "trafilatura",
        "brightdata",
        "oxylabs",
        "browserbase",
        "spider",
        "triggers",
        "desktop",
        "browser",
        "workflow",
        "matrix_api",
        "thread_tags",
        "outlook_mail",
        "outlook_calendar",
        "microsoft_teams",
        "microsoft_files",
        "microsoft_sharepoint",
        "microsoft_onenote",
        "microsoft_todo",
        "microsoft_meetings",
        "microsoft_people",
        "script",
        "terminal",
        "claude_agent",
        "thread_model",
        "thread_snooze",
        "thread_resolution",
        "hackernews",
        "google_bigquery",
        "usage_stats",
    },
)

STRICT_WILDCARD_SCRIPT_RENAMES: dict[str, frozenset[str]] = {
    toolkit: frozenset(renames) for toolkit, renames in _NORMALIZED_INTEGRATION_RENAMES.items()
}

SINGLE_VERB_RENAMED_TARGETS: frozenset[str] = frozenset(
    {
        "contacts_read",
        "email_invite_read",
        "shared_directories_read",
        "google_docs_read",
        "claude_agent_read",
        "twilio_send",
        *(new for renames in _NORMALIZED_INTEGRATION_RENAMES.values() for new in renames.values()),
    },
)
