"""The application lifecycle. Every state change goes through here, so the rules are in one place and every
change is audited.

    draft ─submit─► submitted ─assess─► assessed ─approve─► approved ─accept─► offer_accepted ─disburse─► disbursed
                                 │           └─decline─► declined
                                 ├─(rules say decline)─► declined
                                 └─(rules say refer)──► referred ─approve/decline (senior only)
    draft / submitted / assessed / referred / approved ─withdraw─► withdrawn
"""
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import bureau, credit
from .models import Assessment, AuditEntry, Document, LoanApplication

S = LoanApplication.Status
REQUIRED_DOCUMENTS = {Document.Kind.ID_COPY, Document.Kind.PAYSLIP, Document.Kind.BANK_STATEMENT}
EDITABLE_FIELDS = ['amount_requested', 'term_months', 'purpose', 'gross_monthly_income', 'net_monthly_income',
                   'declared_living_expenses', 'existing_debt_repayments']


class WorkflowError(Exception):
    """The request makes sense but not in the application's current state (HTTP 409)."""


class NotAllowed(Exception):
    """This user may not do this (HTTP 403)."""


def roles(user):
    return set(user.groups.values_list('name', flat=True)) | ({'superuser'} if user.is_superuser else set())


def _audit(app, user, action, changes=None, note=''):
    AuditEntry.objects.create(application=app, actor=user, action=action, changes=changes or {}, note=note)


def _plain(v):
    if isinstance(v, Decimal):
        return str(v)
    if hasattr(v, 'get_username'):
        return v.get_username()
    return v


def _move(app, to, user, action, note='', **fields):
    changes = {'status': [app.status, to]}
    for k, v in fields.items():
        changes[k] = [_plain(getattr(app, k)), _plain(v)]
        setattr(app, k, v)
    app.status = to
    app.save()
    _audit(app, user, action, changes, note)
    return app


def _require(app, *allowed):
    if app.status not in allowed:
        raise WorkflowError('application is %s; this needs it to be %s' % (app.status, ' or '.join(allowed)))


def next_reference():
    last = LoanApplication.objects.order_by('-id').values_list('id', flat=True).first() or 0
    return 'LA-%06d' % (last + 1)


@transaction.atomic
def create(applicant, data, user):
    app = LoanApplication.objects.create(reference=next_reference(), applicant=applicant, created_by=user, **data)
    _audit(app, user, 'created', {k: [None, _plain(getattr(app, k))] for k in EDITABLE_FIELDS})
    return app


@transaction.atomic
def update(app, data, user):
    _require(app, S.DRAFT)
    changes = {}
    for k, v in data.items():
        if k in EDITABLE_FIELDS and getattr(app, k) != v:
            changes[k] = [_plain(getattr(app, k)), _plain(v)]
            setattr(app, k, v)
    if changes:
        app.save()
        _audit(app, user, 'updated', changes)
    return app


@transaction.atomic
def submit(app, user):
    _require(app, S.DRAFT)
    return _move(app, S.SUBMITTED, user, 'submitted')


@transaction.atomic
def assess(app, user, today=None):
    _require(app, S.SUBMITTED)
    if not roles(user) & {'credit_officer', 'senior_credit_officer', 'superuser'}:
        raise NotAllowed('only credit officers can run an assessment')

    report = bureau.fetch(app.applicant.id_number)
    result = credit.assess(app, app.applicant, report, today or timezone.localdate(),
                           settings.LOANS['MAX_ANNUAL_RATE'])
    Assessment.objects.create(
        application=app, bureau_score=report.score, bureau_judgments=report.judgments,
        bureau_accounts_in_arrears=report.accounts_in_arrears, minimum_expenses=result.minimum_expenses,
        expenses_used=result.expenses_used, discretionary_income=result.discretionary_income,
        annual_rate=result.annual_rate, instalment=result.instalment,
        max_affordable_amount=result.max_affordable_amount, outcome=result.outcome, reasons=result.reasons)

    note = '; '.join(result.reasons)
    if result.outcome == 'decline':
        # a hard-rule decline is the system's decision, recorded with the reasons
        return _move(app, S.DECLINED, user, 'auto_declined', note, assessed_by=user, decision_reason=note)
    to = S.REFERRED if result.outcome == 'refer' else S.ASSESSED
    return _move(app, to, user, 'assessed', note, assessed_by=user)


