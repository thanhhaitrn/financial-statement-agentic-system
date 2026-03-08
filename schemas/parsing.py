import json
import re
from typing import Type, TypeVar
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)

def extract_json(text: str) -> str:
    """
    Best-effort: find the first {...} block.
    """
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    m = JSON_BLOCK.search(text)
    if not m:
        raise ValueError("No JSON object found in text.")
    return m.group(0)

def parse_model(text: str, model: Type[T]) -> T:
    raw = extract_json(text)
    data = json.loads(raw)
    return model.model_validate(data)