import re

CORPORATE_SUFFIXES = {
    "pte",
    "ltd",
    "limited",
    "inc",
    "incorporated",
    "llc",
}


def normalize_account_identifier(value: str | None) -> str | None:
    if value is None:
        return None

    normalized = re.sub(r"[^0-9]", "", value)

    return normalized or None


def accounts_match(
    expected: str | None,
    detected: str | None,
    minimum_last_digits: int = 4,
) -> bool | None:
    expected_normalized = normalize_account_identifier(expected)
    detected_normalized = normalize_account_identifier(detected)

    if expected_normalized is None or detected_normalized is None:
        return None

    if expected_normalized == detected_normalized:
        return True

    shorter = min(
        len(expected_normalized),
        len(detected_normalized),
    )

    if shorter < minimum_last_digits:
        return False

    digits = min(shorter, minimum_last_digits)

    return expected_normalized[-digits:] == detected_normalized[-digits:]


def normalize_entity_name(value: str | None) -> str | None:
    if value is None:
        return None

    words = re.findall(r"[a-z0-9]+", value.lower())

    while words and words[-1] in CORPORATE_SUFFIXES:
        words.pop()

    normalized = " ".join(words)

    return normalized or None


def entities_match(
    expected: str | None,
    detected: str | None,
) -> bool | None:
    expected_normalized = normalize_entity_name(expected)
    detected_normalized = normalize_entity_name(detected)

    if expected_normalized is None or detected_normalized is None:
        return None

    return expected_normalized == detected_normalized
