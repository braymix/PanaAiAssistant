#!/bin/bash
# Argo — SessionStart hook: prepara un venv e installa le dipendenze cosi' che
# `pytest` e l'app girino nelle sessioni di Claude Code sul web.
set -euo pipefail

# Solo in ambiente remoto (Claude Code on the web). In locale non tocca nulla.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-.}"

# venv isolato: il pip di sistema qui va in conflitto coi pacchetti Debian.
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate

python -m pip install --quiet --upgrade pip
# idempotente e sfrutta la cache del container fra le sessioni
python -m pip install --quiet -r requirements.txt

# rendi il venv attivo per tutta la sessione (pytest/python -> venv)
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  {
    echo "export VIRTUAL_ENV=\"$CLAUDE_PROJECT_DIR/.venv\""
    echo "export PATH=\"$CLAUDE_PROJECT_DIR/.venv/bin:\$PATH\""
    echo "export PYTHONPATH=\"$CLAUDE_PROJECT_DIR\""
  } >> "$CLAUDE_ENV_FILE"
fi

echo "Argo session-start: venv pronto, dipendenze installate."
