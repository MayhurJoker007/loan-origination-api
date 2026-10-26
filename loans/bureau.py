"""A stand-in for a credit bureau. Deterministic per ID number, so the same applicant always gets the same
report, and a few ID numbers are reserved for testing specific outcomes."""
import hashlib

from .credit import Bureau

SCENARIOS = {
    # last four digits of the ID number body -> report
    '0001': Bureau(score=520),                                   # below minimum
    '0002': Bureau(score=650, judgments=1),                      # judgment on record
    '0003': Bureau(score=580),                                   # lowest band
    '0004': Bureau(score=760),                                   # excellent
}


class BureauUnavailable(Exception):
    pass


def fetch(id_number):
    if id_number[6:10] in SCENARIOS:
        return SCENARIOS[id_number[6:10]]
    h = int(hashlib.sha256(id_number.encode()).hexdigest(), 16)
    score = 600 + h % 180
    return Bureau(score=score, accounts_in_arrears=1 if h % 23 == 0 else 0)
