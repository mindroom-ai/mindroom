"""Matrix operations module for mindroom."""

import os

from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Get homeserver from environment
MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "http://localhost:8008")
