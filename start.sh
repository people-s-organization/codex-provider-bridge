#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${ROOT_DIR}/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
INSTALL_PLAYWRIGHT_BROWSER="${INSTALL_PLAYWRIGHT_BROWSER:-0}"
UPGRADE_PIP="${UPGRADE_PIP:-0}"

cd "${ROOT_DIR}"

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "[*] Creating virtual environment at ${VENV_DIR}"
  "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

echo "[*] Installing Python dependencies"
if [[ "${UPGRADE_PIP}" == "1" ]]; then
  python -m pip install --upgrade pip
fi
python -m pip install -r requirements.txt

if [[ ! -f ".env" && -f ".env.example" ]]; then
  echo "[*] Creating .env from .env.example"
  cp .env.example .env
fi

if [[ "${INSTALL_PLAYWRIGHT_BROWSER}" == "1" ]]; then
  echo "[*] Installing Playwright Chromium browser"
  python -m playwright install chromium
fi

echo "[*] Starting Codex Provider Bridge"
exec python main.py "$@"
