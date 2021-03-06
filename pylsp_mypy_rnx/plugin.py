import atexit
import collections
import logging
import os
import os.path
import re
from pathlib import Path
from tempfile import NamedTemporaryFile, gettempdir
from typing import IO, Any, Dict, List, Optional

import mypy
from mypy import api as mypy_api
from mypy.defaults import CONFIG_FILES as MYPY_CONFIG_FILES
from pylsp import hookimpl
from pylsp.config.config import Config
from pylsp.workspace import Document, Workspace

line_pattern = re.compile(r"((?:^[a-z]:)?[^:]+):(?:(\d+):)?(?:(\d+):)? (\w+): (.*)")

logger = logging.getLogger("pylsp.plugins.mypy_rnx")
logger.info(f"Using mypy located at: {mypy.__file__}")


def _get_dmypy_status_filepath() -> Path:
    tmpdir = Path(gettempdir())
    unique_mypy_path_per_python_exec = Path(mypy.__file__).parent.relative_to(
        Path.home()
    )

    dmypy_status_dirpath = tmpdir / unique_mypy_path_per_python_exec
    if not dmypy_status_dirpath.exists():
        dmypy_status_dirpath.mkdir(parents=True)

    return dmypy_status_dirpath / "dmypy-status.json"


class State:
    initialized: bool = False
    mypy_config_file: Optional[str] = None

    livemode_tmpfile: IO[str] = NamedTemporaryFile("w", delete=False)

    # In non-live-mode the file contents aren't updated.
    # Returning an empty diagnostic clears the diagnostic result,
    # so store a cache of last diagnostics for each file a-la the pylint plugin,
    # so we can return some potentially-stale diagnostics.
    # https://github.com/python-lsp/python-lsp-server/blob/v1.0.1/pylsp/plugins/pylint_lint.py#L55-L62
    last_diagnostics: Dict[str, List[Any]] = collections.defaultdict(list)

    # dmypy stuff (keep one state file per mypy executable):
    dmypy_daemon_status: Optional[int] = None
    dmypy_status_file: str = _get_dmypy_status_filepath().as_posix()


def parse_line(
    line: str, document: Optional[Document] = None
) -> Optional[Dict[str, Any]]:
    result = line_pattern.match(line)
    logger.info(line)
    if not result:
        return None

    file_path, linenoStr, offsetStr, severity, msg = result.groups()

    if file_path != "<string>":  # live mode
        # results from other files can be included, but we cannot return them.
        if document and document.path and not document.path.endswith(file_path):
            msg = f"discarding result for {file_path} against {document.path}"
            logger.warning(msg)
            return None

    lineno = int(linenoStr or 1) - 1  # 0-based line number
    offset = int(offsetStr or 1) - 1  # 0-based offset
    errno = 1 if severity == "error" else 2

    range_diag = {
        "start": {"line": lineno, "character": offset},
        # There may be a better solution, but mypy does not provide end
        "end": {"line": lineno, "character": offset + 1},
    }

    diag: Dict[str, Any] = {
        "source": "mypy",
        "range": range_diag,
        "message": msg,
        "severity": errno,
    }

    if document:
        # although mypy does not provide the end of the affected range, we
        # can make a good guess by highlighting the word that Mypy flagged
        word = document.word_at_position(diag["range"]["start"])
        if word:
            diag["range"]["end"]["character"] = diag["range"]["start"][
                "character"
            ] + len(word)

    return diag


def _ensure_finding_config_file(config: Config) -> None:
    # Check for mypy config file to be used
    if State.mypy_config_file:
        return

    workspace = config._root_path
    logger.info(f"Searching for mypy config file from {workspace}")
    for filepath in MYPY_CONFIG_FILES:
        location = os.path.join(workspace, filepath)
        if os.path.isfile(location):
            State.mypy_config_file = location
            logger.info(f"Found mypy config file at {State.mypy_config_file}")
            break


@hookimpl
def pylsp_settings(config: Config) -> Dict[str, Any]:
    _ensure_finding_config_file(config)
    return {
        "plugins": {
            "pylsp_mypy_rnx": {
                "enabled": True,
                "live_mode": True,
                "args": [],
                "dmypy": False,
                "daemon_args": {},
            }
        }
    }