def _check_authority(app, user, amount):
    r = roles(user)
    if 'superuser' in r:
        return
    if app.status == S.REFERRED and 'senior_credit_officer' not in r:
        raise NotAllowed('referred applications need a senior credit officer')
    limits = settings.LOANS['APPROVAL_LIMITS']
    limit = max((limits[role] for role in r if role in limits), default=0)
    if amount > limit:
        raise NotAllowed('your approval limit is R%s' % limit)
    # maker-checker: nobody approves an application they captured or assessed
    if user.pk in (app.created_by_id, app.assessed_by_id):
        raise NotAllowed('the person who captured or assessed an application cannot decide it')


@transaction.atomic
def approve(app, user, amount=None, note=''):
    _require(app, S.ASSESSED, S.REFERRED)
    latest = app.assessments.first()
    amount = Decimal(str(amount)) if amount is not None else app.amount_requested
    if amount > app.amount_requested:
        raise WorkflowError('an offer cannot be more than the amount applied for')
    if amount > latest.max_affordable_amount:
        raise WorkflowError('R%s is more than the R%s the applicant can afford' % (amount, latest.max_affordable_amount))
    missing = REQUIRED_DOCUMENTS - set(app.documents.filter(status=Document.Status.VERIFIED)
                                       .values_list('kind', flat=True))
    if missing:
        raise WorkflowError('documents not yet verified: %s' % ', '.join(sorted(missing)))
    _check_authority(app, user, amount)

    rate = latest.annual_rate
    return _move(app, S.APPROVED, user, 'approved', note, decided_by=user, decision_reason=note,
                 offered_amount=amount, offered_annual_rate=rate,
                 offered_instalment=credit.instalment(amount, rate, app.term_months))


@transaction.atomic
def decline(app, user, reason):
    _require(app, S.ASSESSED, S.REFERRED)
    if not reason.strip():
        raise WorkflowError('a reason is required to decline')
    _check_authority(app, user, Decimal('0'))
    return _move(app, S.DECLINED, user, 'declined', reason, decided_by=user, decision_reason=reason)


@transaction.atomic
def accept_offer(app, user):
    _require(app, S.APPROVED)
    return _move(app, S.OFFER_ACCEPTED, user, 'offer_accepted')


@transaction.atomic
def disburse(app, user):
    _require(app, S.OFFER_ACCEPTED)
    if not roles(user) & {'operations', 'superuser'}:
        raise NotAllowed('only operations can disburse')
    return _move(app, S.DISBURSED, user, 'disbursed')


@transaction.atomic
def withdraw(app, user, reason=''):
    _require(app, S.DRAFT, S.SUBMITTED, S.ASSESSED, S.REFERRED, S.APPROVED)
    return _move(app, S.WITHDRAWN, user, 'withdrawn', reason)


@transaction.atomic
def add_document(app, kind, file_name, sha256, user):
    if app.status in (S.DECLINED, S.DISBURSED, S.WITHDRAWN):
        raise WorkflowError('application is closed')
    doc = Document.objects.create(application=app, kind=kind, file_name=file_name, sha256=sha256)
    _audit(app, user, 'document_added', {'document': [None, '%s %s' % (kind, file_name)]})
    return doc


@transaction.atomic
def review_document(doc, user, verified, note=''):
    if not roles(user) & {'operations', 'credit_officer', 'senior_credit_officer', 'superuser'}:
        raise NotAllowed('only operations or credit staff can verify documents')
    if doc.status != Document.Status.PENDING:
        raise WorkflowError('document was already %s' % doc.status)
    if not verified and not note.strip():
        raise WorkflowError('say why the document is rejected so the applicant can fix it')
    old = doc.status
    doc.status = Document.Status.VERIFIED if verified else Document.Status.REJECTED
    doc.verified_by, doc.note = user, note
    doc.save()
    _audit(doc.application, user, 'document_reviewed', {'document %s %s' % (doc.kind, doc.pk): [old, doc.status]}, note)
    return doc
