"""Pydantic models and type aliases for Wywy-Docs frontmatter validation."""

from __future__ import annotations

from typing import Literal, cast

from pydantic import BaseModel, model_validator

Section = Literal["docs", "internal"]


class DocFrontmatter(BaseModel):
    """Frontmatter metadata for a documentation page.

    Accepts arbitrary extra fields but rejects ``published`` and
    ``last_updated`` (those are auto-populated server-side).
    """

    model_config = {"extra": "allow"}  # type: ignore[assignment]

    def __init__(
        self,
        _data: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        """Accept a positional dict as an alternative to ``**kwargs``."""
        if _data is not None:
            kwargs = {**_data, **kwargs}
        super().__init__(**kwargs)

    @model_validator(mode="before")
    @classmethod
    def reject_reserved_fields(cls, data: object) -> object:
        """Raise ``ValueError`` if ``published`` or ``last_updated`` are present."""
        if not isinstance(data, dict):
            return data
        data_dict = cast("dict[str, object]", data)
        for key in ("published", "last_updated"):
            if key in data_dict:
                msg = (
                    f"'{key}' is a reserved frontmatter key and cannot be set manually"
                )
                raise ValueError(msg)
        return data_dict
