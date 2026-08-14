"""Tests for ``wywy_docs.models`` — ``DocFrontmatter`` model and ``Section`` type.

Verifies:
- ``DocFrontmatter`` and ``Section`` are importable from ``wywy_docs``
  and from ``wywy_docs.models``.
- ``DocFrontmatter`` accepts empty frontmatter and arbitrary extra fields.
- ``DocFrontmatter`` rejects ``published`` and ``last_updated`` keys
  via ``pydantic.ValidationError``.
"""

from __future__ import annotations

import unittest
from typing import get_args

import pydantic
import pytest

from wywy_docs import DocFrontmatter, Section


class TestDocFrontmatter(unittest.TestCase):
    """``DocFrontmatter`` validation behaviour."""

    def test_empty_frontmatter_accepted(self) -> None:
        """``DocFrontmatter()`` accepts empty frontmatter dict."""
        fm = DocFrontmatter()
        assert fm.model_dump() == {}

    def test_empty_dict_accepted(self) -> None:
        """``DocFrontmatter({})`` accepts empty dict."""
        fm = DocFrontmatter({})
        assert fm.model_dump() == {}

    def test_extra_fields_preserved(self) -> None:
        """Arbitrary extra frontmatter fields are preserved."""
        fm = DocFrontmatter({"title": "Hello", "key": "value", "count": 3})
        dumped = fm.model_dump()
        assert dumped["title"] == "Hello"
        assert dumped["key"] == "value"
        assert dumped["count"] == 3  # noqa: PLR2004 - fixture value above

    def test_published_rejected(self) -> None:
        """``published`` key raises ``pydantic.ValidationError``."""
        with pytest.raises(pydantic.ValidationError):
            DocFrontmatter({"published": "2026-01-01"})

    def test_last_updated_rejected(self) -> None:
        """``last_updated`` key raises ``pydantic.ValidationError``."""
        with pytest.raises(pydantic.ValidationError):
            DocFrontmatter({"last_updated": "2026-01-01"})


class TestSectionType(unittest.TestCase):
    """``Section`` is a ``Literal["docs", "internal"]``."""

    def test_section_is_importable(self) -> None:
        """``Section`` is importable from ``wywy_docs``."""
        from wywy_docs import Section

        assert Section is not None

    def test_section_values(self) -> None:
        """``Section`` accepts only ``"docs"`` and ``"internal"``."""
        args = get_args(Section)
        assert set(args) == {"docs", "internal"}
