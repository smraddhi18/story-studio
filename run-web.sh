#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi
if [ -d /opt/homebrew/opt/expat/lib ]; then
  export DYLD_LIBRARY_PATH="/opt/homebrew/opt/expat/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
fi
PYTHON=.venv/bin/python
if [ ! -x "$PYTHON" ]; then PYTHON=python3; fi
"$PYTHON" manage.py migrate
exec "$PYTHON" manage.py runserver --noreload 127.0.0.1:8000
