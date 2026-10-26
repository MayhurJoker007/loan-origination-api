"""Demo staff users (one per role) with API tokens.

    python manage.py seed_demo
"""
from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand
from rest_framework.authtoken.models import Token

USERS = [('consultant1', 'consultant'), ('officer1', 'credit_officer'), ('officer2', 'credit_officer'),
         ('senior1', 'senior_credit_officer'), ('ops1', 'operations')]


class Command(BaseCommand):
    help = 'Creates one demo user per role and prints their API tokens'

    def handle(self, *args, **options):
        for username, role in USERS:
            user, _ = User.objects.get_or_create(username=username)
            user.groups.add(Group.objects.get(name=role))
            token, _ = Token.objects.get_or_create(user=user)
            self.stdout.write('%-12s %-22s %s' % (username, role, token.key))
