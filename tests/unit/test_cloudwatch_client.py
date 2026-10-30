"""The CloudWatch client the request metrics publish through."""

from __future__ import annotations

from typing import Any

import pytest

from api_deejaytools.config import Settings
from api_deejaytools.services import cloudwatch


@pytest.fixture
def made(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        cloudwatch.boto3,
        "client",
        lambda service, **kw: calls.append((service, kw)) or object(),
    )
    return calls


def test_the_producer_key_is_used_when_both_halves_are_set(
    made: list[tuple[str, dict[str, Any]]],
) -> None:
    settings = Settings.model_construct(
        AWS_REGION="us-east-2",
        EVALUATION_QUEUE_PRODUCER_KEY_ID="AKIA_TEST",
        EVALUATION_QUEUE_PRODUCER_SECRET="secret",
    )
    cloudwatch.client_factory(settings)
    assert made == [
        (
            "cloudwatch",
            {
                "region_name": "us-east-2",
                "aws_access_key_id": "AKIA_TEST",
                "aws_secret_access_key": "secret",
            },
        )
    ]


@pytest.mark.parametrize(
    ("key_id", "secret"), [(None, None), ("AKIA_TEST", None), (None, "secret")]
)
def test_without_a_whole_key_the_default_chain_applies(
    made: list[tuple[str, dict[str, Any]]], key_id: str | None, secret: str | None
) -> None:
    settings = Settings.model_construct(
        AWS_REGION="us-east-2",
        EVALUATION_QUEUE_PRODUCER_KEY_ID=key_id,
        EVALUATION_QUEUE_PRODUCER_SECRET=secret,
    )
    cloudwatch.client_factory(settings)
    assert made == [("cloudwatch", {"region_name": "us-east-2"})]
