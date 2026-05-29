#!/usr/bin/env python3
"""BFM contractor onboarding document helper."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


ACCOUNT = "val@buildfuture.me"
PROJECT_FOLDER_ID = "1hDZf-a3slrw6dYJuw3zEgxUOWaR0oKMj"
NDA_TEMPLATE_ID = "1xJbxgPIipHIPWPIy1EVLm1OAXrdVI4k4U8TPDhg-sac"
OFFER_TEMPLATE_ID = "1lMaKHWTykwNPqQbPUTDetgQUY3rInix2TRC_i87-OT4"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
]

NDA_PLACEHOLDERS = [
    "[Agreement Number]",
    "[Date]",
    "[Contractor Full Name]",
    "[Contractor Passport/ID]",
    "[Contractor Date of Birth]",
    "[Contractor Email]",
    "[Contractor Phone / Telegram / WhatsApp]",
]
OFFER_PLACEHOLDERS = [
    "[Candidate Full Name]",
    "[Passport/ID]",
    "[Date of Birth]",
    "[Candidate Email]",
    "[Candidate Phone]",
    "[Offer Date]",
]


def account_root(account: str = ACCOUNT) -> Path:
    slug = quote(account.strip().lower(), safe="@._+-")
    return Path.home() / ".hermes" / "google" / "accounts" / slug


def credentials(account: str = ACCOUNT) -> Credentials:
    token_path = account_root(account) / "google_token.json"
    if not token_path.exists():
        raise RuntimeError(f"Google token not found: {token_path}")
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        payload = json.loads(creds.to_json())
        payload.setdefault("type", "authorized_user")
        token_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    if not creds.valid:
        raise RuntimeError(f"Google token is invalid for {account}")
    return creds


def services(account: str = ACCOUNT) -> tuple[Any, Any]:
    creds = credentials(account)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    docs = build("docs", "v1", credentials=creds, cache_discovery=False)
    return drive, docs


def get_file(drive: Any, file_id: str) -> dict[str, Any]:
    return (
        drive.files()
        .get(
            fileId=file_id,
            fields=(
                "id,name,mimeType,parents,owners(emailAddress,displayName),"
                "webViewLink,trashed,modifiedTime"
            ),
            supportsAllDrives=True,
        )
        .execute()
    )


def document_text(docs: Any, doc_id: str) -> str:
    doc = (
        docs.documents()
        .get(documentId=doc_id, fields="body(content(paragraph(elements(textRun(content)))))")
        .execute()
    )
    chunks: list[str] = []
    for item in doc.get("body", {}).get("content", []):
        para = item.get("paragraph")
        if not para:
            continue
        chunks.extend(
            element.get("textRun", {}).get("content", "")
            for element in para.get("elements", [])
        )
    return "".join(chunks)


def missing_placeholders(docs: Any, doc_id: str, placeholders: list[str]) -> list[str]:
    text = document_text(docs, doc_id)
    return [placeholder for placeholder in placeholders if placeholder not in text]


def owner_emails(file_meta: dict[str, Any]) -> list[str]:
    return [owner.get("emailAddress", "") for owner in file_meta.get("owners", [])]


def verify_templates(_: argparse.Namespace) -> int:
    drive, docs = services()
    folder = get_file(drive, PROJECT_FOLDER_ID)
    nda = get_file(drive, NDA_TEMPLATE_ID)
    offer = get_file(drive, OFFER_TEMPLATE_ID)
    result = {
        "ok": True,
        "account": ACCOUNT,
        "folder": folder,
        "templates": {
            "nda": {
                "file": nda,
                "missing_placeholders": missing_placeholders(docs, NDA_TEMPLATE_ID, NDA_PLACEHOLDERS),
            },
            "offer": {
                "file": offer,
                "missing_placeholders": missing_placeholders(docs, OFFER_TEMPLATE_ID, OFFER_PLACEHOLDERS),
            },
        },
    }
    checks = [
        folder.get("mimeType") == FOLDER_MIME_TYPE,
        not folder.get("trashed"),
        PROJECT_FOLDER_ID in (nda.get("parents") or []),
        PROJECT_FOLDER_ID in (offer.get("parents") or []),
        ACCOUNT in owner_emails(nda),
        ACCOUNT in owner_emails(offer),
        not nda.get("trashed"),
        not offer.get("trashed"),
        not result["templates"]["nda"]["missing_placeholders"],
        not result["templates"]["offer"]["missing_placeholders"],
    ]
    result["ok"] = all(checks)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def drive_query_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def ensure_folder(drive: Any, *, name: str, parent_id: str) -> dict[str, Any]:
    query = " and ".join(
        [
            f"name = {drive_query_literal(name)}",
            f"mimeType = {drive_query_literal(FOLDER_MIME_TYPE)}",
            f"{drive_query_literal(parent_id)} in parents",
            "trashed = false",
        ]
    )
    existing = (
        drive.files()
        .list(
            q=query,
            spaces="drive",
            pageSize=10,
            fields="files(id,name,mimeType,parents,webViewLink)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
        )
        .execute()
        .get("files", [])
    )
    if existing:
        return existing[0]
    return (
        drive.files()
        .create(
            body={"name": name, "mimeType": FOLDER_MIME_TYPE, "parents": [parent_id]},
            fields="id,name,mimeType,parents,webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )


def copy_template(
    drive: Any,
    *,
    template_id: str,
    name: str,
    parent_id: str,
) -> dict[str, Any]:
    return (
        drive.files()
        .copy(
            fileId=template_id,
            body={"name": name, "parents": [parent_id]},
            fields="id,name,mimeType,parents,owners(emailAddress,displayName),webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )


def replace_all(docs: Any, *, doc_id: str, replacements: dict[str, str]) -> None:
    requests = [
        {
            "replaceAllText": {
                "containsText": {"text": old, "matchCase": True},
                "replaceText": new,
            }
        }
        for old, new in replacements.items()
    ]
    docs.documents().batchUpdate(documentId=doc_id, body={"requests": requests}).execute()


def agreement_number(today: dt.date) -> str:
    return today.strftime("%d%m%Y")


def agreement_date(today: dt.date) -> str:
    return today.strftime("%d/%m/%Y")


def russian_long_date(today: dt.date) -> str:
    months = {
        1: "января",
        2: "февраля",
        3: "марта",
        4: "апреля",
        5: "мая",
        6: "июня",
        7: "июля",
        8: "августа",
        9: "сентября",
        10: "октября",
        11: "ноября",
        12: "декабря",
    }
    return f"{today.day} {months[today.month]} {today.year}"


def clean_name(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value.strip())
    if not cleaned:
        raise RuntimeError("Full name is required")
    return cleaned


def create_candidate_docs(args: argparse.Namespace) -> int:
    drive, docs = services()
    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    full_name = clean_name(args.full_name)
    root_name = args.folder_name or f"06_Onboarding/{full_name} - {today.isoformat()}"
    if "/" in root_name:
        parent_id = PROJECT_FOLDER_ID
        folder = None
        for part in [p for p in root_name.split("/") if p.strip()]:
            folder = ensure_folder(drive, name=part.strip(), parent_id=parent_id)
            parent_id = folder["id"]
        assert folder is not None
        candidate_folder = folder
    else:
        candidate_folder = ensure_folder(drive, name=root_name, parent_id=PROJECT_FOLDER_ID)

    nda = copy_template(
        drive,
        template_id=NDA_TEMPLATE_ID,
        name=f"NDA - {full_name}",
        parent_id=candidate_folder["id"],
    )
    offer = copy_template(
        drive,
        template_id=OFFER_TEMPLATE_ID,
        name=f"Contractor Offer - {full_name}",
        parent_id=candidate_folder["id"],
    )
    replace_all(
        docs,
        doc_id=nda["id"],
        replacements={
            "[Agreement Number]": args.agreement_number or agreement_number(today),
            "[Date]": args.agreement_date or agreement_date(today),
            "[Contractor Full Name]": full_name,
            "[Contractor Passport/ID]": args.passport_id,
            "[Contractor Date of Birth]": args.date_of_birth,
            "[Contractor Email]": args.email,
            "[Contractor Phone / Telegram / WhatsApp]": args.phone,
        },
    )
    replace_all(
        docs,
        doc_id=offer["id"],
        replacements={
            "[Candidate Full Name]": full_name,
            "[Passport/ID]": args.passport_id,
            "[Date of Birth]": args.date_of_birth,
            "[Candidate Email]": args.email,
            "[Candidate Phone]": args.phone,
            "[Offer Date]": args.offer_date or russian_long_date(today),
        },
    )
    result = {
        "ok": True,
        "candidate_folder": candidate_folder,
        "nda": get_file(drive, nda["id"]),
        "offer": get_file(drive, offer["id"]),
        "manual_admin_checklist": [
            "Create corporate Google account in the BFM domain.",
            "Register Time Doctor using the corporate Google account.",
            "Prepare login handoff message to the candidate personal email.",
            "Send credentials only after explicit approval of the exact message.",
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="BFM onboarding document helper")
    sub = root.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify-templates", help="verify BFM folder and templates")
    verify.set_defaults(func=verify_templates)
    create = sub.add_parser("create-candidate-docs", help="copy and fill NDA and offer docs")
    create.add_argument("--full-name", required=True)
    create.add_argument("--passport-id", required=True)
    create.add_argument("--date-of-birth", required=True)
    create.add_argument("--email", required=True)
    create.add_argument("--phone", required=True)
    create.add_argument("--date", default="", help="ISO date for document dates; defaults to today")
    create.add_argument("--agreement-number", default="")
    create.add_argument("--agreement-date", default="")
    create.add_argument("--offer-date", default="")
    create.add_argument("--folder-name", default="")
    create.set_defaults(func=create_candidate_docs)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
