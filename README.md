# Loan Origination & Credit Assessment API

![CI](https://github.com/MayhurJoker007/loan-origination-api/actions/workflows/ci.yml/badge.svg)

The back office of a personal-loan product: an application is captured, assessed against eligibility and
affordability rules and a credit bureau report, decided by someone with the right authority, then accepted and
paid out. Every change along the way is recorded with who made it and what changed.

**Stack:** Python 3.12, Django, Django REST Framework, PostgreSQL, Docker, Kubernetes manifests, GitHub Actions.

```
draft ─submit─► submitted ─assess─► assessed ─approve─► approved ─accept─► offer_accepted ─disburse─► disbursed
                              │          └──decline──► declined
                              ├─ rules say no ───────► declined (automatic, with the reasons)
                              └─ rules say refer ────► referred ─► senior credit officer approves or declines
          (anything before acceptance) ─withdraw─► withdrawn
```

## The credit rules

`loans/credit.py` is pure functions with no database, so each rule can be tested and explained on its own.

**Eligibility** (any one fails the application): loan amount R1 000 to R250 000, term 6 to 72 months, age 18+ and
no older than 70 by the end of the loan, employed (3+ months with the current employer), net income of at least
R4 000, and net income that isn't higher than gross.

**Affordability:**
- Living expenses are the higher of what the applicant declares and a **minimum expense norm** by income band.
  This follows the structure of the NCA affordability regulations: a base amount plus a percentage of income
  above each band. So someone earning R30 000 can't declare R500 of expenses.
- Discretionary income is net income less those expenses and existing debt repayments.
- The instalment (standard amortisation) may use at most **80%** of discretionary income.
- The assessment also stores the **maximum affordable amount** (the inverse of the instalment formula), so a
  credit officer can make a lower counter-offer instead of declining.

**Risk-based pricing** from the bureau score: 700+ → 15%, 640+ → 19%, 600+ → 23.5%, 560+ → 27.75% (capped by a
configurable maximum rate). Below 560 is a decline.

**Referrals** go to a senior instead of being approved or declined automatically: judgments or accounts in arrears
on the bureau report, a score in the lowest band, or an amount over R150 000.

Every assessment is saved as a snapshot (score, expense norm, expenses used, discretionary income, rate,
instalment, maximum affordable amount, outcome and reasons), so a decision can always be explained later, even
after a reassessment.

The thresholds and rates are illustrative configuration for a demo, not a real lender's credit policy.

## Controls around the decision

- **Approval limits by role:** a credit officer can approve up to R50 000, a senior credit officer up to
  R250 000, and referred applications always need a senior.
- **Maker-checker:** whoever captured or assessed an application can't decide it.
- **Documents first:** ID copy, payslip and bank statement must all be *verified* before approval. A rejected
  document needs a reason, so the applicant knows what to fix. Each document's SHA-256 is stored, tying the
  decision to the exact file that was reviewed.
- **Only drafts can be edited.** After submission the numbers are fixed, and what was assessed is what's decided.
- **Offers can't exceed** the amount applied for or the maximum affordable amount.
- **Operations disburses.** Credit staff can't release money.
- **ID numbers** are validated (date of birth, citizenship digit, Luhn check digit) and masked in API responses.

## Audit history

`GET /api/applications/{ref}/history/` returns every change as a field-level diff:

```json
{"at": "...", "actor": "officer2", "action": "approved", "note": "stable income, low debt",
 "changes": {"status": ["assessed", "approved"], "offered_amount": [null, "50000.00"],
             "offered_annual_rate": [null, "0.1500"], "offered_instalment": [null, "1733.27"],
             "decided_by": [null, "officer2"]}}
```

Entries can't be edited or deleted, through the ORM or the admin.

## Running it

```
docker compose up --build
docker compose exec api python manage.py seed_demo     # one user per role, with API tokens
```

| | | |
|---|---|---|
| POST | `/api/applicants/` | capture an applicant (consultant) |
| POST/GET | `/api/applications/` | create or list applications |
| GET/PATCH | `/api/applications/{ref}/` | view, or edit while still a draft |
| POST | `/api/applications/{ref}/submit/` `assess/` `approve/` `decline/` `accept/` `disburse/` `withdraw/` | lifecycle actions |
| POST | `/api/applications/{ref}/documents/` | upload a document (multipart: `kind`, `file`) |
| POST | `/api/documents/{id}/review/` | `{"verified": true}` or `{"verified": false, "note": "..."}` |
| GET | `/api/applications/{ref}/history/` | audit trail |

The mock credit bureau is deterministic per ID number. ID numbers whose digits 7 to 10 are `0001` to `0004`
return fixed reports (below minimum, a judgment, the lowest band, excellent) for trying out each path.

## Tests

```
pytest
```

The 24 tests cover the expense norm table, the instalment formula and its inverse, every hard decline,
every referral reason, the full path to disbursement with its audit trail, maker-checker, document gating,
approval limits on a referred application, affordability-driven declines with the maximum affordable amount,
automatic declines from the bureau, draft-only editing and roles, ID validation, and the append-only audit
log. CI runs them on PostgreSQL 16.
