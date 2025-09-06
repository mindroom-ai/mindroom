"""MindRoom configuration generator based on subscription tiers."""

import logging
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def generate_mindroom_config(
    tier: str,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate MindRoom config.yaml based on subscription tier.

    Args:
        tier: Subscription tier (free, starter, professional, enterprise)
        overrides: Optional configuration overrides

    Returns:
        Complete configuration dictionary

    """
    # Base configuration for all tiers
    base_config = {
        "models": {
            "available_models": {
                "haiku": {
                    "model_id": "claude-3-haiku-20240307",
                    "display_name": "Claude 3 Haiku",
                    "provider": "anthropic",
                },
            },
        },
        "memory": {
            "provider": "mem0",
            "config": {
                "vector_store": {
                    "provider": "chroma",
                    "config": {
                        "collection_name": "mindroom_memory",
                        "persist_directory": "/app/mindroom_data/memory",
                    },
                },
            },
        },
        "general_settings": {
            "response_timeout": 60,
            "max_message_length": 4000,
            "enable_analytics": True,
        },
    }

    # Tier-specific configurations
    if tier == "free":
        agents = _get_free_tier_agents()
        rooms = ["lobby"]
        tools = _get_free_tier_tools()

    elif tier == "starter":
        agents = _get_starter_tier_agents()
        rooms = ["lobby", "research", "development"]
        tools = _get_starter_tier_tools()
        base_config["models"]["available_models"]["haiku"]["rate_limit"] = 100

    elif tier == "professional":
        agents = _get_professional_tier_agents()
        rooms = ["lobby", "research", "development", "team", "automation", "analysis"]
        tools = _get_professional_tier_tools()

        # Add Sonnet model
        base_config["models"]["available_models"]["sonnet"] = {
            "model_id": "claude-3-5-sonnet-20241022",
            "display_name": "Claude 3.5 Sonnet",
            "provider": "anthropic",
            "rate_limit": 500,
        }

    else:  # enterprise
        agents = _get_enterprise_tier_agents()
        rooms = [
            "lobby",
            "research",
            "development",
            "team",
            "automation",
            "analysis",
            "executive",
            "security",
            "compliance",
        ]
        tools = _get_enterprise_tier_tools()

        # Add all models
        base_config["models"]["available_models"]["sonnet"] = {
            "model_id": "claude-3-5-sonnet-20241022",
            "display_name": "Claude 3.5 Sonnet",
            "provider": "anthropic",
        }
        base_config["models"]["available_models"]["opus"] = {
            "model_id": "claude-3-opus-20240229",
            "display_name": "Claude 3 Opus",
            "provider": "anthropic",
        }
        base_config["models"]["available_models"]["gpt4"] = {
            "model_id": "gpt-4-turbo-preview",
            "display_name": "GPT-4 Turbo",
            "provider": "openai",
        }

    # Build complete configuration
    config = {
        **base_config,
        "agents": agents,
        "rooms": {room: {"description": f"{room.capitalize()} room"} for room in rooms},
        "tools": tools,
        "tier": tier,
    }

    # Apply any overrides
    if overrides:
        config = _deep_merge(config, overrides)

    return config


def _get_free_tier_agents() -> dict[str, Any]:
    """Get agent configuration for free tier."""
    return {
        "assistant": {
            "display_name": "Assistant",
            "role": "General AI assistant for basic tasks",
            "model": "haiku",
            "tools": ["calculator", "file"],
            "instructions": [
                "Help users with general questions",
                "Perform basic calculations",
                "Manage simple file operations",
            ],
            "rooms": ["lobby"],
            "num_history_runs": 3,
        },
    }


def _get_starter_tier_agents() -> dict[str, Any]:
    """Get agent configuration for starter tier."""
    return {
        "assistant": {
            "display_name": "Assistant",
            "role": "General AI assistant",
            "model": "haiku",
            "tools": ["calculator", "file", "shell", "web_search"],
            "instructions": [
                "Help users with various tasks",
                "Search the web for information",
                "Execute basic shell commands safely",
                "Manage files and calculations",
            ],
            "rooms": ["lobby", "research", "development"],
            "num_history_runs": 5,
        },
        "researcher": {
            "display_name": "Researcher",
            "role": "Research and analysis specialist",
            "model": "haiku",
            "tools": ["web_search", "wikipedia", "arxiv"],
            "instructions": [
                "Conduct thorough research",
                "Analyze information from multiple sources",
                "Provide summaries and insights",
            ],
            "rooms": ["research"],
            "num_history_runs": 5,
        },
        "coder": {
            "display_name": "Coder",
            "role": "Programming assistant",
            "model": "haiku",
            "tools": ["file", "shell", "github"],
            "instructions": [
                "Write clean, documented code",
                "Help with debugging and testing",
                "Follow best practices",
            ],
            "rooms": ["development"],
            "num_history_runs": 5,
        },
    }


def _get_professional_tier_agents() -> dict[str, Any]:
    """Get agent configuration for professional tier."""
    agents = _get_starter_tier_agents()

    # Upgrade existing agents to Sonnet
    for agent in agents.values():
        agent["model"] = "sonnet"
        agent["num_history_runs"] = 10

    # Add professional agents
    agents.update(
        {
            "analyst": {
                "display_name": "AnalystAgent",
                "role": "Provide analytical insights and recommendations",
                "model": "sonnet",
                "tools": ["calculator", "web_search", "file"],
                "instructions": [
                    "Analyze information comprehensively",
                    "Provide structured insights",
                    "Make evidence-based recommendations",
                ],
                "rooms": ["analysis", "team"],
                "num_history_runs": 10,
            },
            "automator": {
                "display_name": "AutomatorAgent",
                "role": "Automation and workflow specialist",
                "model": "sonnet",
                "tools": ["shell", "file", "scheduler", "webhook"],
                "instructions": [
                    "Create automated workflows",
                    "Schedule tasks and jobs",
                    "Integrate with external services",
                ],
                "rooms": ["automation"],
                "num_history_runs": 10,
            },
            "writer": {
                "display_name": "WriterAgent",
                "role": "Content creation and documentation",
                "model": "sonnet",
                "tools": ["file", "web_search", "grammar"],
                "instructions": [
                    "Create high-quality content",
                    "Write documentation",
                    "Edit and proofread text",
                ],
                "rooms": ["team", "development"],
                "num_history_runs": 10,
            },
        },
    )

    return agents


def _get_enterprise_tier_agents() -> dict[str, Any]:
    """Get agent configuration for enterprise tier."""
    agents = _get_professional_tier_agents()

    # Add enterprise-specific agents
    agents.update(
        {
            "security": {
                "display_name": "SecurityAgent",
                "role": "Security analysis and compliance",
                "model": "sonnet",
                "tools": ["security_scan", "audit", "compliance"],
                "instructions": [
                    "Perform security audits",
                    "Check compliance requirements",
                    "Identify vulnerabilities",
                ],
                "rooms": ["security", "compliance"],
                "num_history_runs": 15,
            },
            "executive": {
                "display_name": "ExecutiveAgent",
                "role": "Executive insights and strategic planning",
                "model": "opus",
                "tools": ["analytics", "reports", "forecasting"],
                "instructions": [
                    "Provide executive summaries",
                    "Strategic planning assistance",
                    "Business intelligence insights",
                ],
                "rooms": ["executive"],
                "num_history_runs": 20,
            },
            "orchestrator": {
                "display_name": "OrchestratorAgent",
                "role": "Multi-agent coordination and complex workflows",
                "model": "opus",
                "tools": ["agent_manager", "workflow", "monitor"],
                "instructions": [
                    "Coordinate multiple agents",
                    "Manage complex workflows",
                    "Monitor and optimize performance",
                ],
                "rooms": ["*"],  # Access to all rooms
                "num_history_runs": 20,
            },
        },
    )

    return agents


def _get_free_tier_tools() -> dict[str, Any]:
    """Get tools configuration for free tier."""
    return {
        "calculator": {"enabled": True},
        "file": {"enabled": True, "max_size_mb": 5},
    }


def _get_starter_tier_tools() -> dict[str, Any]:
    """Get tools configuration for starter tier."""
    return {
        "calculator": {"enabled": True},
        "file": {"enabled": True, "max_size_mb": 25},
        "shell": {"enabled": True, "safe_mode": True},
        "web_search": {"enabled": True, "max_results": 5},
        "wikipedia": {"enabled": True},
        "arxiv": {"enabled": True},
        "github": {"enabled": True, "public_only": True},
    }


def _get_professional_tier_tools() -> dict[str, Any]:
    """Get tools configuration for professional tier."""
    tools = _get_starter_tier_tools()
    tools.update(
        {
            "file": {"enabled": True, "max_size_mb": 100},
            "shell": {"enabled": True, "safe_mode": False},
            "web_search": {"enabled": True, "max_results": 20},
            "github": {"enabled": True, "public_only": False},
            "scheduler": {"enabled": True},
            "webhook": {"enabled": True},
            "grammar": {"enabled": True},
        },
    )
    return tools


def _get_enterprise_tier_tools() -> dict[str, Any]:
    """Get tools configuration for enterprise tier."""
    tools = _get_professional_tier_tools()
    tools.update(
        {
            "file": {"enabled": True, "max_size_mb": 500},
            "security_scan": {"enabled": True},
            "audit": {"enabled": True},
            "compliance": {"enabled": True},
            "analytics": {"enabled": True},
            "reports": {"enabled": True},
            "forecasting": {"enabled": True},
            "agent_manager": {"enabled": True},
            "workflow": {"enabled": True},
            "monitor": {"enabled": True},
        },
    )
    return tools


def _deep_merge(dict1: dict, dict2: dict) -> dict:
    """Deep merge two dictionaries."""
    result = dict1.copy()
    for key, value in dict2.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def save_config_to_file(config: dict[str, Any], file_path: str) -> bool:
    """Save configuration to a YAML file.

    Args:
        config: Configuration dictionary
        file_path: Path to save the file

    Returns:
        True if successful, False otherwise

    """
    try:
        with open(file_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)
        logger.info(f"Saved config to {file_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to save config: {e}")
        return False
