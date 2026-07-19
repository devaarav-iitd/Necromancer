"""Small, validated boundary around OpenAI Responses structured outputs."""

from __future__ import annotations

import json
import os
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError


DEFAULT_MODEL = "gpt-5.6"
DEFAULT_TIMEOUT_SECONDS = 120.0
_ModelT = TypeVar("_ModelT", bound=BaseModel)


class LLMClientError(RuntimeError):
    """Base error for failures at the model/controller boundary."""


class MissingAPIKeyError(LLMClientError):
    """Raised when no OpenAI key is available after loading ``.env``."""


class LLMRefusalError(LLMClientError):
    """Raised when the Responses API explicitly refuses the request."""


class StructuredOutputError(LLMClientError):
    """Raised after a malformed or schema-invalid response and one retry."""


class OpenAIResponsesClient:
    """Call GPT with a Pydantic-derived strict JSON Schema response format."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self._client = client or self._create_client(timeout_seconds)

    def generate(
        self,
        *,
        system_prompt: str,
        user_content: str,
        response_model: type[_ModelT],
    ) -> _ModelT:
        """Return a response validated by ``response_model``.

        Structured Outputs should make malformed JSON exceptional, but the
        controller still validates locally. One malformed-output retry keeps a
        transient invalid response from ending a repair run.
        """

        schema = response_model.model_json_schema()
        request = {
            "model": self.model,
            "instructions": system_prompt,
            "input": user_content,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": _schema_name(response_model),
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        last_error: Exception | None = None
        for _ in range(2):
            try:
                response = self._client.responses.create(**request)
            except Exception as error:
                raise LLMClientError("OpenAI Responses request failed") from error
            refusal = _response_refusal(response)
            if refusal is not None:
                raise LLMRefusalError(f"OpenAI refused structured output: {refusal}")
            try:
                output_text = _response_text(response)
                return response_model.model_validate_json(output_text)
            except (TypeError, ValueError, ValidationError, json.JSONDecodeError) as error:
                last_error = error
        raise StructuredOutputError(
            "OpenAI returned malformed or schema-invalid structured output twice"
        ) from last_error

    @staticmethod
    def _create_client(timeout_seconds: float) -> Any:
        try:
            from dotenv import load_dotenv

            load_dotenv()
        except ImportError as error:  # pragma: no cover - dependency metadata enforces this.
            raise LLMClientError("python-dotenv is required for OpenAI configuration") from error

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise MissingAPIKeyError("OPENAI_API_KEY not set (check .env or your shell environment)")
        try:
            from openai import OpenAI
        except ImportError as error:  # pragma: no cover - dependency metadata enforces this.
            raise LLMClientError("openai is required for the real Surgeon") from error
        return OpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=1)


def _schema_name(response_model: type[BaseModel]) -> str:
    """Produce the API's limited schema-name alphabet from a model class name."""

    return "".join(
        character if character.isalnum() or character in {"_", "-"} else "_"
        for character in response_model.__name__
    )[:64]


def _response_text(response: Any) -> str:
    status = getattr(response, "status", None)
    if status is not None and status != "completed":
        raise ValueError(f"Responses API completed with status {status!r}")
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text:
        return output_text
    raise ValueError("Responses API returned no output text")


def _response_refusal(response: Any) -> str | None:
    """Extract refusals from SDK objects or mapping-shaped test doubles."""

    for item in _items(getattr(response, "output", None)):
        for content in _items(_field(item, "content")):
            if _field(content, "type") != "refusal":
                continue
            refusal = _field(content, "refusal") or _field(content, "text")
            return str(refusal or "no refusal text supplied")
    return None


def _items(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)
