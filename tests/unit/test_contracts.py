"""Skeleton sanity: models import, schema applies to a scratch DB when available."""

from common import models


def test_module_snapshot_requires_four_tiles():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        models.ModuleSnapshot(name="x", title="X", crumb="", source="", snapshot_at="2026-09-04T00:00:00Z", tiles=[])


def test_approval_exec_server_allowlist():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        models.Exec(server="Brex", tool="pay", input={})
