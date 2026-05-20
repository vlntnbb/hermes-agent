#!/usr/bin/env python3
"""Move Google Drive root files into a root-level folder.

This script reuses Hermes' Google Workspace account layout:

  ~/.hermes/google/accounts/<account>/google_client_secret.json
  ~/.hermes/google/accounts/<account>/google_token.json

First run is a dry run:

  python scripts/move_drive_root_documents.py --account niyaz@example.com

To authorize the account on first use, provide a Google OAuth Desktop client
secret JSON. The browser prompt must be completed with the requested account:

  python scripts/move_drive_root_documents.py \
    --account niyaz@example.com \
    --client-secret ~/Downloads/client_secret.json

To actually move the files:

  python scripts/move_drive_root_documents.py --account niyaz@example.com --execute
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote


FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
DEFAULT_DESTINATION_NAME = "Build Future Me"
FILE_FIELDS = "id, name, mimeType, parents, webViewLink"


@dataclass(frozen=True)
class AccountPaths:
    account: str
    root: Path
    token_path: Path
    client_secret_path: Path
    metadata_path: Path


def _account_paths(account: str) -> AccountPaths:
    slug = quote(account.strip().lower(), safe="@._+-")
    root = Path.home() / ".hermes" / "google" / "accounts" / slug
    return AccountPaths(
        account=account.strip().lower(),
        root=root,
        token_path=root / "google_token.json",
        client_secret_path=root / "google_client_secret.json",
        metadata_path=root / "account.json",
    )


def _write_account_metadata(paths: AccountPaths) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.metadata_path.write_text(json.dumps({"account": paths.account}, indent=2) + "\n")


def _drive_query_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _iter_files(service: Any, query: str) -> Iterator[dict[str, Any]]:
    page_token: str | None = None
    while True:
        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                pageSize=1000,
                pageToken=page_token,
                fields=f"nextPageToken, files({FILE_FIELDS})",
            )
            .execute()
        )
        yield from response.get("files", [])
        page_token = response.get("nextPageToken")
        if not page_token:
            return


def _copy_client_secret(client_secret: str, destination: Path) -> None:
    src = Path(client_secret).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Client secret file not found: {src}")

    try:
        payload = json.loads(src.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Client secret is not valid JSON: {src}") from exc

    if "installed" not in payload and "web" not in payload:
        raise ValueError("Client secret JSON must contain an 'installed' or 'web' section")

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, destination)
    try:
        destination.chmod(0o600)
    except OSError:
        pass


def _interactive_login(paths: AccountPaths, client_secret: str | None, force: bool) -> None:
    if client_secret:
        _copy_client_secret(client_secret, paths.client_secret_path)

    if paths.token_path.exists() and not force:
        return

    if not paths.client_secret_path.exists():
        raise RuntimeError(
            "Google token is missing and no OAuth client secret is configured.\n"
            "Pass --client-secret /path/to/client_secret.json to authorize this account."
        )

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing Google OAuth dependencies. Run via: uv run --extra google python ..."
        ) from exc

    flow = InstalledAppFlow.from_client_secrets_file(str(paths.client_secret_path), [DRIVE_SCOPE])
    creds = flow.run_local_server(port=0, prompt="consent")
    token_payload = json.loads(creds.to_json())
    token_payload.setdefault("type", "authorized_user")
    paths.token_path.parent.mkdir(parents=True, exist_ok=True)
    paths.token_path.write_text(json.dumps(token_payload, indent=2) + "\n")
    try:
        paths.token_path.chmod(0o600)
    except OSError:
        pass
    _write_account_metadata(paths)


def _build_drive_service(paths: AccountPaths):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Missing Google API dependencies. Run via: uv run --extra google python ..."
        ) from exc

    if not paths.token_path.exists():
        raise RuntimeError(f"Google token is missing: {paths.token_path}")

    creds = Credentials.from_authorized_user_file(str(paths.token_path), [DRIVE_SCOPE])
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_payload = json.loads(creds.to_json())
        token_payload.setdefault("type", "authorized_user")
        paths.token_path.write_text(json.dumps(token_payload, indent=2) + "\n")
        _write_account_metadata(paths)
    if not creds.valid:
        raise RuntimeError("Google token is invalid. Re-run with --relogin --client-secret ...")
    return build("drive", "v3", credentials=creds)


def _drive_user(service: Any) -> dict[str, str]:
    about = service.about().get(fields="user(emailAddress,displayName)").execute()
    user = about.get("user", {})
    return {
        "email": user.get("emailAddress", ""),
        "name": user.get("displayName", ""),
    }


def _root_id(service: Any) -> str:
    return service.files().get(fileId="root", fields="id").execute()["id"]


def _find_destination_folder(
    service: Any,
    *,
    root_id: str,
    destination_name: str,
) -> dict[str, Any]:
    query = " and ".join(
        [
            f"name = {_drive_query_literal(destination_name)}",
            f"mimeType = {_drive_query_literal(FOLDER_MIME_TYPE)}",
            f"{_drive_query_literal(root_id)} in parents",
            "trashed = false",
        ]
    )
    folders = list(_iter_files(service, query))
    if not folders:
        raise RuntimeError(
            f"Destination folder {destination_name!r} was not found in the Drive root."
        )
    if len(folders) > 1:
        matches = "\n".join(f"  - {f['name']} ({f['id']})" for f in folders)
        raise RuntimeError(
            f"Found multiple root folders named {destination_name!r}; pass a unique folder name.\n"
            f"{matches}"
        )
    return folders[0]


def _get_destination_folder_by_id(service: Any, destination_id: str) -> dict[str, Any]:
    folder = (
        service.files()
        .get(fileId=destination_id, fields=FILE_FIELDS)
        .execute()
    )
    if folder.get("mimeType") != FOLDER_MIME_TYPE:
        raise RuntimeError(f"Destination ID is not a folder: {destination_id}")
    return folder


def _list_root_items(
    service: Any,
    *,
    root_id: str,
    destination_id: str,
    include_folders: bool,
) -> list[dict[str, Any]]:
    query_parts = [
        f"{_drive_query_literal(root_id)} in parents",
        "trashed = false",
    ]
    if not include_folders:
        query_parts.append(f"mimeType != {_drive_query_literal(FOLDER_MIME_TYPE)}")

    items = list(_iter_files(service, " and ".join(query_parts)))
    return [item for item in items if item.get("id") != destination_id]


@dataclass
class MoveResult:
    item: dict[str, Any]
    ok: bool
    error: str = ""


def _move_item(
    service: Any,
    *,
    item: dict[str, Any],
    root_id: str,
    destination_id: str,
) -> MoveResult:
    parents = item.get("parents") or []
    if root_id in parents:
        remove_parents = root_id
    elif "root" in parents:
        remove_parents = "root"
    else:
        remove_parents = root_id

    try:
        service.files().update(
            fileId=item["id"],
            addParents=destination_id,
            removeParents=remove_parents,
            fields=FILE_FIELDS,
        ).execute()
    except Exception as exc:  # Google API errors carry enough detail in str(exc).
        return MoveResult(item=item, ok=False, error=str(exc))
    return MoveResult(item=item, ok=True)


def _print_plan(
    *,
    account: str,
    user: dict[str, str],
    destination: dict[str, Any],
    items: list[dict[str, Any]],
    execute: bool,
    include_folders: bool,
) -> None:
    mode = "EXECUTE" if execute else "DRY RUN"
    print(f"Mode: {mode}")
    print(f"Requested account: {account}")
    print(f"Authenticated user: {user.get('name') or '-'} <{user.get('email') or '-'}>")
    print(f"Destination: {destination['name']} ({destination['id']})")
    print(f"Folders included: {'yes' if include_folders else 'no'}")
    print(f"Root items to move: {len(items)}")
    for item in items:
        print(f"  - {item.get('name', item['id'])} ({item.get('mimeType', '-')})")
    if not execute:
        print("\nNo changes made. Re-run with --execute to move these items.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Move all non-folder items from the selected Google Drive root into "
            "the root-level 'Build Future Me' folder. Defaults to dry-run."
        )
    )
    parser.add_argument(
        "--account",
        required=True,
        help="Google account email to use for OAuth, for example niyaz@example.com.",
    )
    parser.add_argument(
        "--destination-name",
        default=DEFAULT_DESTINATION_NAME,
        help=f"Root folder name to move files into. Default: {DEFAULT_DESTINATION_NAME!r}.",
    )
    parser.add_argument(
        "--destination-id",
        default="",
        help="Destination folder ID. Overrides --destination-name and may point to a nested folder.",
    )
    parser.add_argument(
        "--client-secret",
        default="",
        help="OAuth Desktop client secret JSON path. Needed only for first login or --relogin.",
    )
    parser.add_argument(
        "--relogin",
        action="store_true",
        help="Force a new browser OAuth login for --account.",
    )
    parser.add_argument(
        "--include-folders",
        action="store_true",
        help="Also move root-level folders. The destination folder is always skipped.",
    )
    parser.add_argument(
        "--allow-account-mismatch",
        action="store_true",
        help="Do not abort if the OAuth token email differs from --account.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually move files. Without this flag, the script only prints a plan.",
    )
    parser.add_argument(
        "--quiet-empty",
        action="store_true",
        help="Print nothing and exit 0 when there are no root items to move.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    account = args.account.strip().lower()

    try:
        paths = _account_paths(account)
        _interactive_login(
            paths,
            client_secret=args.client_secret or None,
            force=args.relogin,
        )
        service = _build_drive_service(paths)
        user = _drive_user(service)
        if (
            user.get("email")
            and user["email"].lower() != account
            and not args.allow_account_mismatch
        ):
            raise RuntimeError(
                "Authenticated Google account does not match --account:\n"
                f"  --account: {account}\n"
                f"  OAuth user: {user['email']}\n"
                "Use --relogin --client-secret /path/to/client_secret.json to log in again."
            )

        root_id = _root_id(service)
        if args.destination_id:
            destination = _get_destination_folder_by_id(service, args.destination_id)
        else:
            destination = _find_destination_folder(
                service,
                root_id=root_id,
                destination_name=args.destination_name,
            )
        items = _list_root_items(
            service,
            root_id=root_id,
            destination_id=destination["id"],
            include_folders=args.include_folders,
        )
        if args.quiet_empty and not items:
            return 0
        _print_plan(
            account=account,
            user=user,
            destination=destination,
            items=items,
            execute=args.execute,
            include_folders=args.include_folders,
        )

        if not args.execute or not items:
            return 0

        results = [
            _move_item(
                service,
                item=item,
                root_id=root_id,
                destination_id=destination["id"],
            )
            for item in items
        ]
        failed = [result for result in results if not result.ok]
        moved = len(results) - len(failed)
        print(f"\nMoved: {moved}")
        if failed:
            print(f"Failed: {len(failed)}", file=sys.stderr)
            for result in failed:
                item = result.item
                print(
                    f"  - {item.get('name', item['id'])} ({item['id']}): {result.error}",
                    file=sys.stderr,
                )
            return 1
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
