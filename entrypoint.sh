#!/bin/bash
set -e

# Write GROK_AUTH_JSON to the expected location if provided
if [ -n "$GROK_AUTH_JSON" ]; then
  mkdir -p /root/.grok
  echo "$GROK_AUTH_JSON" > /root/.grok/auth.json
fi

exec python3 /app/bot.py
