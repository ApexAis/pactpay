"""The configured language model as one function: complete(system, user) -> text (or None when none is set up).

OpenAI-compatible endpoints (OpenAI, Groq, or a local open-weight model through Ollama) and Google Gemini. The model
only drafts terms and explains evaluations; it never decides a payment.
"""

from __future__ import annotations

from collections.abc import Callable

from app.config import Settings

Complete = Callable[[str, str], str]


def completer(settings: Settings, *, timeout_s: float = 120.0) -> tuple[Complete | None, str | None]:
    if settings.gemini_api_key.strip():
        model = settings.resolved_ai_model

        def gemini(system: str, user: str) -> str:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=settings.gemini_api_key)
            resp = client.models.generate_content(
                model=model, contents=user,
                config=types.GenerateContentConfig(system_instruction=system, temperature=0.0,
                                                   response_mime_type="application/json"))
            return resp.text or ""

        return gemini, f"google-genai ({model})"
    if settings.openai_api_key.strip():
        model = settings.resolved_ai_model

        def openai_compatible(system: str, user: str) -> str:
            from openai import OpenAI

            kwargs = {"api_key": settings.openai_api_key, "timeout": timeout_s, "max_retries": 1}
            if settings.openai_base_url:
                kwargs["base_url"] = settings.openai_base_url
            resp = OpenAI(**kwargs).chat.completions.create(
                model=model, temperature=0.0, response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
            return resp.choices[0].message.content or ""

        return openai_compatible, f"openai-compatible ({model})"
    return None, None
