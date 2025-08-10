#!/bin/bash

# Quick setup script for ngrok with MindRoom

echo "Setting up ngrok for MindRoom OAuth..."

# Check if ngrok is installed
if ! command -v ngrok &> /dev/null; then
    echo "ngrok not found. Installing..."
    wget https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz
    tar xvzf ngrok-v3-stable-linux-amd64.tgz
    sudo mv ngrok /usr/local/bin/
    rm ngrok-v3-stable-linux-amd64.tgz
fi

# Start ngrok
echo "Starting ngrok on port 8765..."
echo "After ngrok starts:"
echo "1. Copy the HTTPS URL (e.g., https://abc123.ngrok.io)"
echo "2. Add this to Google OAuth redirect URIs: https://abc123.ngrok.io/api/gmail/callback"
echo "3. Update .env file with: GOOGLE_REDIRECT_URI=https://abc123.ngrok.io/api/gmail/callback"
echo ""
ngrok http 8765
