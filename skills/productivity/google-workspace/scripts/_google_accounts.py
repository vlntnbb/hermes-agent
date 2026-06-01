"""Account-scoped Google Workspace credential paths.

The original google-workspace skill used profile-level files:
``google_token.json`` and ``google_client_secret.json``.  Those paths remain
the legacy/default fallback.  Named accounts live under
``google/accounts/<account>/`` so multiple Google OAuth identities can coexist
in one Hermes profile.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote

from _hermes_home import display_hermes_home, get_hermes_home


LEGACY_TOKEN_NAME = "google_token.json"
LEGACY_CLIENT_SECRET_NAME = "google_client_secret.json"
LEGACY_PENDING_AUTH_NAME = "google_oauth_pending.json"
DEFAULT_ACCOUNT_NAME = "default_account"
ACCOUNT_METADATA_NAME = "account.json"
PROFILES_DIR_NAME = "profiles"


@dataclass(frozen=True)
class GoogleAccountPaths:
    account: str | None
    profile: str | None
    root: Path
    token_path: Path
    client_secret_path: Path
    pending_auth_path: Path
    metadata_path: Path | None = None
    account_root: Path | None = None

    @property
    def label(self) -> str:
        base = self.account or "legacy"
        return f"{base}:{self.profile}" if self.profile else base


def _home(home: Path | None = None) -> Path:
    return home or get_hermes_home()


def accounts_root(home: Path | None = None) -> Path:
    return _home(home) / "google" / "accounts"


def default_account_path(home: Path | None = None) -> Path:
    return _home(home) / "google" / DEFAULT_ACCOUNT_NAME


def normalize_account(account: str | None) -> str | None:
    if account is None:
        return None
    value = account.strip().lower()
    return value or None


def normalize_profile(profile: str | None) -> str | None:
    if profile is None:
        return None
    value = profile.strip().lower()
    return value or None


def account_slug(account: str) -> str:
    # Keep email-like names readable while making path separators impossible.
    return quote(account, safe="@._+-")


def profile_slug(profile: str) -> str:
    return quote(profile, safe="@._+-")


def account_from_slug(slug: str) -> str:
    return unquote(slug)


def read_default_account(home: Path | None = None) -> str | None:
    path = default_account_path(home)
    try:
        return normalize_account(path.read_text().strip())
    except FileNotFoundError:
        return None


def resolve_google_account_paths(
    account: str | None = None,
    profile: str | None = None,
    *,
    home: Path | None = None,
) -> GoogleAccountPaths:
    hermes_home = _home(home)
    selected_profile = (
        normalize_profile(profile)
        or normalize_profile(os.getenv("HERMES_GOOGLE_PROFILE"))
    )
    selected = (
        normalize_account(account)
        or normalize_account(os.getenv("HERMES_GOOGLE_ACCOUNT"))
        or read_default_account(hermes_home)
    )
    if not selected:
        if selected_profile:
            root = hermes_home / "google" / PROFILES_DIR_NAME / profile_slug(selected_profile)
            return GoogleAccountPaths(
                account=None,
                profile=selected_profile,
                root=root,
                token_path=root / LEGACY_TOKEN_NAME,
                client_secret_path=hermes_home / LEGACY_CLIENT_SECRET_NAME,
                pending_auth_path=root / LEGACY_PENDING_AUTH_NAME,
                metadata_path=root / ACCOUNT_METADATA_NAME,
                account_root=None,
            )
        return GoogleAccountPaths(
            account=None,
            profile=None,
            root=hermes_home,
            token_path=hermes_home / LEGACY_TOKEN_NAME,
            client_secret_path=hermes_home / LEGACY_CLIENT_SECRET_NAME,
            pending_auth_path=hermes_home / LEGACY_PENDING_AUTH_NAME,
            metadata_path=None,
            account_root=None,
        )

    account_root = accounts_root(hermes_home) / account_slug(selected)
    root = (
        account_root / PROFILES_DIR_NAME / profile_slug(selected_profile)
        if selected_profile
        else account_root
    )
    return GoogleAccountPaths(
        account=selected,
        profile=selected_profile,
        root=root,
        token_path=root / LEGACY_TOKEN_NAME,
        # Profiles intentionally share the OAuth client secret with the
        # account.  Tokens and pending PKCE sessions are profile-isolated.
        client_secret_path=account_root / LEGACY_CLIENT_SECRET_NAME,
        pending_auth_path=root / LEGACY_PENDING_AUTH_NAME,
        metadata_path=root / ACCOUNT_METADATA_NAME,
        account_root=account_root,
    )


def ensure_account_dir(paths: GoogleAccountPaths) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)


def write_account_metadata(paths: GoogleAccountPaths) -> None:
    if not paths.metadata_path:
        return
    ensure_account_dir(paths)
    paths.metadata_path.write_text(
        json.dumps(
            {
                "account": paths.account,
                **({"profile": paths.profile} if paths.profile else {}),
            },
            indent=2,
        )
        + "\n"
    )
    if paths.account_root and paths.account:
        paths.account_root.mkdir(parents=True, exist_ok=True)
        root_metadata = paths.account_root / ACCOUNT_METADATA_NAME
        if not root_metadata.exists():
            root_metadata.write_text(json.dumps({"account": paths.account}, indent=2) + "\n")


def set_default_account(account: str, *, home: Path | None = None) -> GoogleAccountPaths:
    paths = resolve_google_account_paths(account, home=home)
    ensure_account_dir(paths)
    write_account_metadata(paths)
    marker = default_account_path(home)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{paths.account}\n")
    return paths


def _chmod_secret(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def migrate_legacy_account(
    account: str,
    profile: str | None = None,
    *,
    home: Path | None = None,
    overwrite: bool = False,
    make_default: bool = False,
) -> GoogleAccountPaths:
    hermes_home = _home(home)
    paths = resolve_google_account_paths(account, profile=profile, home=hermes_home)
    ensure_account_dir(paths)
    copied = False
    for src, dst in (
        (hermes_home / LEGACY_TOKEN_NAME, paths.token_path),
        (hermes_home / LEGACY_CLIENT_SECRET_NAME, paths.client_secret_path),
        (hermes_home / LEGACY_PENDING_AUTH_NAME, paths.pending_auth_path),
    ):
        if not src.exists():
            continue
        if dst.exists() and not overwrite:
            continue
        shutil.copy2(src, dst)
        _chmod_secret(dst)
        copied = True
    write_account_metadata(paths)
    if make_default:
        set_default_account(paths.account or account, home=hermes_home)
    if not copied and not paths.token_path.exists() and not paths.client_secret_path.exists():
        raise FileNotFoundError(
            f"No legacy Google credentials found under {display_hermes_home()}."
        )
    return paths


def list_google_accounts(home: Path | None = None) -> list[dict[str, object]]:
    hermes_home = _home(home)
    default = read_default_account(hermes_home)
    accounts: list[dict[str, object]] = []
    legacy_token = hermes_home / LEGACY_TOKEN_NAME
    legacy_client = hermes_home / LEGACY_CLIENT_SECRET_NAME
    if legacy_token.exists() or legacy_client.exists():
        accounts.append(
            {
                "account": "legacy",
                "profile": None,
                "default": default is None,
                "token": legacy_token.exists(),
                "client_secret": legacy_client.exists(),
                "path": str(hermes_home),
            }
        )

    root = accounts_root(hermes_home)
    if root.exists():
        for child in sorted(p for p in root.iterdir() if p.is_dir()):
            metadata_path = child / ACCOUNT_METADATA_NAME
            account = account_from_slug(child.name)
            try:
                metadata = json.loads(metadata_path.read_text())
                account = normalize_account(metadata.get("account")) or account
            except Exception:
                pass
            accounts.append(
                {
                    "account": account,
                    "profile": None,
                    "default": account == default,
                    "token": (child / LEGACY_TOKEN_NAME).exists(),
                    "client_secret": (child / LEGACY_CLIENT_SECRET_NAME).exists(),
                    "path": str(child),
                }
            )
            profiles_root = child / PROFILES_DIR_NAME
            if profiles_root.exists():
                for profile_dir in sorted(p for p in profiles_root.iterdir() if p.is_dir()):
                    profile = profile_dir.name
                    profile_metadata_path = profile_dir / ACCOUNT_METADATA_NAME
                    try:
                        profile_metadata = json.loads(profile_metadata_path.read_text())
                        profile = normalize_profile(profile_metadata.get("profile")) or profile
                    except Exception:
                        pass
                    accounts.append(
                        {
                            "account": account,
                            "profile": profile,
                            "default": False,
                            "token": (profile_dir / LEGACY_TOKEN_NAME).exists(),
                            "client_secret": (child / LEGACY_CLIENT_SECRET_NAME).exists(),
                            "path": str(profile_dir),
                        }
                    )
    return accounts
