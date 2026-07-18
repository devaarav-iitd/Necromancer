"""Validated data contracts shared across the controller and LLM stages."""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base model whose JSON Schema disallows undeclared response fields."""

    model_config = ConfigDict(extra="forbid")


class CauseType(str, Enum):
    """Root-cause categories the Coroner may diagnose."""

    REMOVED_STDLIB = "removed_stdlib"
    MISSING_TEST_DEPENDENCY = "missing_test_dependency"
    DEP_UNRESOLVABLE = "dep_unresolvable"
    DEP_API_BREAK = "dep_api_break"
    PACKAGING_OBSOLETE = "packaging_obsolete"
    PY2_SYNTAX = "py2_syntax"
    COLLECTION_IMPORT_ERROR = "collection_import_error"
    UNKNOWN = "unknown"


class Severity(str, Enum):
    FATAL = "fatal"
    DEGRADING = "degrading"


class FailureStage(str, Enum):
    INSTALL = "install"
    COLLECTION = "collection"
    TEST_RUN = "test_run"


class RevivalDifficulty(str, Enum):
    EASY = "easy"
    MODERATE = "moderate"
    HARD = "hard"


class ErrorEvidence(StrictModel):
    """A precise log-backed observation supporting one diagnosed cause."""

    error_message: str = Field(
        min_length=1,
        description="Exact exception or tool error message quoted from the supplied logs.",
    )
    file_path: str = Field(
        min_length=1,
        description="Repository-relative source or test path named by the traceback.",
    )
    line_number: int = Field(
        ge=1,
        description="One-based line number in file_path named by the traceback.",
    )


class DiagnosedCause(StrictModel):
    """One root cause, its impact, and the evidence that supports it."""

    cause_type: CauseType
    summary: str = Field(
        min_length=1,
        description="Concise root-cause diagnosis; do not merely repeat the surface exception.",
    )
    severity: Severity
    failure_stage: FailureStage
    evidence: list[ErrorEvidence] = Field(
        min_length=1,
        description="One or more exact, traceable observations from the supplied logs.",
    )


class DeathCertificate(StrictModel):
    """The Coroner's structured diagnosis of a repository's current failure state."""

    diagnosed_causes: list[DiagnosedCause] = Field(
        min_length=1,
        description="Root causes, ordered from the earliest blocking cause to downstream causes.",
    )
    revival_difficulty: RevivalDifficulty
