#!/bin/bash
# Quick dev run from repo root
cd "$(dirname "$0")"
exec /home/linuxbrew/.linuxbrew/bin/uv run uvicorn server.main:app --host 0.0.0.0 --port 3200 --reload
