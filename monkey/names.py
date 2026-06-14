import random

_ROMAN_NUMERAL_TABLE = [
    (1000, "M"),
    (900, "CM"),
    (500, "D"),
    (400, "CD"),
    (100, "C"),
    (90, "XC"),
    (50, "L"),
    (40, "XL"),
    (10, "X"),
    (9, "IX"),
    (5, "V"),
    (4, "IV"),
    (1, "I"),
]


def _to_roman(n):
    parts = []
    for value, symbol in _ROMAN_NUMERAL_TABLE:
        count, n = divmod(n, value)
        parts.append(symbol * count)
    return "".join(parts)


def generate_monkey_name():
    """Pick a random name from the MonkeyName pool, disambiguating duplicates
    with roman numerals (e.g. the 6th "Arthur" becomes "Arthur VI")."""
    from monkey.models import Monkey, MonkeyName

    base = random.choice(list(MonkeyName.objects.values_list("name", flat=True)))
    count = Monkey.objects.filter(name__startswith=base).count()
    if count == 0:
        return base
    return f"{base} {_to_roman(count + 1)}"
