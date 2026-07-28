"""Tests for project dependencies.

Verifies that ``pydantic`` resolves to a version matching the
constraint inherited from ``mcp`` (transitively).
"""

import unittest


class TestPydanticDependency(unittest.TestCase):
    """``pydantic`` must be importable as an explicit project dependency."""

    def test_pydantic_import_succeeds(self) -> None:
        """``import pydantic`` succeeds in the test venv."""
        import pydantic  # noqa: F811

        self.assertIsNotNone(pydantic)

    def test_pydantic_version_matches_mcp_constraint(self) -> None:
        """The installed ``pydantic`` version is compatible with ``mcp``."""
        import pydantic

        version = tuple(int(x) for x in pydantic.__version__.split("."))
        # mcp depends on pydantic >=2.0.0
        self.assertGreaterEqual(version, (2, 0, 0))