def parse_run(
    document: Document, report: str, errors: str, exit_status: int
) -> List[Dict[str, Any]]:
    logger.debug(f"report:\n{report}")
    if errors:
        logger.warning(f"errors:\n{errors}")
        logger.warning(f"exit_status: {exit_status}")

    last_diags = []
    for line in report.splitlines():
        logger.debug("parsing: line = %r", line)
        diag = parse_line(line, document)
        if diag:
            last_diags.append(diag)

    logger.info("pylsp_mypy_rnx len(diagnostics) = %s", len(last_diags))

    State.last_diagnostics[document.path] = last_diags
    return last_diags


def _to_dmypy_cmd(cmd: List[str]) -> str:
    return f"'dmypy {' '.join(cmd)}'"


@hookimpl
def pylsp_lint(
    workspace: Workspace, document: Document, is_saved: bool
) -> List[Dict[str, Any]]:
    config = workspace._config
    settings = config.plugin_settings("pylsp_mypy_rnx", document_path=document.path)

    if not State.initialized:
        logger.info(f"lint settings: {settings}")
        logger.info(f"document.path: {document.path}")
        State.initialized = True

    live_mode = settings["live_mode"]
    dmypy = settings["dmypy"]

    if dmypy and live_mode:
        # dmypy can only be efficiently run on files that have been saved, see:
        # https://github.com/python/mypy/issues/9309
        logger.warning("live_mode is not supported with dmypy, disabling")
        live_mode = False

    args = settings["args"]
    args = [
        *args,
        "--show-column-numbers",
        "--follow-imports",
        "normal",
        "--incremental",
    ]
    if State.mypy_config_file:
        args.extend(["--config-file", State.mypy_config_file])

    if not is_saved:
        if not live_mode:  # Early return in case of not saved and not live-mode
            last_diags = State.last_diagnostics.get(document.path, [])
            msg = f"non-live, returning cached diagnostics ({len(last_diags)})"
            logger.debug(msg)
            return last_diags

        else:
            # use shadow file in live-mode
            logger.info(f"live_mode with tmpfile: {State.livemode_tmpfile.name}")
            with open(State.livemode_tmpfile.name, "w") as f:
                f.write(document.source)
            args.extend(["--shadow-file", document.path, State.livemode_tmpfile.name])

    if not dmypy:
        cmd = [*args, document.path]
        logger.info(f"executing: 'mypy {' '.join(cmd)}")
        report, errors, exit_status = mypy_api.run(cmd)
        return parse_run(document, report, errors, exit_status)

    daemon_args = ["--status-file", State.dmypy_status_file]

    if State.dmypy_daemon_status is None:
        msg = f"dmypy - status file can be found at {State.dmypy_status_file}"
        logger.info(msg)

        # First check if no daemon already running
        cmd = [*daemon_args, "status"]
        logger.info(f"call dmypy status: {_to_dmypy_cmd(cmd)}")

        result, errors, State.dmypy_daemon_status = mypy_api.run_dmypy(cmd)
        if State.dmypy_daemon_status != 0:  # not yet up and running
            logger.debug(errors)

            daemon_args_start = [
                *daemon_args,
                "start",
                *(settings.get("daemon_args", {}).get("start", [])),
            ]
            cmd = [*daemon_args_start, "--", *args, document.path]
            logger.info(f"call dmypy start: {_to_dmypy_cmd(cmd)}")

            _, errors, State.dmypy_daemon_status = mypy_api.run_dmypy(cmd)
            if State.dmypy_daemon_status != 0:
                logger.warning(errors)

    logger.debug(f"current dmypy daemon status: {State.dmypy_daemon_status}")

    daemon_args_check = [
        *daemon_args,
        "check",
        *(settings.get("daemon_args", {}).get("check", [])),
    ]
    cmd = [*daemon_args_check, document.path]
    logger.info(f"call dmypy check: {_to_dmypy_cmd(cmd)}")
    report, errors, exit_status = mypy_api.run_dmypy(cmd)

    return parse_run(document, report, errors, exit_status)


@atexit.register
def close() -> None:
    if State.dmypy_daemon_status is not None:
        daemon_args = ["--status-file", State.dmypy_status_file]
        cmd = [*daemon_args, "kill"]
        logger.info(f"call dmypy kill: {_to_dmypy_cmd(cmd)}")
        mypy_api.run_dmypy(cmd)

    if State.livemode_tmpfile:
        os.unlink(State.livemode_tmpfile.name)  # cleanup tmpfile
