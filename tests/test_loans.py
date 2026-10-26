from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import Group, User
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from loans import credit
from loans.credit import Bureau
from loans.models import AuditEntry, LoanApplication

pytestmark = pytest.mark.django_db
TODAY = date(2026, 10, 26)
EXCELLENT, JUDGMENT, LOW_BAND, BELOW_MIN = '9005150004083', '9005150002087', '9005150003085', '9005150001089'


# --- credit rules (no database) ------------------------------------------------------

def app(**kw):
    base = dict(amount_requested=D('50000'), term_months=36, gross_monthly_income=D('30000'),
                net_monthly_income=D('23000'), declared_living_expenses=D('6000'), existing_debt_repayments=D('2000'))
    return SimpleNamespace(**{**base, **kw})


PERSON = SimpleNamespace(date_of_birth=date(1990, 5, 15), employment_status='permanent', months_employed=40)


def test_minimum_expense_norm_by_income_band():
    assert credit.minimum_expenses(D('700')) == 0
    assert credit.minimum_expenses(D('5000')) == D('1083.50')       # 800 + 6.75% of 4 200
    assert credit.minimum_expenses(D('30000')) == D('3265.38')      # 2 855.38 + 8.2% of 5 000


def test_instalment_and_its_inverse():
    assert credit.instalment(D('50000'), D('0.15'), 36) == D('1733.27')
    assert credit.principal_for(D('1733.27'), D('0.15'), 36) == D('50000')


def test_approve_when_everything_checks_out():
    r = credit.assess(app(), PERSON, Bureau(760), TODAY)
    assert r.outcome == 'approve'
    assert r.discretionary_income == D('15000.00')
    assert r.instalment == D('1733.27') and r.annual_rate == D('0.15')


def test_declared_expenses_below_the_norm_are_replaced():
    r = credit.assess(app(declared_living_expenses=D('500')), PERSON, Bureau(760), TODAY)
    assert r.expenses_used == D('3265.38')


@pytest.mark.parametrize('changes, person, report, reason', [
    ({'amount_requested': D('500')}, {}, 760, 'amount must be between'),
    ({'term_months': 84}, {}, 760, 'term must be between'),
    ({}, {'employment_status': 'unemployed'}, 760, 'no employment income'),
    ({}, {'months_employed': 2}, 760, 'less than 3 months'),
    ({'net_monthly_income': D('3500'), 'gross_monthly_income': D('4000')}, {}, 760, 'net income below'),
    ({}, {'date_of_birth': date(1958, 1, 1)}, 760, 'over 70 by the end'),
    ({}, {}, 520, 'below the minimum'),
    ({'existing_debt_repayments': D('16000')}, {}, 760, 'more than the R'),
])
def test_hard_declines(changes, person, report, reason):
    r = credit.assess(app(**changes), SimpleNamespace(**{**PERSON.__dict__, **person}), Bureau(report), TODAY)
    assert r.outcome == 'decline'
    assert any(reason in x for x in r.reasons), r.reasons


@pytest.mark.parametrize('changes, report, reason', [
    ({}, Bureau(650, judgments=1), 'judgment'),
    ({}, Bureau(580), 'lowest approvable band'),
    ({'amount_requested': D('160000'), 'term_months': 72}, Bureau(760), 'senior decision'),
])
def test_referrals(changes, report, reason):
    r = credit.assess(app(**changes), PERSON, report, TODAY)
    assert r.outcome == 'refer' and any(reason in x for x in r.reasons)


# --- workflow through the API ------------------------------------------------------------

def staff(name, *roles_):
    u = User.objects.create(username=name)
    for r in roles_:
        u.groups.add(Group.objects.get(name=r))
    c = APIClient()
    c.force_authenticate(u)
    return c


@pytest.fixture
def team():
    return SimpleNamespace(consultant=staff('consultant', 'consultant'), officer=staff('officer', 'credit_officer'),
                           officer2=staff('officer2', 'credit_officer'),
                           senior=staff('senior', 'senior_credit_officer'), ops=staff('ops', 'operations'))


def new_application(team, id_number=EXCELLENT, **overrides):
    r = team.consultant.post('/api/applicants/', {
        'id_number': id_number, 'first_name': 'Naledi', 'last_name': 'Khumalo', 'email': 'naledi@example.com',
        'mobile': '0825550123', 'employment_status': 'permanent', 'employer': 'Kasi Fresh', 'months_employed': 40,
    }, format='json')
    assert r.status_code == 201, r.json()
    body = {'applicant_id': r.json()['id'], 'amount_requested': '50000', 'term_months': 36, 'purpose': 'home improvements',
            'gross_monthly_income': '30000', 'net_monthly_income': '23000', 'declared_living_expenses': '6000',
            'existing_debt_repayments': '2000', **overrides}
    r = team.consultant.post('/api/applications/', body, format='json')
    assert r.status_code == 201, r.json()
    return r.json()['reference']


def act(client, ref, name, **data):
    return client.post('/api/applications/%s/%s/' % (ref, name), data, format='json')


def verify_documents(team, ref):
    for kind in ('id_copy', 'payslip', 'bank_statement'):
        doc = team.consultant.post('/api/applications/%s/documents/' % ref,
                                   {'kind': kind, 'file': SimpleUploadedFile(kind + '.pdf', b'%PDF-1.4 ' + kind.encode())},
                                   format='multipart').json()
        assert team.ops.post('/api/documents/%d/review/' % doc['id'], {'verified': True}, format='json').status_code == 200


