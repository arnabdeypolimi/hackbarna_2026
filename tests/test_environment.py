"""Proves the dependency combination that spec §13 flags as risky."""
import sys
from importlib.metadata import version


def test_python_is_at_least_311():
    assert sys.version_info >= (3, 11)


def test_pipecat_is_modern_line():
    major = int(version("pipecat-ai").split(".")[0])
    assert major >= 1, "pipecat-ai must be the 1.x line, not legacy 0.0.x"


def test_anam_plugin_is_the_prerelease_not_legacy_stable():
    # 0.1.0 (stable) depends on pipecat-ai>=0.0.103 and will not work here.
    assert version("pipecat-anam") == "0.2.0a6"


def test_plugins_import():
    from pipecat_anam import AnamVideoService
    from pipecat_slng import SlngSTTService, SlngTTSService

    assert AnamVideoService is not None
    assert SlngSTTService is not None
    assert SlngTTSService is not None
