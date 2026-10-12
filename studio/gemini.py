import json
import logging

from django.conf import settings
from pydantic import ValidationError


logger = logging.getLogger(__name__)


class GenerationError(Exception):
    def __init__(self, message, *, transient=False, blocked=False, invalid=False):
        super().__init__(message)
        self.transient = transient
        self.blocked = blocked
        self.invalid = invalid


def _usage(response, input_tokens):
    metadata = getattr(response, "usage_metadata", None)
    return {
        "input_tokens": getattr(metadata, "prompt_token_count", None) or input_tokens or 0,
        "output_tokens": getattr(metadata, "candidates_token_count", None) or 0,
        "total_tokens": getattr(metadata, "total_token_count", None) or 0,
    }


class GeminiGateway:
    def __init__(self):
        if not settings.GEMINI_API_KEY:
            raise GenerationError("GEMINI_API_KEY is missing. Add it to the server environment.", blocked=True)
        from google import genai

        self.client = genai.Client(api_key=settings.GEMINI_API_KEY)
        self.model = settings.GEMINI_MODEL

    def _call(self, prompt, schema=None):
        from google.genai import types

        try:
            token_result = self.client.models.count_tokens(model=self.model, contents=prompt)
            input_tokens = getattr(token_result, "total_tokens", 0)
            config = None
            if schema is not None:
                config = types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema)
            response = self.client.models.generate_content(model=self.model, contents=prompt, config=config)
            feedback = getattr(response, "prompt_feedback", None)
            if getattr(feedback, "block_reason", None):
                raise GenerationError("Gemini blocked the prompt. Edit the source text and retry.", blocked=True)
            candidates = getattr(response, "candidates", None) or []
            if candidates:
                reason = str(getattr(candidates[0], "finish_reason", ""))
                if "SAFETY" in reason or "RECITATION" in reason:
                    raise GenerationError("Gemini blocked the output. Edit the source text and retry.", blocked=True)
            if not getattr(response, "text", None) and getattr(response, "parsed", None) is None:
                raise GenerationError("Gemini returned no text.", invalid=True)
            return response, _usage(response, input_tokens)
        except GenerationError:
            raise
        except Exception as exc:
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            try:
                code = int(code)
            except (TypeError, ValueError):
                code = None
            transient = code in {408, 429, 500, 502, 503, 504} or isinstance(exc, (TimeoutError, ConnectionError))
            logger.warning("Gemini request failed: %s", type(exc).__name__)
            raise GenerationError("Gemini is temporarily unavailable." if transient else "Gemini request failed. Check the API key and model settings.", transient=transient) from exc

    def generate_structured(self, prompt, schema):
        response, usage = self._call(prompt, schema)
        try:
            parsed = getattr(response, "parsed", None)
            value = schema.model_validate(parsed) if parsed is not None else schema.model_validate_json(response.text)
            return value.model_dump(), usage
        except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise GenerationError("Gemini returned an invalid structured response.", invalid=True) from exc

    def generate_text(self, prompt):
        response, usage = self._call(prompt)
        value = (response.text or "").strip()
        if not value:
            raise GenerationError("Gemini returned an empty scene.", invalid=True)
        return value, usage
