"""Matrix operations module for mindroom."""

import os

from dotenv import load_dotenv

from .client import (
    create_room,
    extract_thread_info,
    fetch_thread_history,
    get_room_members,
    invite_to_room,
    join_room,
    login,
    matrix_client,
    prepare_response_content,
    register_user,
)
from .identity import (
    MatrixID,
    ThreadStateKey,
    extract_agent_name,
    extract_server_name_from_homeserver,
    get_known_agents,
    is_agent_id,
    parse_matrix_id,
)
from .mentions import create_mention_content, create_mention_content_from_text
from .rooms import (
    add_room,
    get_room_aliases,
    get_room_id,
    load_rooms,
    remove_room,
)
from .state import MatrixAccount, MatrixRoom, MatrixState
from .users import (
    AgentMatrixUser,
    construct_agent_user_id,
    create_agent_user,
    ensure_all_agent_users,
    extract_domain_from_user_id,
    extract_username_from_user_id,
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
    "extract_thread_info",
    "fetch_thread_history",
    "get_room_members",
    "invite_to_room",
    "join_room",
    "login",
    "matrix_client",
    "prepare_response_content",
    "register_user",
    # Identity classes and functions
    "MatrixID",
    "ThreadStateKey",
    "extract_agent_name",
    "extract_server_name_from_homeserver",
    "get_known_agents",
    "is_agent_id",
    "parse_matrix_id",
    # Config models
    "MatrixAccount",
    "MatrixState",
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
    # User functions
    "AgentMatrixUser",
    "construct_agent_user_id",
    "create_agent_user",
    "ensure_all_agent_users",
    "extract_domain_from_user_id",
    "extract_username_from_user_id",
    "get_agent_credentials",
    "login_agent_user",
    "save_agent_credentials",
    # Constants
    "MATRIX_HOMESERVER",
]
