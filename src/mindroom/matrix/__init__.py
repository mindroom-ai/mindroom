"""Matrix operations module for mindroom."""

import os

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Get homeserver from environment
MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "http://localhost:8008")
# Get server name from environment (for federation setups where hostname != server_name)
MATRIX_SERVER_NAME = os.getenv("MATRIX_SERVER_NAME", None)
