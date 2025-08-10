#!/bin/bash

# Script to set up Gmail token symlink for MindRoom agents

echo "Setting up Gmail token for MindRoom agents..."

# Check if google_token.json exists
if [ -f "/home/basnijholt/Work/mindroom/google_token.json" ]; then
    echo "✓ Found google_token.json from widget OAuth"

    # Create symlink for Agno Gmail tools
    if [ ! -f "/home/basnijholt/Work/mindroom/token.json" ]; then
        ln -s /home/basnijholt/Work/mindroom/google_token.json /home/basnijholt/Work/mindroom/token.json
        echo "✓ Created symlink: token.json -> google_token.json"
    else
        echo "⚠ token.json already exists"
    fi

    echo ""
    echo "✅ Gmail setup complete! Your email_assistant agent can now access Gmail."
    echo ""
    echo "Try these commands in MindRoom:"
    echo "  @email_assistant show me my latest 5 emails"
    echo "  @email_assistant check for unread emails"
    echo "  @email_assistant search for emails about meetings"
else
    echo "❌ google_token.json not found!"
    echo ""
    echo "Please complete the OAuth flow first:"
    echo "1. Open the widget UI (http://localhost:3003)"
    echo "2. Go to the Gmail/Google integration section"
    echo "3. Click 'Connect Gmail' or 'Setup with Google'"
    echo "4. Authorize the application"
    echo "5. Run this script again"
fi
