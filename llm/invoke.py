from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional

from pydantic import ValidationError

from llm.client import llm


STRUCTURED_OUTPUT_ERROR_MARKERS = (
    "unsupported function",
    "tool is not supported",
    "tools are not supported",
    "response_format is not supported",
    "json_schema is not supported",
)
STRUCTURED_PARSE_ERROR_MARKERS = (
    "validation error",
    "invalid json",
    "json_invalid",
    "failed to parse",
    "output parser",
    "did not return valid json",
)
VALID_STRUCTURED_OUTPUT_MODES = {"auto", "off", "on"}
USAGE_TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens")


def _normalize_structured_output_mode(value: str) -> str:
    mode = str(value or "").strip().lower()
    if mode in VALID_STRUCTURED_OUTPUT_MODES:
        return mode
    return "auto"


LLM_STRUCTURED_OUTPUT_MODE = _normalize_structured_output_mode(
    os.getenv("LLM_STRUCTURED_OUTPUT_MODE", "auto")
)
_STRUCTURED_OUTPUT_SUPPORTED: Optional[bool] = None


def looks_like_schema_support_error(error: Exception) -> bool:
    text = str(error or "").strip().lower()
    return any(marker in text for marker in STRUCTURED_OUTPUT_ERROR_MARKERS)


def looks_like_structured_parse_error(error: Exception) -> bool:
    if isinstance(error, ValidationError):
        return True

    text = str(error or "").strip().lower()
    return any(marker in text for marker in STRUCTURED_PARSE_ERROR_MARKERS)


def get_structured_output_mode() -> str:
    return LLM_STRUCTURED_OUTPUT_MODE


def _should_try_structured_output() -> bool:
    if LLM_STRUCTURED_OUTPUT_MODE == "on":
        return True
    if LLM_STRUCTURED_OUTPUT_MODE == "off":
        return False
    return _STRUCTURED_OUTPUT_SUPPORTED is not False


