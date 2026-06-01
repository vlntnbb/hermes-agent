---
name: sysadmin-onboarding
description: Create BFM Google and Time Doctor accounts.
version: 1.0.0
author: Valentin Vasilevsky + Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [bfm, onboarding, sysadmin, google-workspace, timedoctor, gmail]
    category: domain
    related_skills: [google-workspace, bfm-onboarding]
---

# Sysadmin Onboarding Skill

Use this skill after the BFM document onboarding step is ready. It provisions the operational accounts for a new BFM team member and sends the login handoff to the candidate's personal email; it does not prepare NDA or offer documents.

## When to Use

- Use when the user asks to create corporate Google Workspace access for a BFM employee or contractor.
- Use when the user asks to invite or create the same person in Time Doctor.
- Use when the user asks to send the finished login details to the candidate's personal email.
- Do not use for legal-document generation; use `bfm-onboarding` for NDA and offer documents.

## Prerequisites

- Google Workspace admin account is `val@buildfuture.me`.
- Google Workspace domain is `buildfuture.me`.
- Corporate email format is `name.surname@buildfuture.me`.
- Google OAuth for `val@buildfuture.me` must include Admin SDK Directory user scope and Gmail send scope.
- This skill uses the Google credential profile `sysadmin` by default, stored separately from cron profiles such as `funnel-sync`.
- If the existing Google token is missing Admin SDK scope, use this skill's `google-auth-url` and `google-auth-code` commands to re-authorize the `sysadmin` profile for the same account.
- Time Doctor admin credentials must be saved in Hermes environment variables: `TIMEDOCTOR_ACCESS_TOKEN` and `TIMEDOCTOR_REFRESH_TOKEN`, or legacy `TIMEDOCTOR_TOKEN`.
- Time Doctor company defaults to `TIMEDOCTOR_COMPANY_ID` or the first company visible to `val@buildfuture.me`.
- Drive folder grants are disabled by default; only pass Drive folder IDs when the user explicitly asks.
- Use the `terminal` tool from the Hermes repo root so the helper can load repo modules and the active Hermes profile.

## How to Run

Verify that the admin accounts and tokens work:

```bash
.venv/bin/python skills/domain/sysadmin-onboarding/scripts/sysadmin_onboarding.py verify-access
```

If Google reports missing scopes, create an OAuth URL and save the callback code:

```bash
.venv/bin/python skills/domain/sysadmin-onboarding/scripts/sysadmin_onboarding.py google-auth-url
.venv/bin/python skills/domain/sysadmin-onboarding/scripts/sysadmin_onboarding.py google-auth-code "<PASTED_CALLBACK_URL_OR_CODE>"
```

Preview the onboarding plan without creating accounts:

```bash
.venv/bin/python skills/domain/sysadmin-onboarding/scripts/sysadmin_onboarding.py plan \
  --full-name "MUHAMMET ALI TASHLIYEV" \
  --personal-email "tashliyev123@gmail.com" \
  --phone "+90 546 742 2517"
```

Create the accounts and send the login handoff:

```bash
.venv/bin/python skills/domain/sysadmin-onboarding/scripts/sysadmin_onboarding.py run \
  --full-name "MUHAMMET ALI TASHLIYEV" \
  --personal-email "tashliyev123@gmail.com" \
  --phone "+90 546 742 2517" \
  --execute
```

## Quick Reference

| Item | Value |
|---|---|
| Admin account | `val@buildfuture.me` |
| Workspace domain | `buildfuture.me` |
| Corporate email rule | first name + last name |
| Default Drive folders | none |
| Time Doctor role | `user` |
| Time Doctor email | corporate email |
| Handoff recipient | personal email |

## Procedure

1. Collect the minimum input:
   - legal full name;
   - personal email;
   - phone number when available;
   - explicit corporate email only if the default `name.surname@buildfuture.me` format is not correct.

2. Run `verify-access` before provisioning. If Google reports missing Directory scope, run `google-auth-url`, have the user authorize `val@buildfuture.me`, then run `google-auth-code` with the pasted callback URL or code.

3. Run `plan` and check the derived corporate email. For `MUHAMMET ALI TASHLIYEV`, the default email is `muhammet.tashliyev@buildfuture.me`.

4. Run `run --execute` only after the user confirms the plan. The helper:
   - creates the Google Workspace user with a generated temporary password;
   - creates or invites the Time Doctor user on the corporate email;
   - grants no Drive folders unless `--drive-folder-id` is passed;
   - sends the login handoff to the personal email unless `--no-send-email` is passed.

5. Report the JSON result back to the user without exposing generated passwords. If the email was not sent, provide the next safe step instead of printing secrets by default.

## Pitfalls

- Do not create project Drive access by default; BFM currently grants no folders during this step.
- Do not reuse personal email as the corporate account unless the user explicitly asks.
- Do not reset an existing Google user password silently. If an account already exists, stop and ask whether to reset it.
- Do not print generated passwords in chat. Use automatic email handoff or run with `--show-secrets` only when the user explicitly approves local secret output.
- Time Doctor may return `hireExists` when the user was already invited or hired; treat that as an account-state check, not as a reason to create a duplicate.
- If Time Doctor refresh fails, re-authenticate Time Doctor and update `TIMEDOCTOR_ACCESS_TOKEN` and `TIMEDOCTOR_REFRESH_TOKEN` in Hermes before running `--execute`.

## Verification

- `verify-access` returns `ok: true`.
- `plan` derives the expected corporate email.
- `run --execute` returns Google user status `created` or an explicit existing-account warning.
- Time Doctor returns a user ID, invitation status, or an explicit existing-user warning.
- Gmail send returns a message ID when automatic handoff is enabled.
