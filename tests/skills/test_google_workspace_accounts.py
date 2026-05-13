"""Tests for account-scoped Google Workspace credential paths."""

import importlib.util
import json
import sys
from pathlib import Path


SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/scripts"
)
HELPER_PATH = SCRIPTS_DIR / "_google_accounts.py"


def _load_helper(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.syspath_prepend(str(SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location("google_accounts_test", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_legacy_paths_are_default_without_selected_account(monkeypatch, tmp_path):
    module = _load_helper(monkeypatch, tmp_path)
    hermes_home = tmp_path / ".hermes"

    paths = module.resolve_google_account_paths()

    assert paths.account is None
    assert paths.token_path == hermes_home / "google_token.json"
    assert paths.client_secret_path == hermes_home / "google_client_secret.json"


def test_named_account_paths_are_isolated(monkeypatch, tmp_path):
    module = _load_helper(monkeypatch, tmp_path)
    hermes_home = tmp_path / ".hermes"

    paths = module.resolve_google_account_paths("User+One@Example.COM")

    assert paths.account == "user+one@example.com"
    assert paths.token_path == (
        hermes_home / "google/accounts/user+one@example.com/google_token.json"
    )
    assert paths.client_secret_path == (
        hermes_home / "google/accounts/user+one@example.com/google_client_secret.json"
    )


def test_default_account_marker_selects_named_account(monkeypatch, tmp_path):
    module = _load_helper(monkeypatch, tmp_path)

    module.set_default_account("default@example.com")
    paths = module.resolve_google_account_paths()

    assert paths.account == "default@example.com"
    assert paths.token_path.name == "google_token.json"
    assert "default@example.com" in str(paths.token_path)


def test_migrate_legacy_account_copies_credentials(monkeypatch, tmp_path):
    module = _load_helper(monkeypatch, tmp_path)
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "google_token.json").write_text(json.dumps({"token": "t"}))
    (hermes_home / "google_client_secret.json").write_text(json.dumps({"installed": {}}))

    paths = module.migrate_legacy_account("user@example.com", make_default=True)

    assert paths.token_path.exists()
    assert paths.client_secret_path.exists()
    assert module.read_default_account() == "user@example.com"
    metadata = json.loads(paths.metadata_path.read_text())
    assert metadata["account"] == "user@example.com"
