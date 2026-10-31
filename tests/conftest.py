"""Shared test setup. Fakes for every port live in tests/fakes.py; tests build a Container from them."""

from app.config import Settings

# Tests must not depend on a developer's local .env (e.g. DEV_AUTH=true would break production-mode tests).
Settings.model_config["env_file"] = None
