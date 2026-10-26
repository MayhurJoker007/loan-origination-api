from datetime import date


def luhn_ok(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def parse_sa_id(value, today=None):
    """Returns the date of birth for a valid SA ID number, or raises ValueError with a reason."""
    v = ''.join(c for c in str(value) if not c.isspace())
    if len(v) != 13 or not v.isdigit():
        raise ValueError('ID number must be 13 digits')
    today = today or date.today()
    yy, mm, dd = int(v[:2]), int(v[2:4]), int(v[4:6])
    year = 2000 + yy if 2000 + yy <= today.year else 1900 + yy
    try:
        born = date(year, mm, dd)
    except ValueError:
        raise ValueError('ID number does not start with a valid date of birth')
    if born > today:
        born = date(year - 100, mm, dd)
    if v[10] not in '012':
        raise ValueError('ID number has an invalid citizenship digit')
    if not luhn_ok(v):
        raise ValueError('ID number check digit is wrong')
    return born