def test_happy_path_to_disbursement_with_full_history(team):
    ref = new_application(team)
    assert act(team.consultant, ref, 'submit').status_code == 200
    r = act(team.officer, ref, 'assess')
    assert r.json()['status'] == 'assessed'
    assert r.json()['latest_assessment']['bureau_score'] == 760
    verify_documents(team, ref)

    r = act(team.officer2, ref, 'approve', note='stable income, low debt')
    assert r.status_code == 200, r.json()
    assert r.json()['offered_instalment'] == '1733.27'
    assert act(team.consultant, ref, 'accept').status_code == 200
    assert act(team.officer, ref, 'disburse').status_code == 403
    assert act(team.ops, ref, 'disburse').json()['status'] == 'disbursed'

    history = team.officer.get('/api/applications/%s/history/' % ref).json()
    statuses = [h['changes']['status'][1] for h in history if 'status' in h['changes']]
    assert statuses == ['submitted', 'assessed', 'approved', 'offer_accepted', 'disbursed']
    approved = next(h for h in history if h['action'] == 'approved')
    assert approved['actor'] == 'officer2' and approved['changes']['offered_amount'] == [None, '50000.00']


def test_maker_checker(team):
    ref = new_application(team)
    act(team.consultant, ref, 'submit')
    act(team.officer, ref, 'assess')
    verify_documents(team, ref)
    r = act(team.officer, ref, 'approve')
    assert r.status_code == 403 and 'cannot decide' in r.json()['detail']


def test_documents_must_be_verified_before_approval(team):
    ref = new_application(team)
    act(team.consultant, ref, 'submit')
    act(team.officer, ref, 'assess')
    r = act(team.officer2, ref, 'approve')
    assert r.status_code == 409 and 'bank_statement, id_copy, payslip' in r.json()['detail']


def test_approval_limits_and_referrals(team):
    ref = new_application(team, amount_requested='160000', term_months=72, gross_monthly_income='60000',
                          net_monthly_income='45000')
    act(team.consultant, ref, 'submit')
    assert act(team.officer, ref, 'assess').json()['status'] == 'referred'
    verify_documents(team, ref)
    r = act(team.officer2, ref, 'approve')
    assert r.status_code == 403 and 'senior' in r.json()['detail']
    assert act(team.senior, ref, 'approve').json()['status'] == 'approved'


def test_counter_offer_must_be_affordable(team):
    ref = new_application(team, net_monthly_income='12000', gross_monthly_income='15000',
                          declared_living_expenses='4000', existing_debt_repayments='6000')
    act(team.consultant, ref, 'submit')
    r = act(team.officer, ref, 'assess')
    assert r.json()['status'] == 'declined'           # R50k at 15% over 36m is R1 733; only R1 600 a month is spare
    assessment = r.json()['latest_assessment']
    assert assessment['max_affordable_amount'] == '46155.00'
    assert 'instalment R1733.27' in r.json()['decision_reason']


def test_bad_bureau_report_auto_declines_with_reasons(team):
    ref = new_application(team, id_number=BELOW_MIN)
    act(team.consultant, ref, 'submit')
    r = act(team.officer, ref, 'assess')
    assert r.json()['status'] == 'declined' and 'credit score 520' in r.json()['decision_reason']


def test_only_drafts_can_be_edited_and_roles_are_checked(team):
    ref = new_application(team)
    assert team.officer.patch('/api/applications/%s/' % ref, {'term_months': 24}, format='json').status_code == 403
    assert team.consultant.patch('/api/applications/%s/' % ref, {'term_months': 24}, format='json').status_code == 200
    act(team.consultant, ref, 'submit')
    assert team.consultant.patch('/api/applications/%s/' % ref, {'term_months': 12}, format='json').status_code == 409
    assert act(team.consultant, ref, 'assess').status_code == 403
    edit = AuditEntry.objects.get(action='updated')
    assert edit.changes == {'term_months': [36, 24]}
    assert APIClient().get('/api/applications/').status_code in (401, 403)


def test_invalid_id_number_is_rejected(team):
    r = team.consultant.post('/api/applicants/', {
        'id_number': '9005150004084', 'first_name': 'A', 'last_name': 'B', 'email': 'a@example.com',
        'mobile': '0825550123', 'employment_status': 'permanent', 'months_employed': 12}, format='json')
    assert r.status_code == 400 and 'check digit' in str(r.json())


def test_rejected_document_needs_a_reason_and_audit_is_append_only(team):
    ref = new_application(team)
    doc = team.consultant.post('/api/applications/%s/documents/' % ref,
                               {'kind': 'payslip', 'file': SimpleUploadedFile('p.pdf', b'x')}, format='multipart').json()
    assert team.ops.post('/api/documents/%d/review/' % doc['id'], {'verified': False}, format='json').status_code == 409
    assert team.ops.post('/api/documents/%d/review/' % doc['id'], {'verified': False, 'note': 'payslip is from 2023'},
                         format='json').status_code == 200
    entry = AuditEntry.objects.first()
    with pytest.raises(PermissionError):
        entry.delete()
    assert LoanApplication.objects.get(reference=ref).documents.get().status == 'rejected'
