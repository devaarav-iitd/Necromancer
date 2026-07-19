from __future__ import annotations

from types import SimpleNamespace

import pytest

from necromancer.domain.models import StrictModel
from necromancer.llm.client import LLMClientError, LLMRefusalError, OpenAIResponsesClient


class _Answer(StrictModel):
    answer: str


class _Responses:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_client_uses_responses_strict_json_schema_and_validates() -> None:
    responses = _Responses([SimpleNamespace(output_text='{"answer":"ok"}', output=[])])
    client = OpenAIResponsesClient(client=SimpleNamespace(responses=responses))

    result = client.generate(
        system_prompt="system", user_content="user", response_model=_Answer
    )

    assert result.answer == "ok"
    request = responses.calls[0]
    assert request["model"] == "gpt-5.6"
    response_format = request["text"]["format"]  # type: ignore[index]
    assert response_format["type"] == "json_schema"
    assert response_format["strict"] is True
    assert response_format["schema"]["additionalProperties"] is False


def test_client_retries_one_invalid_response() -> None:
    responses = _Responses(
        [
            SimpleNamespace(output_text="not json", output=[]),
            SimpleNamespace(output_text='{"answer":"recovered"}', output=[]),
        ]
    )
    client = OpenAIResponsesClient(client=SimpleNamespace(responses=responses))

    result = client.generate(
        system_prompt="system", user_content="user", response_model=_Answer
    )

    assert result.answer == "recovered"
    assert len(responses.calls) == 2


def test_client_surfaces_a_refusal_without_retrying() -> None:
    responses = _Responses(
        [
            SimpleNamespace(
                output_text="",
                output=[SimpleNamespace(content=[SimpleNamespace(type="refusal", refusal="no")])],
            )
        ]
    )
    client = OpenAIResponsesClient(client=SimpleNamespace(responses=responses))

    with pytest.raises(LLMRefusalError, match="no"):
        client.generate(system_prompt="system", user_content="user", response_model=_Answer)
    assert len(responses.calls) == 1


def test_client_wraps_transport_failures() -> None:
    responses = _Responses([RuntimeError("network unavailable")])
    client = OpenAIResponsesClient(client=SimpleNamespace(responses=responses))

    with pytest.raises(LLMClientError, match="Responses request failed"):
        client.generate(system_prompt="system", user_content="user", response_model=_Answer)
