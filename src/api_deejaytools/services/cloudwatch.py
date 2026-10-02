"""The CloudWatch client the request-metrics middleware publishes through (CD-036).

The middleware lives in common-python-utils and takes no dependency on
boto3; it asks for a client on its first publish, from its flush thread,
never on a request. It records nothing outside production.
"""

from __future__ import annotations

from typing import Any

import boto3

from ..config import Settings


def client_factory(settings: Settings) -> Any:
    """A CloudWatch client for this API's region and metrics credentials.

    With no key configured, boto3's default credential chain applies.
    """
    credentials: dict[str, str] = {}
    if settings.DEEJAYTOOLS_METRICS_KEY_ID and settings.DEEJAYTOOLS_METRICS_SECRET:
        credentials = {
            "aws_access_key_id": settings.DEEJAYTOOLS_METRICS_KEY_ID,
            "aws_secret_access_key": settings.DEEJAYTOOLS_METRICS_SECRET,
        }
    return boto3.client("cloudwatch", region_name=settings.AWS_REGION, **credentials)
