---
name: bfm-onboarding
description: Onboard BFM contractors with Drive templates.
version: 1.0.0
author: Valentin Vasilevsky + Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [bfm, onboarding, google-drive, docs, nda, contractor-offer]
    category: domain
    related_skills: [google-workspace, ocr-and-documents]
---

# BFM Onboarding Skill

Use this skill to onboard a new contractor for the BuildFuture.Me / BFM project from Google Drive templates. It prepares NDA and contractor-offer copies, fills candidate data, and leaves admin-account tasks as explicit checklist items; it does not guess legal terms or silently create accounts.

## When to Use

- Use when the user asks to onboard a new BFM contractor, employee, project manager, video editor, монтажер, or similar project role.
- Use when the user asks to prepare BFM NDA or contractor-offer documents from the shared Drive templates.
- Use when the user asks to verify or repair the BFM onboarding template setup in Google Drive.
- Do not use for non-BFM hiring flows unless the user explicitly says to adapt this workflow.

## Prerequisites

- Google OAuth named account `val@buildfuture.me` must exist in Hermes Google Workspace layout.
- Use the `google-workspace` skill if OAuth setup or account switching is needed.
- The canonical BFM project folder and templates are recorded in `references/template-registry.md`.
- The `terminal` tool must run from the Hermes repo so the helper script can import the repo virtualenv's Google packages.
- Do not send passwords, recovery codes, or Time Doctor credentials in plain text unless the user explicitly provides and approves the exact message.

## How to Run

Use the helper script through `terminal`:

```bash
.venv/bin/python skills/domain/bfm-onboarding/scripts/bfm_onboarding.py verify-templates
```

Create filled document copies for a candidate:

```bash
.venv/bin/python skills/domain/bfm-onboarding/scripts/bfm_onboarding.py create-candidate-docs \
  --full-name "MUHAMMET ALI TASHLIYEV" \
  --passport-id "A1965026" \
  --date-of-birth "06.11.2005" \
  --email "tashliyev123@gmail.com" \
  --phone "+90 546 742 2517"
```

The script outputs JSON with created Drive links and a checklist for manual account steps.

## Quick Reference

| Item | Value |
|---|---|
| Google account | `val@buildfuture.me` |
| BFM project folder | `1hDZf-a3slrw6dYJuw3zEgxUOWaR0oKMj` |
| NDA template | `1xJbxgPIipHIPWPIy1EVLm1OAXrdVI4k4U8TPDhg-sac` |
| Offer template | `1lMaKHWTykwNPqQbPUTDetgQUY3rInix2TRC_i87-OT4` |
| Default candidate folder | `06_Onboarding/<FULL_NAME> - YYYY-MM-DD` |

## Procedure

1. Collect candidate data:
   - legal full name exactly as it should appear in documents;
   - passport or ID number;
   - date of birth;
   - personal email;
   - phone number;
   - role, compensation, hours, start date, and probation terms if they differ from the template.

2. Verify the BFM template setup before generating documents:
   ```bash
   .venv/bin/python skills/domain/bfm-onboarding/scripts/bfm_onboarding.py verify-templates
   ```
   Check that both templates are owned by `val@buildfuture.me`, are not trashed, and are in the BFM project folder.

3. Create candidate document copies:
   ```bash
   .venv/bin/python skills/domain/bfm-onboarding/scripts/bfm_onboarding.py create-candidate-docs \
     --full-name "<FULL_NAME>" \
     --passport-id "<PASSPORT_OR_ID>" \
     --date-of-birth "<DD.MM.YYYY>" \
     --email "<PERSONAL_EMAIL>" \
     --phone "<PHONE>"
   ```

4. Review the created Google Docs before sharing:
   - NDA agreement number and date;
   - candidate name, passport or ID, date of birth, email, and phone;
   - offer title, role text, compensation, weekly hours, and Time Doctor clause;
   - whether any remaining placeholders are intentional.

5. Handle account setup as manual admin work unless a dedicated admin tool is available:
   - create the corporate Google account under the BFM domain;
   - register Time Doctor with the corporate Google account;
   - prepare a credential handoff message for the candidate's personal email;
   - send only after the user approves the exact login data and message.

## Pitfalls

- Do not modify the canonical templates while onboarding a candidate; always copy first.
- Do not use the old bbooster-owned NDA template. It was migrated into the BFM folder and the old source was trashed.
- Do not treat the offer as a pure video-editor offer. The current template is project-manager titled, with video-production functions still inside.
- Do not create or send account passwords from the model's text without an explicit user-approved secret handoff.
- If the script reports missing placeholders, stop and inspect the template instead of overwriting documents manually.

## Verification

- `verify-templates` returns `ok: true`.
- Candidate folder exists under the BFM project folder.
- Created NDA and offer links are owned by `val@buildfuture.me`.
- Created NDA and offer contain no accidental old candidate data.
- Manual admin checklist is visible in the final response.
