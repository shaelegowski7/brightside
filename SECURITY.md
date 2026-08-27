# Security & Incident Response

Brightside is a small (3-person) internal tool. This document defines how
the team detects, responds to, and reports security incidents.

## Roles

- **Primary responder**: Shae Legowski — owns the Railway, Vercel, GitHub,
  Supabase, and Amazon Seller Central accounts. First point of contact
  for any suspected incident.
- **Backup responder**: Jac McLaughlin — can rotate credentials and access
  infrastructure if the primary responder is unavailable.
- Any team member who notices anything suspicious (an unexpected
  failed-login alert, unfamiliar activity, data that looks wrong) reports
  it to the primary responder immediately, by the fastest channel available.

## What counts as an incident

- Unauthorized access to a team member's account (Supabase/email/MFA
  compromise), the database, or the Railway / Vercel / GitHub / Amazon
  Seller Central accounts
- Credential exposure — committed to git, leaked in logs, or shared
  outside the team
- Unexpected or unauthorized changes to deployed code or infrastructure
- Any suspected compromise of Amazon Information obtained via SP-API
  (fee estimates, gating results, ASIN/pricing data)

## Response procedure

1. **Contain** — for a compromised user account, revoke the affected
   user's Supabase sessions (Admin API `auth.admin.sign_out(user_id)` or
   the dashboard's "Sign out user" action) and force a password reset;
   for infrastructure credentials, rotate them immediately (Railway/
   Vercel/Supabase service tokens, SP-API refresh token).
2. **Assess** — determine what was actually accessed, and whether it
   included Amazon Information.
3. **Notify**:
   - If Amazon Information was involved, email **security@amazon.com**
     within **24 hours of detection**, describing what happened and what
     was accessed.
   - Notify every team member.
4. **Document** — add a dated entry to the incident log below: what
   happened, when it was detected, what was done about it.
5. **Review** — revisit this document and update it based on what the
   incident revealed.

## Review schedule

Reviewed every 6 months.

- Last review: 2026-08-28
- Next review: 2027-02-28

## Incident log

_No incidents recorded._
