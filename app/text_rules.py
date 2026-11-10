"""Wording rules for everything the practice shows or sends: the AI's text is cleaned the same way everywhere."""

import re

# The practice never uses em dashes. Any the AI writes become commas; so does a spaced en dash used the same way
# (an en dash in a range such as "60–85" or "£199 – £299" is kept).
DASHES = re.compile(r"[ \t]*\u2014[ \t]*|(?<![\d£$€%])[ \t]+\u2013[ \t]+(?![\d£$€])")


def no_em_dashes(text: str) -> str:
    text = re.sub(r"(?m)^([ \t#>*]*)\u2014[ \t]*", r"\1", text)  # a dash opening a line (or heading) is dropped
    text = re.sub(r"(?m)[ \t]*\u2014[ \t]*$", "", text)  # and so is one ending a line
    text = DASHES.sub(", ", text)
    text = re.sub(r",[ \t]*,", ",", text)  # "word, - more" had its comma already
    return re.sub(r"([.!?:;])[ \t]*, ", r"\1 ", text)  # and so had "word. - More"


def without_em_dashes(value):
    """no_em_dashes for every text in a record (lists and dicts included)."""
    if isinstance(value, str):
        return no_em_dashes(value)
    if isinstance(value, list):
        return [without_em_dashes(item) for item in value]
    if isinstance(value, dict):
        return {key: without_em_dashes(item) for key, item in value.items()}
    return value
