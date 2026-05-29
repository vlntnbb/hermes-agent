# BFM Sysadmin Onboarding Defaults

These defaults are intentionally narrow. Change them only after the user gives a new BFM operating rule.

| Setting | Value |
|---|---|
| Google Workspace domain | `buildfuture.me` |
| Google Workspace admin | `val@buildfuture.me` |
| Corporate email format | `name.surname@buildfuture.me` |
| Default Drive folder grants | none |
| Time Doctor admin account | `val@buildfuture.me` |
| Time Doctor company | `TIMEDOCTOR_COMPANY_ID` or first authorized company |
| Time Doctor role | `user` |
| Handoff delivery | send automatically to personal email |

## Required Candidate Input

- Full legal name.
- Personal email.
- Phone number when available.

## Optional Overrides

- `--given-name` and `--family-name` when the legal-name split is ambiguous.
- `--corporate-email` when the default first-name plus last-name address is wrong.
- `--drive-folder-id` only when the user explicitly names folders to grant.
- `--timedoctor-company-id` when the saved Time Doctor admin token can see more than one company.
- `--timedoctor-role` when the user should be a manager, admin, guest, or owner instead of a regular user.

## Current Candidate Example

```bash
.venv/bin/python skills/domain/sysadmin-onboarding/scripts/sysadmin_onboarding.py plan \
  --full-name "MUHAMMET ALI TASHLIYEV" \
  --personal-email "tashliyev123@gmail.com" \
  --phone "+90 546 742 2517"
```

Expected corporate email: `muhammet.tashliyev@buildfuture.me`.
