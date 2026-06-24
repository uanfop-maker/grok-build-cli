#!/bin/bash

# Write GROK_AUTH_JSON to the expected location using Python for safe handling
python3 -c "
import os, sys
data = os.environ.get('GROK_AUTH_JSON', '')
if data:
    import pathlib
    pathlib.Path('/root/.grok').mkdir(parents=True, exist_ok=True)
    pathlib.Path('/root/.grok/auth.json').write_text(data)
    print('auth.json written', flush=True)
" 2>&1 || true

exec python3 /app/bot.py
