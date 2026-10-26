from decimal import Decimal

from django.conf import settings
from django.db import models


class Applicant(models.Model):
    class Employment(models.TextChoices):
        PERMANENT = 'permanent'
        CONTRACT = 'contract'
        SELF_EMPLOYED = 'self_employed'
        UNEMPLOYED = 'unemployed'

    id_number = models.CharField(max_length=13, unique=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    date_of_birth = models.DateField()
    email = models.EmailField()
    mobile = models.CharField(max_length=15)
    employment_status = models.CharField(max_length=15, choices=Employment.choices)
    employer = models.CharField(max_length=200, blank=True)
    months_employed = models.PositiveIntegerField(default=0)

    def __str__(self):
        return '%s %s' % (self.first_name, self.last_name)


class LoanApplication(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'draft'
        SUBMITTED = 'submitted'
        ASSESSED = 'assessed'          # rules and bureau have run; waiting for a credit decision
        REFERRED = 'referred'          # needs a senior credit officer
        APPROVED = 'approved'
        DECLINED = 'declined'
        OFFER_ACCEPTED = 'offer_accepted'
        DISBURSED = 'disbursed'
        WITHDRAWN = 'withdrawn'

    reference = models.CharField(max_length=12, unique=True)
    applicant = models.ForeignKey(Applicant, on_delete=models.PROTECT, related_name='applications')
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.DRAFT, db_index=True)
    amount_requested = models.DecimalField(max_digits=12, decimal_places=2)
    term_months = models.PositiveSmallIntegerField()
    purpose = models.CharField(max_length=40)

    # what the applicant declares; the assessment checks these against minimum norms
    gross_monthly_income = models.DecimalField(max_digits=12, decimal_places=2)
    net_monthly_income = models.DecimalField(max_digits=12, decimal_places=2)
    declared_living_expenses = models.DecimalField(max_digits=12, decimal_places=2)
    existing_debt_repayments = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0'))

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='+')
    assessed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                    related_name='+')
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name='+')
    decision_reason = models.TextField(blank=True)

    offered_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    offered_annual_rate = models.DecimalField(max_digits=6, decimal_places=4, null=True, blank=True)
    offered_instalment = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(condition=models.Q(amount_requested__gt=0), name='amount_positive'),
            models.CheckConstraint(condition=models.Q(term_months__gt=0), name='term_positive'),
        ]

    def __str__(self):
        return self.reference


class Assessment(models.Model):
    """A snapshot of the numbers a decision was based on. Kept even if the application is later reassessed."""

    application = models.ForeignKey(LoanApplication, on_delete=models.CASCADE, related_name='assessments')
    created_at = models.DateTimeField(auto_now_add=True)
    bureau_score = models.PositiveSmallIntegerField()
    bureau_judgments = models.PositiveSmallIntegerField(default=0)
    bureau_accounts_in_arrears = models.PositiveSmallIntegerField(default=0)
    minimum_expenses = models.DecimalField(max_digits=12, decimal_places=2)
    expenses_used = models.DecimalField(max_digits=12, decimal_places=2)
    discretionary_income = models.DecimalField(max_digits=12, decimal_places=2)
    annual_rate = models.DecimalField(max_digits=6, decimal_places=4, null=True)
    instalment = models.DecimalField(max_digits=12, decimal_places=2, null=True)
    max_affordable_amount = models.DecimalField(max_digits=12, decimal_places=2)
    outcome = models.CharField(max_length=12)            # approve | refer | decline
    reasons = models.JSONField(default=list)

    class Meta:
        ordering = ['-created_at']


class Document(models.Model):
    class Kind(models.TextChoices):
        ID_COPY = 'id_copy'
        PAYSLIP = 'payslip'
        BANK_STATEMENT = 'bank_statement'
        PROOF_OF_ADDRESS = 'proof_of_address'

    class Status(models.TextChoices):
        PENDING = 'pending'
        VERIFIED = 'verified'
        REJECTED = 'rejected'

    application = models.ForeignKey(LoanApplication, on_delete=models.CASCADE, related_name='documents')
    kind = models.CharField(max_length=20, choices=Kind.choices)
    file_name = models.CharField(max_length=200)
    sha256 = models.CharField(max_length=64)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    verified_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT)
    note = models.CharField(max_length=300, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)


class AuditEntry(models.Model):
    """Every change to an application: who, when, what changed from what to what. Append-only."""

    application = models.ForeignKey(LoanApplication, on_delete=models.PROTECT, related_name='history')
    at = models.DateTimeField(auto_now_add=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=40)
    changes = models.JSONField(default=dict)      # {"field": [old, new]}
    note = models.TextField(blank=True)

    class Meta:
        ordering = ['at', 'id']

    def save(self, *args, **kwargs):
        if self.pk:
            raise PermissionError('audit entries are append-only')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError('audit entries are append-only')
