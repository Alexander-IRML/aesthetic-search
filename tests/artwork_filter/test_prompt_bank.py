import pytest

from artsearch.artwork_filter.enums import ContentClass
from artsearch.artwork_filter.prompt_bank import PROMPTED_CLASSES, PromptBank, load_prompt_bank


def test_default_prompt_bank_is_complete_and_deterministic():
    bank = load_prompt_bank()

    prompts, memberships = bank.flattened()

    assert bank.version == "1.0.0"
    assert set(bank.classes) == set(PROMPTED_CLASSES)
    assert memberships[0] == ContentClass.FINISHED_ILLUSTRATION
    assert len(prompts) == len(memberships)
    assert bank.prompt_hash


def test_prompt_bank_rejects_missing_class():
    with pytest.raises(ValueError, match="prompt bank has no prompts"):
        PromptBank(version="test", classes={ContentClass.FINISHED_ILLUSTRATION: ["art"]})
