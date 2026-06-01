#!/usr/bin/env python3
"""BFM sysadmin onboarding helper."""

from __future__ import annotations

import argparse
import base64
import email.utils
import json
import os
import re
import secrets
import string
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from importlib import util as importlib_util
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover - fallback for direct script reuse
    def get_hermes_home() -> Path:
        return Path.home() / ".hermes"


ADMIN_ACCOUNT = "val@buildfuture.me"
DOMAIN = "buildfuture.me"
TIME_DOCTOR_BASE_URL = "https://api2.timedoctor.com/api"
TIME_DOCTOR_LOGIN_URL = "https://web.timedoctor.com/"
REDIRECT_URI = "http://localhost:1"
GOOGLE_PENDING_AUTH_NAME = "sysadmin_google_oauth_pending.json"
GOOGLE_PROFILE = os.getenv("BFM_SYSADMIN_GOOGLE_PROFILE", "sysadmin")
GOOGLE_ADMIN_SCOPE = "https://www.googleapis.com/auth/admin.directory.user"
GOOGLE_GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GOOGLE_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
GOOGLE_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
GOOGLE_SCOPES = [GOOGLE_ADMIN_SCOPE, GOOGLE_GMAIL_SEND_SCOPE, GOOGLE_CLOUD_PLATFORM_SCOPE]
GOOGLE_SCOPES_WITH_DRIVE = [*GOOGLE_SCOPES, GOOGLE_DRIVE_SCOPE]
GOOGLE_AUTH_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    GOOGLE_ADMIN_SCOPE,
    GOOGLE_CLOUD_PLATFORM_SCOPE,
]
TIME_DOCTOR_ROLES = ("owner", "admin", "guest", "manager", "user")


class OnboardingError(RuntimeError):
    """Expected user-fixable onboarding failure."""


@dataclass(frozen=True)
class Candidate:
    full_name: str
    given_name: str
    family_name: str
    display_name: str
    personal_email: str
    phone: str
    corporate_email: str


def hermes_env_path() -> Path:
    return get_hermes_home() / ".env"


def load_hermes_env() -> None:
    path = hermes_env_path()
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = strip_env_value(value.strip())


def strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def account_root(account: str) -> Path:
    slug = urllib.parse.quote(account.strip().lower(), safe="@._+-")
    return get_hermes_home() / "google" / "accounts" / slug


def google_profile_root(account: str, profile: str | None = GOOGLE_PROFILE) -> Path:
    root = account_root(account)
    if profile:
        return root / "profiles" / urllib.parse.quote(profile.strip().lower(), safe="@._+-")
    return root


def google_token_path(account: str, profile: str | None = GOOGLE_PROFILE) -> Path:
    return google_profile_root(account, profile) / "google_token.json"


def google_client_secret_path(account: str) -> Path:
    return account_root(account) / "google_client_secret.json"


def google_pending_auth_path(account: str, profile: str | None = GOOGLE_PROFILE) -> Path:
    return google_profile_root(account, profile) / GOOGLE_PENDING_AUTH_NAME


def google_credentials(account: str, *, include_drive: bool = False, profile: str | None = GOOGLE_PROFILE) -> Credentials:
    token_path = google_token_path(account, profile)
    if not token_path.exists():
        raise OnboardingError(f"Google token not found: {token_path}")
    scopes = GOOGLE_SCOPES_WITH_DRIVE if include_drive else GOOGLE_SCOPES
    creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        payload = json.loads(creds.to_json())
        payload.setdefault("type", "authorized_user")
        token_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    if not creds.valid:
        raise OnboardingError(f"Google token is invalid for {account}")
    return creds


def google_service(
    api: str,
    version: str,
    account: str,
    *,
    include_drive: bool = False,
    profile: str | None = GOOGLE_PROFILE,
) -> Any:
    return build(
        api,
        version,
        credentials=google_credentials(account, include_drive=include_drive, profile=profile),
        cache_discovery=False,
    )


