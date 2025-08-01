"""Matrix operations module for mindroom."""

import os

from dotenv import load_dotenv

from .client import (
    create_room,
    fetch_thread_history,
    get_room_members,
    invite_to_room,
    join_room,
    login,
    matrix_client,
    prepare_response_content,
    register_user,
)
from .config import MatrixAccount, MatrixConfig, MatrixRoom
from .mentions import create_mention_content, create_mention_content_from_text, mention_agent
from .rooms import (
    add_room,
    get_room_aliases,
    get_room_id,
    load_rooms,
    remove_room,
)
from .users import (
    AgentMatrixUser,
    create_agent_user,
    ensure_all_agent_users,
    get_agent_credentials,
    login_agent_user,
    save_agent_credentials,
)

# Load environment variables
load_dotenv()

# Get homeserver from environment
MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "http://localhost:8008")

__all__ = [
    # Client functions
    "create_room",
    "fetch_thread_history",
    "get_room_members",
    "invite_to_room",
    "join_room",
    "login",
    "matrix_client",
    "prepare_response_content",
    "register_user",
    # Config models
    "MatrixAccount",
    "MatrixConfig",
    "MatrixRoom",
    # Room functions
    "add_room",
    "get_room_aliases",
    "get_room_id",
    "load_rooms",
    "remove_room",
    # Mention functions
    "create_mention_content",
    "create_mention_content_from_text",
    "mention_agent",
    # User functions
    "AgentMatrixUser",
    "create_agent_user",
    "ensure_all_agent_users",
    "get_agent_credentials",
    "login_agent_user",
    "save_agent_credentials",
    # Constants
    "MATRIX_HOMESERVER",
]
