"""Credit rules: eligibility, affordability and pricing. Pure functions, no database, so each rule is easy to test
and to explain to a credit committee.

The minimum living-expense table follows the structure of the National Credit Act affordability assessment
regulations (a base amount plus a percentage of gross income above each band). Check the current published
figures before relying on them; they're configuration, not code.
"""
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal('0.01')

# (lower bound of gross monthly income, base expense, % of income above the lower bound)
MINIMUM_EXPENSE_TABLE = [
    (Decimal('0'), Decimal('0'), Decimal('0')),
    (Decimal('800.01'), Decimal('800'), Decimal('0.0675')),
    (Decimal('6250.01'), Decimal('1167.88'), Decimal('0.09')),
    (Decimal('25000.01'), Decimal('2855.38'), Decimal('0.082')),
    (Decimal('50000.01'), Decimal('4905.38'), Decimal('0.0675')),
]

LIMITS = {
    'min_amount': Decimal('1000'), 'max_amount': Decimal('250000'),
    'min_term': 6, 'max_term': 72,
    'min_age': 18, 'max_age_at_end': 70,
    'min_months_employed': 3,
    'min_net_income': Decimal('4000'),
    'affordability_buffer': Decimal('0.80'),    # instalment may use at most 80% of what's left over
    'refer_above_amount': Decimal('150000'),
}

# bureau score bands -> annual rate. Below the lowest band: decline.
PRICING = [(700, Decimal('0.15')), (640, Decimal('0.19')), (600, Decimal('0.235')), (560, Decimal('0.2775'))]


def money(x):
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def minimum_expenses(gross_monthly):
    lower, base, pct = next(row for row in reversed(MINIMUM_EXPENSE_TABLE) if gross_monthly >= row[0])
    if pct == 0:
        return Decimal('0')
    return money(base + (gross_monthly - (lower - CENT)) * pct)


def instalment(principal, annual_rate, months):
    """Standard amortising loan payment."""
    r = Decimal(annual_rate) / 12
    if r == 0:
        return money(principal / months)
    return money(principal * r / (1 - (1 + r) ** -months))


def principal_for(payment, annual_rate, months):
    """The largest loan a given monthly payment can repay. The inverse of instalment()."""
    r = Decimal(annual_rate) / 12
    return (payment * (1 - (1 + r) ** -months) / r).quantize(Decimal('1'), rounding='ROUND_DOWN')


def rate_for(score, max_rate):
    for floor, rate in PRICING:
        if score >= floor:
            return min(rate, Decimal(max_rate))
    return None


@dataclass
class Bureau:
    score: int
    judgments: int = 0
    accounts_in_arrears: int = 0


@dataclass
class Result:
    outcome: str                        # approve | refer | decline
    reasons: list = field(default_factory=list)
    minimum_expenses: Decimal = Decimal('0')
    expenses_used: Decimal = Decimal('0')
    discretionary_income: Decimal = Decimal('0')
    annual_rate: Decimal | None = None
    instalment: Decimal | None = None
    max_affordable_amount: Decimal = Decimal('0')


def age_on(born, on):
    return on.year - born.year - ((on.month, on.day) < (born.month, born.day))


def assess(app, applicant, bureau: Bureau, today: date, max_rate='0.2775'):
    """`app` and `applicant` only need the attributes used here, so plain objects work in tests."""
    declines, refers = [], []
    L = LIMITS

    # --- eligibility: hard rules, any one is a decline ---
    if not (L['min_amount'] <= app.amount_requested <= L['max_amount']):
        declines.append('amount must be between R%s and R%s' % (L['min_amount'], L['max_amount']))
    if not (L['min_term'] <= app.term_months <= L['max_term']):
        declines.append('term must be between %d and %d months' % (L['min_term'], L['max_term']))
    age = age_on(applicant.date_of_birth, today)
    if age < L['min_age']:
        declines.append('applicant is under %d' % L['min_age'])
    if age + app.term_months / 12 > L['max_age_at_end']:
        declines.append('applicant would be over %d by the end of the loan' % L['max_age_at_end'])
    if applicant.employment_status == 'unemployed':
        declines.append('no employment income')
    elif applicant.months_employed < L['min_months_employed']:
        declines.append('employed for less than %d months' % L['min_months_employed'])
    if app.net_monthly_income < L['min_net_income']:
        declines.append('net income below R%s' % L['min_net_income'])
    if app.net_monthly_income > app.gross_monthly_income:
        declines.append('net income is higher than gross income; check the figures')

    rate = rate_for(bureau.score, max_rate)
    if rate is None:
        declines.append('credit score %d is below the minimum' % bureau.score)
    if bureau.judgments:
        refers.append('%d judgment(s) on the credit report' % bureau.judgments)
    if bureau.accounts_in_arrears:
        refers.append('%d account(s) in arrears' % bureau.accounts_in_arrears)

    # --- affordability ---
    minimum = minimum_expenses(app.gross_monthly_income)
    # if someone declares less than the minimum norm, the norm is used: R500 a month for a family isn't plausible
    expenses = max(app.declared_living_expenses, minimum)
    discretionary = money(app.net_monthly_income - expenses - app.existing_debt_repayments)
    result = Result('approve', minimum_expenses=minimum, expenses_used=expenses, discretionary_income=discretionary,
                    annual_rate=rate)

    if rate is not None and app.term_months > 0:
        result.instalment = instalment(app.amount_requested, rate, app.term_months)
        available = money(max(Decimal('0'), discretionary) * L['affordability_buffer'])
        result.max_affordable_amount = principal_for(available, rate, app.term_months) if available > 0 else Decimal('0')
        if result.instalment > available:
            declines.append('instalment R%s is more than the R%s affordable each month' % (result.instalment, available))

    if not declines and app.amount_requested > L['refer_above_amount']:
        refers.append('amount above R%s needs a senior decision' % L['refer_above_amount'])
    if rate is not None and bureau.score < 600:
        refers.append('score %d is in the lowest approvable band' % bureau.score)

    if declines:
        result.outcome, result.reasons = 'decline', declines
    elif refers:
        result.outcome, result.reasons = 'refer', refers
    else:
        result.reasons = ['meets all eligibility and affordability rules']
    return result
