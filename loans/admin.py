from django.contrib import admin

from .models import Applicant, AuditEntry, Document, LoanApplication


@admin.register(LoanApplication)
class LoanApplicationAdmin(admin.ModelAdmin):
    list_display = ['reference', 'applicant', 'status', 'amount_requested', 'term_months', 'created_at']
    list_filter = ['status']
    search_fields = ['reference', 'applicant__last_name']


admin.site.register(Applicant)
admin.site.register(Document)


@admin.register(AuditEntry)
class AuditEntryAdmin(admin.ModelAdmin):
    list_display = ['at', 'application', 'actor', 'action']

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
