from __future__ import annotations

from enum import Enum


class FilterDecision(str, Enum):
    ACCEPT = "accept"
    REVIEW = "review"
    REJECT = "reject"
    ERROR = "error"


class ContentClass(str, Enum):
    FINISHED_ILLUSTRATION = "finished_illustration"
    TRADITIONAL_ART = "traditional_art"
    COMIC = "comic"
    CHARACTER_SHEET = "character_sheet"
    SKETCH_OR_WIP = "sketch_or_wip"
    THREE_D_RENDER = "three_d_render"
    COMMISSION_SHEET = "commission_sheet"
    ADOPTABLE_SHEET = "adoptable_sheet"
    ART_MERCH_PHOTO = "art_merch_photo"
    PHOTO_OF_ART = "photo_of_art"
    CASUAL_PHOTO = "casual_photo"
    SELFIE = "selfie"
    FOOD_PHOTO = "food_photo"
    PET_PHOTO = "pet_photo"
    SCREENSHOT = "screenshot"
    MEME = "meme"
    TEXT_ANNOUNCEMENT = "text_announcement"
    OTHER = "other"
    UNKNOWN = "unknown"


class RuleDisposition(str, Enum):
    CONTINUE = "continue"
    FORCE_ACCEPT = "force_accept"
    FORCE_REVIEW = "force_review"
    FORCE_REJECT = "force_reject"


class ModelMode(str, Enum):
    ZERO_SHOT = "zero_shot"
    SUPERVISED = "supervised"
    HYBRID = "hybrid"


class HumanContentLabel(str, Enum):
    FINISHED_ILLUSTRATION = "finished_illustration"
    TRADITIONAL_ART = "traditional_art"
    COMIC = "comic"
    CHARACTER_SHEET = "character_sheet"
    SKETCH_OR_WIP = "sketch_or_wip"
    THREE_D_RENDER = "three_d_render"
    COMMISSION_SHEET = "commission_sheet"
    ADOPTABLE_SHEET = "adoptable_sheet"
    ART_MERCH_PHOTO = "art_merch_photo"
    PHOTO_OF_ART = "photo_of_art"
    CASUAL_PHOTO = "casual_photo"
    SELFIE = "selfie"
    FOOD_PHOTO = "food_photo"
    PET_PHOTO = "pet_photo"
    SCREENSHOT = "screenshot"
    MEME = "meme"
    TEXT_ANNOUNCEMENT = "text_announcement"
    OTHER = "other"
    UNCERTAIN = "uncertain"


class OriginalWorkLabel(str, Enum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class CorpusInclusionLabel(str, Enum):
    YES = "yes"
    NO = "no"
    REVIEW = "review"
