"""Generate contextual topics for Matrix rooms."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.config import Config


def generate_room_topic(room_key: str, room_name: str, config: Config) -> str:
    """Generate a contextual topic for a room based on its purpose and configured agents.

    Args:
        room_key: The room key/alias (e.g., 'dev', 'analysis', 'lobby')
        room_name: Display name for the room
        config: Configuration with agent settings

    Returns:
        A contextual topic string for the room

    """
    # Get agents configured for this room
    agents_in_room = []
    for agent_name, agent_config in config.agents.items():
        if room_key in agent_config.rooms:
            display_name = agent_config.display_name or agent_name
            agents_in_room.append(display_name)

    # Room-specific topic templates based on common room types
    room_topics = {
        "lobby": [
            "🏠 Main hub for MindRoom agents • General discussions and coordination",
            "🎯 Central command • Where all agents meet and collaborate",
            "💬 Welcome to MindRoom • Your AI team headquarters",
            "🤝 Agent collaboration hub • Ask questions, get intelligent answers",
        ],
        "dev": [
            "💻 Development workspace • Code, build, and innovate with AI assistance",
            "🚀 Code collaboration • Where ideas become reality",
            "⚡ Development hub • Write, test, deploy with your AI team",
            "🛠️ Engineering room • Building the future, one commit at a time",
        ],
        "analysis": [
            "📊 Data insights center • Transform information into intelligence",
            "🔍 Analysis headquarters • Deep insights, clear recommendations",
            "📈 Strategic analysis • Where data meets decision-making",
            "🧠 Intelligence hub • Comprehensive analysis and insights",
        ],
        "science": [
            "🔬 Research laboratory • Explore, experiment, discover",
            "🌌 Scientific exploration • Where curiosity meets computation",
            "⚗️ Innovation center • Testing hypotheses with AI precision",
            "🔭 Discovery zone • Pushing the boundaries of knowledge",
        ],
        "finance": [
            "💰 Financial command center • Markets, metrics, and money management",
            "📉 Trading floor • Real-time insights and financial analysis",
            "💳 Finance hub • Your AI-powered financial advisors",
            "🏦 Investment insights • Strategic financial intelligence",
        ],
        "business": [
            "💼 Business strategy room • Growth, planning, and execution",
            "🎯 Strategic planning • Where business meets intelligence",
            "📋 Operations center • Streamline, optimize, succeed",
            "🚀 Growth hub • Business insights powered by AI",
        ],
        "communication": [
            "📞 Communication center • Calls, messages, and connections",
            "💬 Message hub • Stay connected with AI assistance",
            "📡 Communications room • Bridging conversations across platforms",
            "🌐 Connection point • Your AI-powered communication team",
        ],
        "automation": [
            "⚙️ Automation workshop • Streamline workflows with intelligent agents",
            "🤖 Process automation • Let AI handle the repetitive tasks",
            "🔄 Workflow optimization • Automate, integrate, accelerate",
            "⏰ Scheduling center • Your AI automation specialists",
        ],
        "personal": [
            "🏡 Personal assistant room • Your private AI team",
            "📝 Personal workspace • Tailored AI assistance just for you",
            "🎨 Creative studio • Where your ideas come to life",
            "💭 Thinking space • Personal productivity with AI support",
        ],
        "home": [
            "🏠 Smart home control • Your AI-powered home automation center",
            "🔌 Home assistant hub • Control, monitor, automate",
            "🌡️ Home automation • Intelligent living with AI",
            "💡 Connected home • Where comfort meets intelligence",
        ],
        "music": [
            "🎵 Music room • Discover, play, and explore with AI",
            "🎸 Sound studio • Your AI DJ and music companion",
            "🎶 Playlist central • Curated tunes powered by intelligence",
            "🎼 Music discovery • Let AI find your next favorite song",
        ],
        "news": [
            "📰 News briefing room • Stay informed with AI curation",
            "🌍 Information center • Breaking news and deep analysis",
            "📡 News hub • Real-time updates, intelligent summaries",
            "🗞️ Media room • Your AI news team at work",
        ],
        "shopping": [
            "🛍️ Shopping assistant • Smart recommendations and deal hunting",
            "🏪 Marketplace • AI-powered shopping intelligence",
            "💳 Shopping hub • Find, compare, save with AI",
            "📦 Purchase planning • Your intelligent shopping companion",
        ],
        "weather": [
            "☀️ Weather station • Forecasts and climate insights",
            "🌦️ Meteorology center • AI-powered weather intelligence",
            "⛈️ Climate hub • Real-time conditions and predictions",
            "🌡️ Weather room • Your AI meteorologist on duty",
        ],
    }

    # If we have a specific template for this room type, use it
    if room_key in room_topics:
        base_topics = room_topics[room_key]
    else:
        # Generic topics that work for any room
        base_topics = [
            f"🤖 {room_name} • Powered by MindRoom agents",
            f"💡 {room_name} • Intelligent collaboration space",
            f"🎯 {room_name} • Where AI agents work for you",
            f"✨ {room_name} • Your specialized AI team",
        ]

    # If we have agents, we can add agent-specific information
    if agents_in_room:
        # Take up to 3 agents to avoid overly long topics
        featured_agents = agents_in_room[:3]
        agent_list = ", ".join(featured_agents)
        if len(agents_in_room) > 3:
            agent_list += f" +{len(agents_in_room) - 3} more"

        # Add agent-aware topics
        agent_topics = [
            f"{base_topics[0].split('•')[0]}• Featuring: {agent_list}",
            f"🤝 Team: {agent_list} • Ready to assist in {room_name}",
        ]
        base_topics.extend(agent_topics)

    # Select a random topic from the available options
    return random.choice(base_topics)  # noqa: S311


def get_default_topic(room_name: str) -> str:
    """Get a simple default topic if topic generation fails.

    Args:
        room_name: Display name for the room

    Returns:
        A simple default topic string

    """
    return f"MindRoom {room_name}"
