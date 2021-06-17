import atexit
import collections
import logging
import os
import os.path
import re
from tempfile import NamedTemporaryFile
from typing import Any, Dict, List, Optional, IO

from mypy import api as mypy_api
# from mypy.defaults import CONFIG_FILES
from pylsp import hookimpl
from pylsp.config.config import Config
from pylsp.workspace import Document, Workspace

line_pattern: str = r"((?:^[a-z]:)?[^:]+):(?:(\d+):)?(?:(\d+):)? (\w+): (.*)"

logger = logging.getLogger(__name__)

mypy_config_file: Optional[str] = None

map_document_tmpfile: Dict[str, IO[str]] = collections.defaultdict(
    lambda: NamedTemporaryFile("w", delete=False)
)

# In non-live-mode the file contents aren't updated.
# Returning an empty diagnostic clears the diagnostic result,
# so store a cache of last diagnostics for each file a-la the pylint plugin,
# so we can return some potentially-stale diagnostics.
# https://github.com/python-lsp/python-lsp-server/blob/v1.0.1/pylsp/plugins/pylint_lint.py#L55-L62
last_diagnostics: Dict[str, List] = collections.defaultdict(list)


def find_config_file(path: str, name: str) -> Optional[str]:
    while True:
        p = f"{path}/{name}"
        if os.path.isfile(p):
            return p
        else:
            loc = path.rfind("/")
            if loc == -1:
                return None
            path = path[:loc]


def parse_line(line: str, document: Optional[Document] = None) -> Optional[Dict[str, Any]]:
    """
    Return a language-server diagnostic from a line of the Mypy error report.

    optionally, use the whole document to provide more context on it.


    Parameters
    ----------
    line : str
        Line of mypy output to be analysed.
    document : Optional[Document], optional
        Document in wich the line is found. The default is None.

    Returns
    -------
    Optional[Dict[str, Any]]
        The dict with the lint data.

    """
    result = re.match(line_pattern, line)
    logger.info(line)
    if result:
        file_path, linenoStr, offsetStr, severity, msg = result.groups()

        if file_path != "<string>":  # live mode
            # results from other files can be included, but we cannot return
            # them.
            if document and document.path and not document.path.endswith(file_path):
                logger.warning("discarding result for %s against %s", file_path, document.path)
                return None

        lineno = int(linenoStr or 1) - 1  # 0-based line number
        offset = int(offsetStr or 1) - 1  # 0-based offset
        errno = 2
        if severity == "error":
            errno = 1
        diag: Dict[str, Any] = {
            "source": "mypy",
            "range": {
                "start": {"line": lineno, "character": offset},
                # There may be a better solution, but mypy does not provide end
                "end": {"line": lineno, "character": offset + 1},
            },
            "message": msg,
            "severity": errno,
        }
        if document:
            # although mypy does not provide the end of the affected range, we
            # can make a good guess by highlighting the word that Mypy flagged
            word = document.word_at_position(diag["range"]["start"])
            if word:
                diag["range"]["end"]["character"] = diag["range"]["start"]["character"] + len(word)

        return diag
    return None


@hookimpl
def pylsp_settings(config):
    # Check for mypy config file to be used
    logger.info("PASSING BY SETTINGS")
    global mypy_config_file
    if not mypy_config_file:
        workspace = config._root_path
        logger.info(f"Searching for mypy config file from {workspace}")
        for filepath in ["mypy.ini", ".mypy.ini", "setup.cfg", "pyproject.toml"]:
            mypy_config_file = find_config_file(workspace, filepath)
            if mypy_config_file:
                logger.info(f"Found mypy config file at {mypy_config_file}")
                break

    return {
        "plugins": {
            "pylsp_mypy_rnx": {"enabled": True, "live_mode": True, "dmypy": False, "args": []}
        }
    }


@hookimpl
def pylsp_lint(workspace: Workspace, document: Document) -> List[Dict[str, Any]]:
    logger.info("PASSING BY LINT")
    config = workspace._config
    settings = config.plugin_settings("pylsp_mypy_rnx", document_path=document.path)
    logger.info(f"lint settings: {settings}")
    logger.info(f"document.path = {document.path}")

    live_mode = settings["live_mode"]
    dmypy = settings["dmypy"]

    if dmypy and live_mode:
        # dmypy can only be efficiently run on files that have been saved, see:
        # https://github.com/python/mypy/issues/9309
        logger.warning("live_mode is not supported with dmypy, disabling")
        live_mode = False

    args = settings["args"]
    args = [*args, "--show-column-numbers"]

    if live_mode:
        tmpfile = map_document_tmpfile[document.source]
        logger.info(f"live_mode for document.path = {document.path} with tmpfile = {tmpfile.name}")
        with open(tmpfile.name, "w") as f:
            f.write(document.source)

        args.extend(["--shadow-file", document.path, tmpfile.name])

    elif document.path in last_diagnostics:
        # On-launch the document isn't marked as saved, so fall through and run
        # the diagnostics anyway even if the file contents may be out of date.
        last_diags = last_diagnostics[document.path]
        logger.info(f"non-live, returning cached diagnostics len(cached) = {last_diags}")
        return last_diags

    if mypy_config_file:
        args.append("--config-file")
        args.append(mypy_config_file)

    args.append(document.path)

    if not dmypy:
        args.extend(["--incremental", "--follow-imports", "silent"])
        logger.info(f"executing mypy args = {args}")

        report, errors, _ = mypy_api.run(args)
    else:
        args = ["run", "--"] + args
        logger.info(f"executing dmypy args = {args}")

        report, errors, _ = mypy_api.run_dmypy(args)

    logger.debug("report:\n%s", report)
    logger.debug("errors:\n%s", errors)

    last_diags = []
    for line in report.splitlines():
        logger.debug("parsing: line = %r", line)
        diag = parse_line(line, document)
        if diag:
            last_diags.append(diag)

    logger.info("pylsp_mypy_rnx len(diagnostics) = %s", len(last_diags))

    last_diagnostics[document.path] = last_diags
    return last_diags


@atexit.register
def close() -> None:
    """Deltes the tempFile should it exist."""
    for _, tmpfile in map_document_tmpfile.items():
        os.unlink(tmpfile.name)
