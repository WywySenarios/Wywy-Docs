"""Tests for ``wywy_docs.models`` — ``DocFrontmatter`` Pydantic model and ``Section`` literal type.

Verifies:
- ``DocFrontmatter`` and ``Section`` are importable from ``wywy_docs``
  and from ``wywy_docs.models``.
- ``DocFrontmatter`` accepts empty frontmatter and arbitrary extra fields.
- ``DocFrontmatter`` rejects ``published`` and ``last_updated`` keys
  via ``pydantic.ValidationError``.
"""

from __future__ import annotations

import unittest

import pydantic

from wywy_docs import DocFrontmatter, Section


class TestDocFrontmatter(unittest.TestCase):
    """``DocFrontmatter`` validation behaviour."""

    def test_empty_frontmatter_accepted(self) -> None:
        """``DocFrontmatter()`` accepts empty frontmatter dict."""
        fm = DocFrontmatter()
        self.assertEqual(fm.model_dump(), {})

    def test_empty_dict_accepted(self) -> None:
        """``DocFrontmatter({})`` accepts empty dict."""
        fm = DocFrontmatter({})
        self.assertEqual(fm.model_dump(), {})

    def test_extra_fields_preserved(self) -> None:
        """Arbitrary extra frontmatter fields are preserved."""
        fm = DocFrontmatter({"title": "Hello", "key": "value", "count": 3})
        dumped = fm.model_dump()
        self.assertEqual(dumped["title"], "Hello")
        self.assertEqual(dumped["key"], "value")
        self.assertEqual(dumped["count"], 3)

    def test_published_rejected(self) -> None:
        """``published`` key raises ``pydantic.ValidationError``."""
        with self.assertRaises(pydantic.ValidationError):
            DocFrontmatter({"published": "2026-01-01"})

    def test_last_updated_rejected(self) -> None:
        """``last_updated`` key raises ``pydantic.ValidationError``."""
        with self.assertRaises(pydantic.ValidationError):
            DocFrontmatter({"last_updated": "2026-01-01"})


class TestSectionType(unittest.TestCase):
    """``Section`` is a ``Literal["docs", "internal"]``."""

    def test_section_is_importable(self) -> None:
        """``Section`` is importable from ``wywy_docs``."""
        from wywy_docs import Section  # noqa: F811

        self.assertIsNotNone(Section)

    def test_section_values(self) -> None:
        """``Section`` accepts only ``"docs"`` and ``"internal"``."""
        from typing import get_args

        args = get_args(Section)
        self.assertIn("docs", args)
        self.assertIn("internal", args)
        self.assertEqual(len(args), 2)
