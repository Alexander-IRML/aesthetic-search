from __future__ import annotations

from pathlib import Path
import tomllib

from pydantic import BaseModel, Field, field_validator, model_validator

from artsearch.artwork_filter.enums import ContentClass
from artsearch.artwork_filter.hashing import stable_json_hash


PROMPTED_CLASSES = tuple(
    content_class for content_class in ContentClass if content_class is not ContentClass.UNKNOWN
)


class PromptBank(BaseModel):
    version: str
    top_k: int = 2
    classes: dict[ContentClass, list[str]] = Field(default_factory=dict)
    prompt_hash: str = ""

    @field_validator("version")
    @classmethod
    def _version_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt-bank version is required")
        return value

    @field_validator("top_k")
    @classmethod
    def _positive_top_k(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("prompt-bank top_k must be positive")
        return value

    @model_validator(mode="after")
    def _complete_and_unique(self) -> "PromptBank":
        missing = [item.value for item in PROMPTED_CLASSES if not self.classes.get(item)]
        if missing:
            raise ValueError(f"prompt bank has no prompts for: {', '.join(missing)}")

        prompts = [prompt.strip() for item in PROMPTED_CLASSES for prompt in self.classes[item]]
        if any(not prompt for prompt in prompts):
            raise ValueError("prompt bank contains an empty prompt")
        normalized = [prompt.casefold() for prompt in prompts]
        if len(normalized) != len(set(normalized)):
            raise ValueError("prompt bank contains a duplicate prompt")
        return self

    def flattened(self) -> tuple[list[str], list[ContentClass]]:
        prompts: list[str] = []
        memberships: list[ContentClass] = []
        for content_class in PROMPTED_CLASSES:
            class_prompts = self.classes[content_class]
            prompts.extend(class_prompts)
            memberships.extend([content_class] * len(class_prompts))
        return prompts, memberships


def load_prompt_bank(
    path: str | Path = "configs/artwork_filter.prompts.v1.toml",
) -> PromptBank:
    prompt_path = Path(path)
    with prompt_path.open("rb") as handle:
        raw = tomllib.load(handle)
    bank = PromptBank.model_validate(raw)
    bank.prompt_hash = stable_json_hash(bank.model_dump(mode="json", exclude={"prompt_hash"}))
    return bank
