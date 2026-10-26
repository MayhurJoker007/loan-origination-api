from django.apps import AppConfig
from django.db.models.signals import post_migrate

ROLES = ('consultant', 'credit_officer', 'senior_credit_officer', 'operations')


def _roles(sender, **kwargs):
    from django.contrib.auth.models import Group
    for role in ROLES:
        Group.objects.get_or_create(name=role)


class LoansConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'loans'

    def ready(self):
        post_migrate.connect(_roles, sender=self)
