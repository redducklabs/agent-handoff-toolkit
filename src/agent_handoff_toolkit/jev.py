"""Optional single-request TypeSafe adapter; no lifecycle authority."""

import json
import http.client
import math
from fractions import Fraction
import socket
import urllib.error
import urllib.request

from .review_config import (
    MODEL_PATTERN,
    ReviewInputError,
    ReviewUnavailable,
    strict_object,
)
from .review_rubrics import VERDICTS

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MAX_RESPONSE_BYTES = 256 * 1024
PROBABILITY_TOLERANCE = 1e-6  # Toolkit policy, not a vendor guarantee.
# Two-decimal probability rounding contributes 1.5 * 0.005 to the
# three-option formula; confidence rounding contributes another 0.005.
CONFIDENCE_ROUNDING_TOLERANCE = 0.0125


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_json(payload: dict, credential: str, timeout: float) -> bytes:
    if not credential or any(not 0x21 <= ord(char) <= 0x7E for char in credential):
        raise ReviewUnavailable("credential-format")
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + credential,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=timeout
        ) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        code = (
            "redirect"
            if 300 <= error.code < 400
            else "authentication"
            if error.code in (401, 403)
            else "rate-limit"
            if error.code == 429
            else "server"
            if error.code >= 500
            else "http"
        )
        error.close()
        raise ReviewUnavailable(code) from None
    except (TimeoutError, socket.timeout):
        raise ReviewUnavailable("timeout") from None
    except urllib.error.URLError as error:
        raise ReviewUnavailable(
            "timeout" if isinstance(error.reason, TimeoutError) else "connection"
        ) from None
    except (OSError, http.client.HTTPException):
        raise ReviewUnavailable("connection") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ReviewUnavailable("response-size")
    try:
        raw.decode("utf-8")
    except UnicodeError:
        raise ReviewUnavailable("response-json") from None
    return raw


def validate_response(raw: bytes, questions: dict) -> dict:
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ReviewUnavailable("response-size")
    try:
        data = strict_object(raw)
    except ReviewInputError:
        raise ReviewUnavailable("response-json") from None
    model = data.get("model")
    answers = data.get("answers")
    if (
        not isinstance(model, str)
        or not MODEL_PATTERN.fullmatch(model)
        or not isinstance(answers, dict)
        or set(answers) != set(questions)
        or not 1 <= len(questions) <= 21
    ):
        raise ReviewUnavailable("response-schema")

    def probability(value):
        return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)

    reduced = {}
    for key, answer in answers.items():
        if not isinstance(answer, dict):
            raise ReviewUnavailable("response-schema")
        choice = answer.get("choice")
        probabilities = answer.get("probabilities")
        confidence = answer.get("confidence")
        if (
            answer.get("type") != "choice"
            or choice not in VERDICTS
            or not isinstance(probabilities, dict)
            or set(probabilities) != set(VERDICTS)
            or not all(probability(value) for value in probabilities.values())
            or abs(sum(probabilities.values()) - 1) > PROBABILITY_TOLERANCE
            or probabilities[choice] < max(probabilities.values())
            or not probability(confidence)
        ):
            raise ReviewUnavailable("response-schema")
        # Evaluate the decimal wire probability exactly before converting back
        # to float, so e.g. 0.94 produces 0.91 at a configured 0.91 floor.
        derived_confidence = max(
            0.0, float((3 * Fraction(str(max(probabilities.values()))) - 1) / 2)
        )
        if (
            abs(confidence - derived_confidence)
            > CONFIDENCE_ROUNDING_TOLERANCE + PROBABILITY_TOLERANCE
        ):
            raise ReviewUnavailable("response-schema")
        reduced[key] = {
            "type": "choice",
            "choice": choice,
            "probabilities": probabilities,
            "confidence": (
                derived_confidence
                if confidence - derived_confidence > PROBABILITY_TOLERANCE
                else confidence
            ),
        }
    return {"model": model, "answers": reduced}