def stored_google_scopes(account: str, profile: str | None = GOOGLE_PROFILE) -> list[str]:
    token_path = google_token_path(account, profile)
    try:
        payload = json.loads(token_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw = payload.get("scopes") or payload.get("scope") or []
    if isinstance(raw, str):
        return sorted(scope for scope in raw.split() if scope)
    if isinstance(raw, list):
        return sorted(str(scope) for scope in raw if str(scope).strip())
    return []


def missing_google_auth_scopes(account: str, profile: str | None = GOOGLE_PROFILE) -> list[str]:
    granted = set(stored_google_scopes(account, profile))
    if not granted:
        return []
    return sorted(scope for scope in GOOGLE_AUTH_SCOPES if scope not in granted)


def extract_oauth_code_and_state(value: str) -> tuple[str, str | None]:
    text = value.strip()
    if text.startswith("http"):
        parsed = urllib.parse.urlparse(text)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" not in params:
            raise OnboardingError("No OAuth code found in callback URL.")
        return params["code"][0], (params.get("state") or [None])[0]
    return text, None


def google_auth_url(args: argparse.Namespace) -> int:
    client_secret = google_client_secret_path(args.google_admin)
    if not client_secret.exists():
        raise OnboardingError(
            f"Google client secret not found: {client_secret}. "
            "Store it with the google-workspace setup first."
        )
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:
        raise OnboardingError("Missing google-auth-oauthlib. Install the Google Workspace dependencies first.") from exc

    flow = Flow.from_client_secrets_file(
        str(client_secret),
        scopes=GOOGLE_AUTH_SCOPES,
        redirect_uri=REDIRECT_URI,
        autogenerate_code_verifier=True,
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    pending_path = google_pending_auth_path(args.google_admin, args.google_profile)
    pending_path.parent.mkdir(parents=True, exist_ok=True)
    pending_path.write_text(
        json.dumps(
            {
                "state": state,
                "code_verifier": flow.code_verifier,
                "redirect_uri": REDIRECT_URI,
                "scopes": GOOGLE_AUTH_SCOPES,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        pending_path.chmod(0o600)
    except OSError:
        pass
    print_json({
        "ok": True,
        "google_admin": args.google_admin,
        "google_profile": args.google_profile,
        "auth_url": auth_url,
        "pending_path": str(pending_path),
    })
    return 0


def google_auth_code(args: argparse.Namespace) -> int:
    client_secret = google_client_secret_path(args.google_admin)
    pending_path = google_pending_auth_path(args.google_admin, args.google_profile)
    if not client_secret.exists():
        raise OnboardingError(f"Google client secret not found: {client_secret}")
    if not pending_path.exists():
        raise OnboardingError("No pending sysadmin Google OAuth flow. Run google-auth-url first.")
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:
        raise OnboardingError("Missing google-auth-oauthlib. Install the Google Workspace dependencies first.") from exc

    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    code, returned_state = extract_oauth_code_and_state(args.code)
    if returned_state and returned_state != pending.get("state"):
        raise OnboardingError("OAuth state mismatch. Run google-auth-url again.")

    flow = Flow.from_client_secrets_file(
        str(client_secret),
        scopes=pending.get("scopes") or GOOGLE_AUTH_SCOPES,
        redirect_uri=pending.get("redirect_uri", REDIRECT_URI),
        state=pending["state"],
        code_verifier=pending["code_verifier"],
    )
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    flow.fetch_token(code=code)
    payload = json.loads(flow.credentials.to_json())
    payload.setdefault("type", "authorized_user")
    if getattr(flow.credentials, "granted_scopes", None):
        payload["scopes"] = list(flow.credentials.granted_scopes or [])

    token_path = google_token_path(args.google_admin, args.google_profile)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        token_path.chmod(0o600)
    except OSError:
        pass
    pending_path.unlink(missing_ok=True)

    missing = missing_google_auth_scopes(args.google_admin, args.google_profile)
    print_json(
        {
            "ok": not missing,
            "google_admin": args.google_admin,
            "google_profile": args.google_profile,
            "token_path": str(token_path),
            "missing_scopes": missing,
        }
    )
    return 0 if not missing else 1


def enable_admin_sdk_api(args: argparse.Namespace) -> int:
    creds = google_credentials(args.google_admin, profile=args.google_profile)
    service = build("serviceusage", "v1", credentials=creds, cache_discovery=False)
    name = f"projects/{args.project}/services/admin.googleapis.com"
    operation = service.services().enable(name=name, body={}).execute()
    print_json(
        {
            "ok": True,
            "google_admin": args.google_admin,
            "project": args.project,
            "service": "admin.googleapis.com",
            "operation": operation,
        }
    )
    return 0


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def title_words(value: str) -> str:
    return " ".join(part[:1].upper() + part[1:].lower() for part in normalize_space(value).split())


def ascii_slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", ".", ascii_text).strip(".")
    return re.sub(r"\.+", ".", slug)


def derive_candidate(args: argparse.Namespace) -> Candidate:
    full_name = normalize_space(args.full_name)
    if not full_name:
        raise OnboardingError("Full name is required.")
    personal_email = normalize_space(args.personal_email).lower()
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", personal_email):
        raise OnboardingError("A valid personal email is required.")

    parts = full_name.split()
    if len(parts) < 2 and not (args.given_name and args.family_name):
        raise OnboardingError("Pass --given-name and --family-name when full name has fewer than two words.")

    family_name = normalize_space(args.family_name) if args.family_name else parts[-1]
    given_name = normalize_space(args.given_name) if args.given_name else " ".join(parts[:-1])
    display_name = title_words(f"{given_name} {family_name}")

    if args.corporate_email:
        corporate_email = normalize_space(args.corporate_email).lower()
    else:
        first_token = args.given_name.split()[0] if args.given_name else parts[0]
        local = ".".join(filter(None, [ascii_slug(first_token), ascii_slug(family_name)]))
        if not local or "." not in local:
            raise OnboardingError("Could not derive corporate email. Pass --corporate-email explicitly.")
        corporate_email = f"{local}@{args.domain}"

    if not corporate_email.endswith(f"@{args.domain}"):
        raise OnboardingError(f"Corporate email must be under {args.domain}.")

    return Candidate(
        full_name=full_name,
        given_name=title_words(given_name),
        family_name=title_words(family_name),
        display_name=display_name,
        personal_email=personal_email,
        phone=normalize_space(args.phone or ""),
        corporate_email=corporate_email,
    )


def generate_password(length: int = 22) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"
    required = [string.ascii_lowercase, string.ascii_uppercase, string.digits, "!@#$%^&*()-_=+"]
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if all(any(char in group for char in password) for group in required):
            return password


def redact_secret(value: str | None) -> str:
    if not value:
        return ""
    return "[set]"


def http_error_payload(exc: HttpError) -> dict[str, Any]:
    try:
        payload = json.loads(exc.content.decode("utf-8", errors="replace"))
    except Exception:
        payload = {"message": exc.content.decode("utf-8", errors="replace")[:500]}
    return {"status": exc.resp.status, "reason": exc.resp.reason, "payload": payload}


def get_google_user(directory: Any, email: str) -> dict[str, Any] | None:
    try:
        return directory.users().get(userKey=email).execute()
    except HttpError as exc:
        if exc.resp.status == 404:
            return None
        raise


def create_google_user(directory: Any, candidate: Candidate, password: str, role: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "primaryEmail": candidate.corporate_email,
        "name": {"givenName": candidate.given_name, "familyName": candidate.family_name},
        "password": password,
        "changePasswordAtNextLogin": True,
        "recoveryEmail": candidate.personal_email,
        "includeInGlobalAddressList": True,
    }
    if candidate.phone:
        body["phones"] = [{"value": candidate.phone, "type": "work"}]
    if role:
        body["organizations"] = [{"title": role, "type": "work", "primary": True}]
    return directory.users().insert(body=body).execute()


def grant_drive_access(drive: Any, *, folder_id: str, email: str, role: str) -> dict[str, Any]:
    body = {"type": "user", "role": role, "emailAddress": email}
    return (
        drive.permissions()
        .create(
            fileId=folder_id,
            body=body,
            fields="id,type,role,emailAddress",
            sendNotificationEmail=False,
            supportsAllDrives=True,
        )
        .execute()
    )


def gmail_send(gmail: Any, *, sender: str, recipient: str, subject: str, body: str) -> dict[str, Any]:
    message = EmailMessage()
    message["To"] = recipient
    message["From"] = sender
    message["Subject"] = subject
    message["Date"] = email.utils.formatdate(localtime=True)
    message.set_content(body)
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    return gmail.users().messages().send(userId="me", body={"raw": encoded}).execute()


def load_timedoctor_client() -> Any:
    load_hermes_env()
    plugin_path = get_hermes_home() / "plugins" / "timedoctor" / "client.py"
    if not plugin_path.exists():
        raise OnboardingError(f"Time Doctor plugin client not found: {plugin_path}")
    spec = importlib_util.spec_from_file_location("hermes_timedoctor_client", plugin_path)
    if not spec or not spec.loader:
        raise OnboardingError(f"Could not load Time Doctor plugin client: {plugin_path}")
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TimeDoctorClient()


def timedoctor_request(
    client: Any,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v not in (None, "")})
    url = f"{client.base_url.rstrip('/')}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{query}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "Authorization": f"{client.auth_scheme} {client.token}",
        "Accept": "application/json",
        "User-Agent": "Hermes-BFM-Sysadmin-Onboarding/1.0",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            payload: Any = json.loads(raw)
        except ValueError:
            payload = raw[:500]
        raise OnboardingError(f"Time Doctor API error {exc.code} for {method.upper()} {path}: {payload}") from exc
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def exact_timedoctor_user(users: list[dict[str, Any]], email: str) -> dict[str, Any] | None:
    email_l = email.lower()
    for user in users:
        values = [
            user.get("email"),
            user.get("login"),
            user.get("userEmail"),
            user.get("user_email"),
        ]
        if any(str(value or "").lower() == email_l for value in values):
            return user
    return None


def timedoctor_invitation_exists(client: Any, *, company_id: str, email: str) -> dict[str, Any] | None:
    try:
        payload = client.request("GET", "/1.0/invitations/exists", params={"company": company_id, "email": email})
    except Exception:
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else None


def create_timedoctor_user(
    client: Any,
    *,
    company_id: str,
    candidate: Candidate,
    password: str,
    role: str,
    send_timedoctor_email: bool,
    project_ids: list[str],
    tag_ids: list[str],
) -> dict[str, Any]:
    matches = client.find_users(company_id=company_id, query=candidate.corporate_email, limit=1000)
    existing = exact_timedoctor_user(matches, candidate.corporate_email)
    if existing:
        return {"status": "exists", "user": summarize_timedoctor_user(existing)}

    invitation = timedoctor_invitation_exists(client, company_id=company_id, email=candidate.corporate_email)
    if invitation and invitation.get("invitePending"):
        return {"status": "invitation_exists", "invitation": invitation}

    body: dict[str, Any] = {
        "users": [
            {
                "name": candidate.display_name,
                "email": candidate.corporate_email,
                "password": password,
                "role": role,
            }
        ],
        "noSendEmail": "false" if send_timedoctor_email else "true",
    }
    if project_ids:
        body["onlyProjectIds"] = project_ids
    if tag_ids:
        body["tagIds"] = tag_ids

    payload = timedoctor_request(
        client,
        "POST",
        "/1.0/invitations/bulk",
        params={"company": company_id},
        body=body,
    )
    return {"status": "invited", "payload": payload}


def summarize_timedoctor_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user.get("id") or user.get("_id") or user.get("userId"),
        "name": user.get("name") or user.get("fullName") or user.get("displayName"),
        "email": user.get("email") or user.get("login") or user.get("userEmail"),
        "role": user.get("role"),
    }


def handoff_body(
    *,
    candidate: Candidate,
    google_password: str | None,
    timedoctor_password: str | None,
    google_created: bool,
    timedoctor_created: bool,
) -> str:
    lines = [
        f"Hi {candidate.display_name},",
        "",
        "Your BFM work accounts are ready.",
        "",
        "Google Workspace",
        f"Email: {candidate.corporate_email}",
    ]
    if google_created and google_password:
        lines.extend(
            [
                f"Temporary password: {google_password}",
                "Sign in: https://accounts.google.com/",
                "Google will ask you to change this password after the first login.",
            ]
        )
    else:
        lines.append("The Google account already exists; the password was not changed by this onboarding run.")

    lines.extend(["", "Time Doctor", f"Email: {candidate.corporate_email}"])
    if timedoctor_created and timedoctor_password:
        lines.extend([f"Temporary password: {timedoctor_password}", f"Sign in: {os.getenv('TIMEDOCTOR_LOGIN_URL', TIME_DOCTOR_LOGIN_URL)}"])
    else:
        lines.append("The Time Doctor account or invitation already exists; the password was not changed by this onboarding run.")

    lines.extend(["", "Please keep these credentials private."])
    return "\n".join(lines)


def common_result(args: argparse.Namespace, candidate: Candidate) -> dict[str, Any]:
    return {
        "candidate": {
            "full_name": candidate.full_name,
            "display_name": candidate.display_name,
            "personal_email": candidate.personal_email,
            "phone": candidate.phone,
            "corporate_email": candidate.corporate_email,
        },
        "defaults": {
            "domain": args.domain,
            "google_admin": args.google_admin,
            "google_profile": args.google_profile,
            "drive_folder_count": len(args.drive_folder_id or []),
            "timedoctor_role": args.timedoctor_role,
            "send_handoff_email": not args.no_send_email,
        },
    }


def verify_access(args: argparse.Namespace) -> int:
    result: dict[str, Any] = {
        "ok": True,
        "google": {
            "admin": args.google_admin,
            "profile": args.google_profile,
            "stored_missing_scopes": missing_google_auth_scopes(args.google_admin, args.google_profile),
        },
        "timedoctor": {},
    }
    if result["google"]["stored_missing_scopes"]:
        result["ok"] = False
    try:
        directory = google_service("admin", "directory_v1", args.google_admin, profile=args.google_profile)
        admin_user = directory.users().get(userKey=args.google_admin).execute()
        result["google"]["directory"] = {
            "ok": True,
            "admin_email": admin_user.get("primaryEmail"),
            "is_admin": admin_user.get("isAdmin"),
        }
    except Exception as exc:
        result["ok"] = False
        result["google"]["directory"] = error_dict(exc)

    try:
        gmail = google_service("gmail", "v1", args.google_admin, profile=args.google_profile)
        profile = gmail.users().getProfile(userId="me").execute()
        result["google"]["gmail"] = {"ok": True, "email": profile.get("emailAddress")}
    except Exception as exc:
        result["ok"] = False
        result["google"]["gmail"] = error_dict(exc)

    try:
        client = load_timedoctor_client()
        company_id = client.resolve_company_id()
        auth = client.authorization(company_id)
        result["timedoctor"] = {
            "ok": True,
            "company_id": company_id,
            "auth_keys": sorted(auth.keys()) if isinstance(auth, dict) else [],
        }
    except Exception as exc:
        result["ok"] = False
        result["timedoctor"] = error_dict(exc)

    print_json(result)
    return 0 if result["ok"] else 1


def plan(args: argparse.Namespace) -> int:
    candidate = derive_candidate(args)
    result = common_result(args, candidate)
    result["ok"] = True
    result["mode"] = "dry-run"
    result["planned_steps"] = [
        "create Google Workspace user",
        "create or invite Time Doctor user",
        "grant Drive folders" if args.drive_folder_id else "grant no Drive folders",
        "send login handoff email" if not args.no_send_email else "do not send login handoff email",
    ]
    result["secrets"] = {"google_password": "[generated at execution]", "timedoctor_password": "[generated at execution]"}
    print_json(result)
    return 0


def run(args: argparse.Namespace) -> int:
    candidate = derive_candidate(args)
    result = common_result(args, candidate)
    result["mode"] = "execute" if args.execute else "dry-run"
    if not args.execute:
        result["ok"] = True
        result["notice"] = "No accounts were created. Re-run with --execute to provision."
        print_json(result)
        return 0

    include_drive = bool(args.drive_folder_id)
    google_password = args.google_password or generate_password()
    timedoctor_password = args.timedoctor_password or generate_password()
    result["secrets"] = {
        "google_password": google_password if args.show_secrets else redact_secret(google_password),
        "timedoctor_password": timedoctor_password if args.show_secrets else redact_secret(timedoctor_password),
    }

    if args.no_send_email and not args.show_secrets and (not args.google_password or not args.timedoctor_password):
        raise OnboardingError("--no-send-email with generated passwords requires --show-secrets or explicit passwords.")

    client = load_timedoctor_client()
    company_id = args.timedoctor_company_id or client.resolve_company_id()
    client.authorization(company_id)

    directory = google_service(
        "admin",
        "directory_v1",
        args.google_admin,
        include_drive=include_drive,
        profile=args.google_profile,
    )
    existing_google = get_google_user(directory, candidate.corporate_email)
    google_created = False
    if existing_google:
        result["google_user"] = {
            "status": "exists",
            "primary_email": existing_google.get("primaryEmail"),
            "id": existing_google.get("id"),
        }
        if not args.allow_existing_google:
            result["ok"] = False
            result["error"] = "Google user already exists. Re-run with --allow-existing-google if this is expected."
            print_json(result)
            return 1
    else:
        created = create_google_user(directory, candidate, google_password, args.role)
        google_created = True
        result["google_user"] = {"status": "created", "primary_email": created.get("primaryEmail"), "id": created.get("id")}

    drive_results = []
    if args.drive_folder_id:
        drive = google_service(
            "drive",
            "v3",
            args.google_admin,
            include_drive=True,
            profile=args.google_profile,
        )
        for folder_id in args.drive_folder_id:
            permission = grant_drive_access(drive, folder_id=folder_id, email=candidate.corporate_email, role=args.drive_role)
            drive_results.append({"folder_id": folder_id, "permission": permission})
    result["drive"] = {"status": "granted" if drive_results else "none", "permissions": drive_results}

    td_result = create_timedoctor_user(
        client,
        company_id=company_id,
        candidate=candidate,
        password=timedoctor_password,
        role=args.timedoctor_role,
        send_timedoctor_email=args.timedoctor_send_email,
        project_ids=args.timedoctor_project_id or [],
        tag_ids=args.timedoctor_tag_id or [],
    )
    result["timedoctor"] = {"company_id": company_id, **td_result}
    timedoctor_created = td_result.get("status") == "invited"

    if not args.no_send_email:
        gmail = google_service(
            "gmail",
            "v1",
            args.google_admin,
            include_drive=include_drive,
            profile=args.google_profile,
        )
        sent = gmail_send(
            gmail,
            sender=args.google_admin,
            recipient=args.recipient_email or candidate.personal_email,
            subject=args.email_subject,
            body=handoff_body(
                candidate=candidate,
                google_password=google_password,
                timedoctor_password=timedoctor_password,
                google_created=google_created or bool(args.google_password),
                timedoctor_created=timedoctor_created or bool(args.timedoctor_password),
            ),
        )
        result["handoff_email"] = {"status": "sent", "message_id": sent.get("id"), "recipient": args.recipient_email or candidate.personal_email}
    else:
        result["handoff_email"] = {"status": "skipped"}

    result["ok"] = True
    print_json(result)
    return 0


def error_dict(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, HttpError):
        payload = http_error_payload(exc)
        return {"ok": False, "type": exc.__class__.__name__, **payload}
    return {"ok": False, "type": exc.__class__.__name__, "message": str(exc)}


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def add_candidate_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--full-name", required=True)
    parser.add_argument("--personal-email", required=True)
    parser.add_argument("--phone", default="")
    parser.add_argument("--given-name")
    parser.add_argument("--family-name")
    parser.add_argument("--corporate-email")
    parser.add_argument("--domain", default=DOMAIN)
    parser.add_argument("--google-admin", default=ADMIN_ACCOUNT)
    parser.add_argument("--google-profile", default=GOOGLE_PROFILE)
    parser.add_argument("--role", default="Project Manager")
    parser.add_argument("--drive-folder-id", action="append", default=[])
    parser.add_argument("--drive-role", choices=("reader", "commenter", "writer"), default="reader")
    parser.add_argument("--timedoctor-company-id")
    parser.add_argument("--timedoctor-role", choices=TIME_DOCTOR_ROLES, default="user")
    parser.add_argument("--timedoctor-project-id", action="append", default=[])
    parser.add_argument("--timedoctor-tag-id", action="append", default=[])
    parser.add_argument("--timedoctor-send-email", action="store_true")
    parser.add_argument("--recipient-email")
    parser.add_argument("--email-subject", default="BFM account access")
    parser.add_argument("--no-send-email", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Provision BFM Google Workspace and Time Doctor accounts.")
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify-access", help="Check Google admin, Gmail, and Time Doctor access.")
    verify.add_argument("--google-admin", default=ADMIN_ACCOUNT)
    verify.add_argument("--google-profile", default=GOOGLE_PROFILE)
    verify.set_defaults(func=verify_access)

    auth_url = sub.add_parser("google-auth-url", help="Create a Google OAuth URL with sysadmin scopes.")
    auth_url.add_argument("--google-admin", default=ADMIN_ACCOUNT)
    auth_url.add_argument("--google-profile", default=GOOGLE_PROFILE)
    auth_url.set_defaults(func=google_auth_url)

    auth_code = sub.add_parser("google-auth-code", help="Save Google OAuth callback code for sysadmin scopes.")
    auth_code.add_argument("code")
    auth_code.add_argument("--google-admin", default=ADMIN_ACCOUNT)
    auth_code.add_argument("--google-profile", default=GOOGLE_PROFILE)
    auth_code.set_defaults(func=google_auth_code)

    enable_api = sub.add_parser("enable-admin-sdk-api", help="Enable Admin SDK API in the OAuth Google Cloud project.")
    enable_api.add_argument("--google-admin", default=ADMIN_ACCOUNT)
    enable_api.add_argument("--google-profile", default=GOOGLE_PROFILE)
    enable_api.add_argument("--project", default="381203265311")
    enable_api.set_defaults(func=enable_admin_sdk_api)

    plan_parser = sub.add_parser("plan", help="Preview derived accounts and planned actions.")
    add_candidate_args(plan_parser)
    plan_parser.set_defaults(func=plan)

    run_parser = sub.add_parser("run", help="Provision accounts. Dry-run unless --execute is passed.")
    add_candidate_args(run_parser)
    run_parser.add_argument("--execute", action="store_true")
    run_parser.add_argument("--allow-existing-google", action="store_true")
    run_parser.add_argument("--google-password")
    run_parser.add_argument("--timedoctor-password")
    run_parser.add_argument("--show-secrets", action="store_true")
    run_parser.set_defaults(func=run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        load_hermes_env()
        return args.func(args)
    except OnboardingError as exc:
        print_json({"ok": False, "error": str(exc)})
        return 1
    except HttpError as exc:
        print_json({"ok": False, "google_error": http_error_payload(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
