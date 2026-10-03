# Copyright 2025-2026 Dimensional Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from io import BytesIO
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request

import numpy as np
import pytest
from pytest_mock import MockerFixture

from dimos.perception.experimental import vlm_caption_provider
from dimos.perception.experimental.vlm_caption_provider import VlmCaptionProvider


@pytest.mark.parametrize(
    ("base_url", "token_parameter"),
    [
        ("https://api.openai.com", "max_completion_tokens"),
        ("https://api.openai.com/", "max_completion_tokens"),
        ("http://localhost:8000", "max_tokens"),
        ("https://api.openai.com.example.test", "max_tokens"),
    ],
)
def test_caption_uses_endpoint_token_parameter(
    base_url: str, token_parameter: str, mocker: MockerFixture
) -> None:
    provider = VlmCaptionProvider(base_url, "gpt-4o-mini", max_tokens=256)
    response = BytesIO(json.dumps({"choices": [{"message": {"content": "An office"}}]}).encode())
    request_mock = mocker.patch.object(vlm_caption_provider, "urlopen", return_value=response)

    assert provider.caption(np.zeros((2, 2, 3), dtype=np.uint8)) == "An office"
    request: Request = request_mock.call_args.args[0]
    assert request.data is not None
    payload = json.loads(request.data)
    assert payload.pop("messages")[0]["role"] == "user"
    assert payload == {"model": "gpt-4o-mini", token_parameter: 256}
    assert response.closed


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b'{"error":{"message":"Invalid image"}}', '{"error":{"message":"Invalid image"}}'),
        (b"", "[empty]"),
        (b"\xff", "\ufffd"),
        (b"x" * 5000, "x" * 4096),
    ],
)
def test_http_error_logs_response_body(body: bytes, expected: str, mocker: MockerFixture) -> None:
    provider = VlmCaptionProvider("https://example.test", "test-model")
    response = BytesIO(body)
    error = HTTPError("https://example.test", 400, "Bad Request", None, response)
    mocker.patch.object(vlm_caption_provider, "urlopen", side_effect=error)
    warning = mocker.patch.object(vlm_caption_provider.logger, "warning")

    assert provider.caption(np.zeros((2, 2, 3), dtype=np.uint8)) is None
    warning.assert_called_once_with(
        f"VlmCaptionProvider: request failed: HTTP Error 400: Bad Request; response body: {expected}"
    )
    assert response.closed


def test_http_error_redacts_key_and_image(mocker: MockerFixture) -> None:
    provider = VlmCaptionProvider("https://example.test", "test-model", api_key="test-secret")

    def reject_request(request, **kwargs):
        payload = json.loads(request.data)
        image_url = payload["messages"][0]["content"][0]["image_url"]["url"]
        body = f"test-secret {image_url} {image_url.split(',')[1]}".encode()
        raise HTTPError(request.full_url, 400, "Bad Request", None, BytesIO(body))

    mocker.patch.object(vlm_caption_provider, "urlopen", side_effect=reject_request)
    warning = mocker.patch.object(vlm_caption_provider.logger, "warning")

    assert provider.caption(np.zeros((2, 2, 3), dtype=np.uint8)) is None
    warning.assert_called_once_with(
        "VlmCaptionProvider: request failed: HTTP Error 400: Bad Request; "
        "response body: [redacted] [redacted image] [redacted image]"
    )


def test_connection_error_preserves_failure_behavior(mocker: MockerFixture) -> None:
    provider = VlmCaptionProvider("https://example.test", "test-model")
    mocker.patch.object(vlm_caption_provider, "urlopen", side_effect=URLError("offline"))
    warning = mocker.patch.object(vlm_caption_provider.logger, "warning")

    assert provider.caption(np.zeros((2, 2, 3), dtype=np.uint8)) is None
    warning.assert_called_once_with("VlmCaptionProvider: request failed: <urlopen error offline>")