def _normalize_structured_schema(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return schema

    # LangChain requires top-level title/description for raw JSON Schema dicts.
    if "name" in schema and "parameters" in schema:
        return schema

    if not any(
        key in schema
        for key in ("type", "properties", "oneOf", "anyOf", "allOf", "$defs", "items")
    ):
        return schema

    normalized = dict(schema)
    normalized.setdefault("title", "StructuredResponse")
    normalized.setdefault("description", "Structured response schema.")
    return normalized


def _normalize_invoke_result(result: Any, *, mode: str) -> Dict[str, Any]:
    if isinstance(result, dict):
        payload = dict(result)
    else:
        payload = {
            "raw": result,
            "parsed": None,
            "parsing_error": None,
        }

    payload.setdefault("raw", None)
    payload.setdefault("parsed", None)
    payload.setdefault("parsing_error", None)
    payload["mode"] = mode
    payload["structured_output_mode"] = LLM_STRUCTURED_OUTPUT_MODE
    return payload


def _resolve_prompt_template(prompt_template: Any, payload: dict) -> Any:
    if callable(prompt_template) and not hasattr(prompt_template, "invoke"):
        return prompt_template(payload)
    return prompt_template


def extract_usage_metadata(raw: Any) -> Dict[str, Any]:
    if raw is None:
        return {}

    usage_metadata = getattr(raw, "usage_metadata", None) or {}
    response_metadata = getattr(raw, "response_metadata", None) or {}

    input_tokens = usage_metadata.get("input_tokens")
    output_tokens = usage_metadata.get("output_tokens")
    total_tokens = usage_metadata.get("total_tokens")

    if input_tokens is None:
        input_tokens = response_metadata.get("prompt_eval_count")
    if output_tokens is None:
        output_tokens = response_metadata.get("eval_count")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens

    usage: Dict[str, Any] = {}
    if input_tokens is not None:
        usage["input_tokens"] = int(input_tokens)
    if output_tokens is not None:
        usage["output_tokens"] = int(output_tokens)
    if total_tokens is not None:
        usage["total_tokens"] = int(total_tokens)

    model = str(response_metadata.get("model") or response_metadata.get("model_name") or "").strip()
    if model:
        usage["model"] = model

    return usage


def merge_usage_metadata(*items: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    model = ""

    for item in items:
        if not isinstance(item, dict):
            continue

        for field in USAGE_TOKEN_FIELDS:
            value = item.get(field)
            if value is None:
                continue
            merged[field] = int(merged.get(field, 0) or 0) + int(value)

        current_model = str(item.get("model", "") or "").strip()
        if current_model:
            model = current_model

    if model:
        merged["model"] = model

    return merged


def _invoke_plain_prompt(
    prompt_template: Any,
    payload: dict,
    *,
    normalized_schema: Any,
    plain_payload_factory: Optional[Callable[[dict], dict]],
    mode: Optional[str] = None,
    structured_error: Optional[Exception] = None,
) -> Dict[str, Any]:
    plain_payload = dict(payload)
    if plain_payload_factory is not None:
        plain_payload = plain_payload_factory(dict(payload))

    resolved_prompt_template = _resolve_prompt_template(prompt_template, plain_payload)
    chain = resolved_prompt_template | llm
    result = chain.invoke(plain_payload)
    fallback_mode = mode or ("plain_json" if normalized_schema is not None else "plain")
    normalized = _normalize_invoke_result(
        {
            "raw": result,
            "parsed": None,
            "parsing_error": None,
        },
        mode=fallback_mode,
    )
    if structured_error is not None:
        normalized["structured_error"] = str(structured_error)
    return normalized


def invoke_prompt(
    prompt_template: Any,
    payload: dict,
    *,
    structured_schema: Any = None,
    plain_payload_factory: Optional[Callable[[dict], dict]] = None,
    include_raw: bool = True,
) -> Dict[str, Any]:
    global _STRUCTURED_OUTPUT_SUPPORTED
    normalized_schema = _normalize_structured_schema(structured_schema)

    if normalized_schema is not None and _should_try_structured_output():
        try:
            resolved_prompt_template = _resolve_prompt_template(prompt_template, payload)
            chain = resolved_prompt_template | llm.with_structured_output(
                normalized_schema,
                include_raw=include_raw,
            )
            result = chain.invoke(payload)
        except Exception as exc:
            if LLM_STRUCTURED_OUTPUT_MODE == "auto" and looks_like_schema_support_error(exc):
                _STRUCTURED_OUTPUT_SUPPORTED = False
                return _invoke_plain_prompt(
                    prompt_template,
                    payload,
                    normalized_schema=normalized_schema,
                    plain_payload_factory=plain_payload_factory,
                    mode="plain_json_after_schema_support_fallback",
                    structured_error=exc,
                )
            if looks_like_structured_parse_error(exc):
                return _invoke_plain_prompt(
                    prompt_template,
                    payload,
                    normalized_schema=normalized_schema,
                    plain_payload_factory=plain_payload_factory,
                    mode="plain_json_after_structured_parse_error",
                    structured_error=exc,
                )
            raise
        else:
            if LLM_STRUCTURED_OUTPUT_MODE == "auto":
                _STRUCTURED_OUTPUT_SUPPORTED = True
            normalized = _normalize_invoke_result(result, mode="structured")
            if normalized.get("parsing_error") is not None:
                return _invoke_plain_prompt(
                    prompt_template,
                    payload,
                    normalized_schema=normalized_schema,
                    plain_payload_factory=plain_payload_factory,
                    mode="plain_json_after_structured_parsing_error",
                    structured_error=normalized.get("parsing_error"),
                )
            return normalized

    return _invoke_plain_prompt(
        prompt_template,
        payload,
        normalized_schema=normalized_schema,
        plain_payload_factory=plain_payload_factory,
    )
