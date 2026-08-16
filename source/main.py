import argparse
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from config import MiningConfig
from exceptions import RepositoryListError, RepositoryMinerError
from git_repository_cloner import GitRepositoryCloner, RetryingRepositoryCloner
from mining_service import RepositoryMiningService
from mining_state import SqliteMiningStateStore
from repository_list_reader import RepositoryListReader

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPOSITORIES_FILE = (
    PROJECT_ROOT / "input" / "65_cs_repositories.txt"
)
DEFAULT_PROJECTS_DIR = PROJECT_ROOT / "output" / "projects"
DEFAULT_STATE_FILE = PROJECT_ROOT / ".repository-miner" / "mining-state.sqlite3"


class _RunContextFilter(logging.Filter):
    """Attach one execution identifier to every emitted log record."""

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self._run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self._run_id
        return True


def resolve_project_path(path: Path) -> Path:
    """Resolve relative CLI paths from the repository root."""
    expanded_path = path.expanduser()
    if expanded_path.is_absolute():
        return expanded_path.resolve()
    return (PROJECT_ROOT / expanded_path).resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repository-miner",
        description=(
            "Clone GitHub and GitLab repositories listed in a text file into "
            "the projects directory."
        ),
    )
    parser.add_argument(
        "--repositories",
        type=Path,
        default=DEFAULT_REPOSITORIES_FILE,
        help="text file containing one GitHub or GitLab HTTPS URL per line",
    )
    parser.add_argument(
        "--projects-dir",
        type=Path,
        default=DEFAULT_PROJECTS_DIR,
        help=(
            "directory that will receive cloned repositories "
            "(default: output/projects)"
        ),
    )
    parser.add_argument(
        "--clone-depth",
        type=int,
        default=1,
        help="number of commits fetched by git clone (default: 1)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="maximum number of concurrent clones (default: 4)",
    )
    parser.add_argument(
        "--clone-timeout",
        type=float,
        default=1800.0,
        help="timeout in seconds for each clone attempt (default: 1800)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=2,
        help="retries for transient clone failures (default: 2)",
    )
    parser.add_argument(
        "--retry-base-delay",
        type=float,
        default=2.0,
        help="base retry delay in seconds (default: 2)",
    )
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=30.0,
        help="seconds between progress messages (default: 30)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="process at most this many pending repositories in one run",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help="SQLite file used for checkpoints and resume",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="optional rotating log file (default: console only)",
    )
    parser.add_argument(
        "--strict-input",
        action="store_true",
        help="abort the run when any input line is invalid",
    )
    parser.add_argument(
        "--c-only",
        action="store_true",
        help=(
            "materialize only tracked C source files instead of the complete "
            "working tree"
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="enable detailed logging",
    )
    return parser


def configure_logging(
    verbose: bool,
    log_file: Path | None = None,
    run_id: str = "startup",
) -> None:
    """Configure console logs and, when requested, one bounded log file."""
    console_level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [run=%(run_id)s] "
        "[%(threadName)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    context_filter = _RunContextFilter(run_id)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(context_filter)
    handlers: list[logging.Handler] = [console_handler]

    if log_file is None:
        logging.basicConfig(level=logging.DEBUG, handlers=handlers, force=True)
        return

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
    except OSError as exc:
        logging.basicConfig(level=logging.DEBUG, handlers=handlers, force=True)
        LOGGER.warning("Could not open log file %s: %s", log_file, exc)
    else:
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(context_filter)
        handlers.append(file_handler)
        logging.basicConfig(level=logging.DEBUG, handlers=handlers, force=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log_file = (
        resolve_project_path(args.log_file)
        if args.log_file is not None
        else None
    )
    run_id = uuid4().hex[:12]
    configure_logging(args.verbose, log_file, run_id)
    execution_started_at = time.monotonic()

    try:
        projects_dir = resolve_project_path(args.projects_dir)
        config = MiningConfig(
            repositories_file=resolve_project_path(args.repositories),
            projects_dir=projects_dir,
            clone_depth=args.clone_depth,
            state_file=resolve_project_path(args.state_file),
            workers=args.workers,
            clone_timeout_seconds=args.clone_timeout,
            max_retries=args.max_retries,
            retry_base_delay_seconds=args.retry_base_delay,
            progress_interval_seconds=args.progress_interval,
            batch_size=args.batch_size,
            c_only=args.c_only,
        )
        LOGGER.info(
            "Execution started: repositories_file=%s projects_dir=%s "
            "state_file=%s log_file=%s workers=%d clone_depth=%d "
            "timeout_seconds=%.1f max_retries=%d batch_size=%s "
            "checkout_mode=%s destination_layout=direct_children",
            config.repositories_file,
            config.projects_dir,
            config.state_file,
            log_file if log_file is not None else "console-only",
            config.workers,
            config.clone_depth,
            config.clone_timeout_seconds,
            config.max_retries,
            config.batch_size if config.batch_size is not None else "all",
            "c-only" if config.c_only else "full",
        )
        list_result = RepositoryListReader().scan(config.repositories_file)
        for issue in list_result.issues:
            LOGGER.warning(
                "Invalid input line %d: %s",
                issue.line_number,
                issue.message,
            )
        if args.strict_input and list_result.issues:
            raise RepositoryListError(
                f"repository list contains {len(list_result.issues)} invalid line(s)"
            )
        if not list_result.repositories:
            raise RepositoryListError(
                f"repository list '{config.repositories_file}' contains no valid URLs"
            )

        for group in list_result.possible_mirror_groups:
            LOGGER.warning(
                "Possible cross-host mirrors will both be mined: logical_name=%s "
                "repositories=%s",
                group.logical_name,
                ",".join(
                    repository.qualified_name for repository in group.repositories
                ),
            )

        LOGGER.info(
            "Loaded repository list: syntactically_valid=%d invalid=%d "
            "exact_duplicates=%d possible_mirror_groups=%d",
            len(list_result.repositories),
            len(list_result.issues),
            list_result.duplicate_count,
            len(list_result.possible_mirror_groups),
        )

        previous_projects_dir = PROJECT_ROOT / "projects"
        if (
            projects_dir == DEFAULT_PROJECTS_DIR
            and previous_projects_dir.is_dir()
            and any(previous_projects_dir.iterdir())
        ):
            LOGGER.warning(
                "Previous projects tree detected outside the configured output: "
                "path=%s. It will not be moved, deleted or reused; new clones "
                "will be written directly under %s",
                previous_projects_dir,
                projects_dir,
            )

        base_cloner = GitRepositoryCloner(
            clone_depth=config.clone_depth,
            timeout_seconds=config.clone_timeout_seconds,
            sparse_patterns=("*.c",) if config.c_only else None,
        )
        cloner = RetryingRepositoryCloner(
            base_cloner,
            max_retries=config.max_retries,
            base_delay_seconds=config.retry_base_delay_seconds,
        )
        if config.state_file is None:
            raise ValueError("state_file is required")
        with SqliteMiningStateStore(config.state_file) as state_store:
            service = RepositoryMiningService(
                cloner,
                max_workers=config.workers,
                state_store=state_store,
                progress_interval_seconds=config.progress_interval_seconds,
                batch_size=config.batch_size,
            )
            result = service.mine(list_result.repositories, config.projects_dir)
    except KeyboardInterrupt:
        LOGGER.warning(
            "Execution interrupted by user; in-flight repositories will be "
            "recovered from the checkpoint on the next run; "
            "duration_seconds=%.1f",
            time.monotonic() - execution_started_at,
        )
        return 130
    except (RepositoryMinerError, ValueError) as exc:
        LOGGER.error(
            "Execution failed: duration_seconds=%.1f error=%s",
            time.monotonic() - execution_started_at,
            exc,
        )
        return 2

    exit_code = 0 if result.succeeded else 1
    status = "success" if result.succeeded else "partial_failure"
    LOGGER.info(
        "Finished: status=%s cloned=%d failed=%d already_completed=%d "
        "remaining=%d total=%d duration_seconds=%.1f exit_code=%d",
        status,
        len(result.cloned),
        len(result.failures),
        result.skipped_count,
        result.remaining_count,
        result.total,
        time.monotonic() - execution_started_at,
        exit_code,
    )
    if result.failures:
        failed_names = ", ".join(
            failure.repository.qualified_name for failure in result.failures[:10]
        )
        suffix = "" if len(result.failures) <= 10 else ", ..."
        LOGGER.error(
            "Repositories requiring attention: count=%d repositories=%s%s. "
            "Correct inaccessible URLs or credentials and run again",
            len(result.failures),
            failed_names,
            suffix,
        )
    if result.remaining_count:
        LOGGER.info(
            "Batch completed successfully; run the command again to process "
            "%d remaining repositories",
            result.remaining_count,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
