"""Deterministic LLM for local development without vendor keys (LLM_PROVIDER=fake).

Builds the smallest valid instance of whatever schema is requested by walking its fields, so it keeps working as
schemas evolve.
"""

from __future__ import annotations

import types
from enum import Enum
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel


def _sample(annotation: Any, field_name: str) -> Any:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, types.UnionType):
        return None if type(None) in args else _sample(args[0], field_name)
    if origin is Literal:
        return args[0]
    if origin in (list, tuple, set, frozenset):
        return []
    if origin is dict:
        return {}
    if isinstance(annotation, type):
        if issubclass(annotation, BaseModel):
            return sample_instance(annotation)
        if issubclass(annotation, Enum):
            return next(iter(annotation))
        if issubclass(annotation, bool):
            return False
        if issubclass(annotation, int):
            return 0
        if issubclass(annotation, float):
            return 0.0
        if issubclass(annotation, str):
            return f"Stub {field_name.replace('_', ' ')}"
    return None


def sample_instance[T: BaseModel](schema: type[T]) -> T:
    values = {
        name: _sample(field.annotation, name) for name, field in schema.model_fields.items() if field.is_required()
    }
    return schema.model_validate(values)


class StubLanguageModel:
    name = "stub-llm"

    async def generate[T: BaseModel](self, *, system: str, prompt: str, schema: type[T], max_tokens: int = 8000) -> T:
        return sample_instance(schema)
