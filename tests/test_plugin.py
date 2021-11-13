from unittest.mock import Mock

import pytest
from pylsp import uris
from pylsp.config.config import Config
from pylsp.workspace import Document, Workspace

from pylsp_mypy_rnx import plugin

DOC_URI = __file__
DOC_TYPE_ERR = """{}.append(3)
"""
TYPE_ERR_MSG = '"Dict[<nothing>, <nothing>]" has no attribute "append"'

TEST_LINE = 'test_plugin.py:279:8: error: "Request" has no attribute "id"'
TEST_LINE_WITHOUT_COL = "test_plugin.py:279: " 'error: "Request" has no attribute "id"'
TEST_LINE_WITHOUT_LINE = "test_plugin.py: " 'error: "Request" has no attribute "id"'


@pytest.fixture
def workspace(tmpdir):
    """Return a workspace."""
    ws = Workspace(uris.from_fs_path(str(tmpdir)), Mock())
    ws._config = Config(ws.root_uri, {}, 0, {})
    return ws


class FakeConfig:
    def __init__(self):
        self._root_path = "C:"

    def plugin_settings(self, plugin, document_path=None):
        return {}


def test_settings():
    config = FakeConfig()
    settings = plugin.pylsp_settings(config)
    assert settings == {
        "plugins": {
            "pylsp_mypy_rnx": {
                "args": [],
                "daemon_args": {},
                "dmypy": False,
                "enabled": True,
                "live_mode": True,
            }
        }
    }


def test_plugin(workspace):
    doc = Document(DOC_URI, workspace, DOC_TYPE_ERR)
    plugin.pylsp_settings(workspace._config)
    diags = plugin.pylsp_lint(workspace, doc, is_saved=False)

    assert len(diags) == 1
    diag = diags[0]
    assert diag["message"] == TYPE_ERR_MSG
    assert diag["range"]["start"] == {"line": 0, "character": 0}
    assert diag["range"]["end"] == {"line": 0, "character": 1}


def test_parse_full_line(workspace):
    doc = Document(DOC_URI, workspace)
    diag = plugin.parse_line(TEST_LINE, doc)
    assert diag["message"] == '"Request" has no attribute "id"'  # type: ignore
    assert diag["range"]["start"] == {"line": 278, "character": 7}  # type: ignore
    assert diag["range"]["end"] == {"line": 278, "character": 8}  # type: ignore


def test_parse_line_without_col(workspace):
    doc = Document(DOC_URI, workspace)
    diag = plugin.parse_line(TEST_LINE_WITHOUT_COL, doc)
    assert diag["message"] == '"Request" has no attribute "id"'  # type: ignore
    assert diag["range"]["start"] == {"line": 278, "character": 0}  # type: ignore
    assert diag["range"]["end"] == {"line": 278, "character": 1}  # type: ignore
