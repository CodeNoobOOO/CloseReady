from closeready.document_normalization import (
    accounts_match,
    entities_match,
    normalize_account_identifier,
    normalize_entity_name,
)


def test_normalize_full_account_number():
    assert normalize_account_identifier("001-234-567") == "001234567"


def test_normalize_masked_account():
    assert normalize_account_identifier("****4567") == "4567"


def test_full_and_masked_accounts_match_by_last_digits():
    assert (
        accounts_match(
            "001234567",
            "****4567",
        )
        is True
    )


def test_different_account_does_not_match():
    assert (
        accounts_match(
            "001234567",
            "****9999",
        )
        is False
    )


def test_missing_account_is_unknown():
    assert (
        accounts_match(
            "001234567",
            None,
        )
        is None
    )


def test_entity_matching_ignores_case_and_punctuation():
    assert (
        entities_match(
            "Northstar Pte Ltd",
            "NORTHSTAR PTE. LTD.",
        )
        is True
    )


def test_entity_matching_handles_common_suffix_variation():
    assert (
        entities_match(
            "Northstar Pte Ltd",
            "Northstar",
        )
        is True
    )


def test_different_entity_does_not_match():
    assert (
        entities_match(
            "Northstar Pte Ltd",
            "Southstar Pte Ltd",
        )
        is False
    )


def test_missing_entity_is_unknown():
    assert (
        entities_match(
            "Northstar Pte Ltd",
            None,
        )
        is None
    )


def test_normalize_entity_name():
    assert normalize_entity_name("Northstar Pte. Ltd.") == "northstar"
