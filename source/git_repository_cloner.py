import logging
import os
import random
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable
from uuid import uuid4

from exceptions import (
    CloneError,
    CloneTimeoutError,
    DestinationAlreadyExistsError,
    GitNotAvailableError,
    RepositoryNotFoundError,
    TransientCloneError,
)
from ports import RepositoryCloner
from repository import GitRepository

LOGGER = logging.getLogger(__name__)

_TRANSIENT_ERROR_MARKERS = (
    "connection reset",
    "connection timed out",
    "could not resolve host",
    "early eof",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "remote end hung up unexpectedly",
    "the requested url returned error: 429",
    "the requested url returned error: 500",
    "the requested url returned error: 502",
    "the requested url returned error: 503",
    "the requested url returned error: 504",
)

_REPOSITORY_NOT_FOUND_MARKERS = (
    "remote: repository not found",
    "the project you were looking for could not be found",
)

_FILTER_UNSUPPORTED_MARKERS = (
    "filtering not recognized by server",
    "server does not support filter",
)


class GitRepositoryCloner:
    """Clone full or sparse repositories through controlled Git processes."""

    def __init__(
        self,
        clone_depth: int = 1,
        git_executable: str = "git",
        timeout_seconds: float = 1800.0,
        partial_suffix_factory: Callable[[], str] | None = None,
        sparse_patterns: tuple[str, ...] | None = None,
    ) -> None:
        if clone_depth < 1:
            raise ValueError("clone_depth must be greater than zero")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if sparse_patterns is not None and (
            not sparse_patterns
            or any(not pattern.strip() for pattern in sparse_patterns)
        ):
            raise ValueError("sparse_patterns must contain non-empty patterns")

        git_path = shutil.which(git_executable)
        if git_path is None:
            raise GitNotAvailableError(f"Git executable not found: {git_executable}")

        self._clone_depth = clone_depth
        self._git_path = git_path
        self._timeout_seconds = timeout_seconds
        self._sparse_patterns = sparse_patterns
        self._partial_suffix_factory = partial_suffix_factory or (
            lambda: uuid4().hex
        )

    def clone(self, repository: GitRepository, destination: Path) -> None:
        if destination.exists():
            raise DestinationAlreadyExistsError(
                f"destination already exists: {destination}"
            )

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CloneError(
                f"cannot prepare destination for {repository.qualified_name}: {exc}"
            ) from exc

        temporary_root = destination.parent / (
            f".{destination.name}.part-{self._partial_suffix_factory()}"
        )
        try:
            temporary_root.mkdir()
        except OSError as exc:
            raise CloneError(
                f"cannot create partial workspace for "
                f"{repository.qualified_name}: {exc}"
            ) from exc
        temporary_destination = temporary_root / "repository"
        clone_command = [
            self._git_path,
            "clone",
            "--depth",
            str(self._clone_depth),
            "--single-branch",
            "--no-tags",
        ]
        if self._sparse_patterns is not None:
            clone_command.extend(("--filter=blob:none", "--no-checkout"))
        clone_command.extend(
            ("--", repository.clone_url, str(temporary_destination))
        )
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GIT_LFS_SKIP_SMUDGE"] = "1"

        LOGGER.debug(
            "Starting Git clone: repository=%s temporary_path=%s mode=%s",
            repository.qualified_name,
            temporary_destination,
            "full" if self._sparse_patterns is None else "sparse",
        )
        deadline = time.monotonic() + self._timeout_seconds
        try:
            self._run_git(
                clone_command,
                repository,
                environment,
                deadline,
                operation="clone",
            )

            if self._sparse_patterns is not None:
                LOGGER.debug(
                    "Configuring sparse checkout: repository=%s patterns=%s",
                    repository.qualified_name,
                    ",".join(self._sparse_patterns),
                )
                self._run_git(
                    [
                        self._git_path,
                        "-C",
                        str(temporary_destination),
                        "sparse-checkout",
                        "set",
                        "--no-cone",
                        "--",
                        *self._sparse_patterns,
                    ],
                    repository,
                    environment,
                    deadline,
                    operation="configure sparse checkout",
                )
                self._run_git(
                    [
                        self._git_path,
                        "-C",
                        str(temporary_destination),
                        "checkout",
                        "--force",
                    ],
                    repository,
                    environment,
                    deadline,
                    operation="materialize sparse checkout",
                )

            if not temporary_destination.is_dir():
                raise CloneError(
                    f"Git reported success but did not create a directory for "
                    f"{repository.qualified_name}"
                )
            if destination.exists():
                raise DestinationAlreadyExistsError(
                    f"destination appeared during clone: {destination}"
                )

            try:
                temporary_destination.replace(destination)
            except OSError as exc:
                raise CloneError(
                    f"cannot finalize clone for {repository.qualified_name}: {exc}"
                ) from exc
        finally:
            self._cleanup_partial(temporary_root)

    def _run_git(
        self,
        command: list[str],
        repository: GitRepository,
        environment: dict[str, str],
        deadline: float,
        *,
        operation: str,
    ) -> None:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise CloneTimeoutError(
                f"clone timed out after {self._timeout_seconds:g}s for "
                f"{repository.qualified_name} while attempting to {operation}"
            )
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=remaining_seconds,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            detail = self._normalize_output(exc.stderr)
            suffix = f": {detail}" if detail else ""
            raise CloneTimeoutError(
                f"clone timed out after {self._timeout_seconds:g}s for "
                f"{repository.qualified_name} while attempting to "
                f"{operation}{suffix}"
            ) from exc
        except OSError as exc:
            raise CloneError(
                f"could not start Git for {repository.qualified_name} while "
                f"attempting to {operation}: {exc}"
            ) from exc

        if completed.returncode == 0:
            detail = self._normalize_output(completed.stderr)
            if operation == "clone" and any(
                marker in detail.casefold()
                for marker in _FILTER_UNSUPPORTED_MARKERS
            ):
                LOGGER.warning(
                    "Remote does not support partial clone filtering: "
                    "repository=%s. The working tree remains C-only, but Git "
                    "may transfer all blobs",
                    repository.qualified_name,
                )
            return

        detail = self._normalize_output(completed.stderr) or "unknown Git error"
        if operation == "clone" and self._is_repository_not_found(detail):
            LOGGER.debug(
                "Remote repository rejected clone: repository=%s detail=%s",
                repository.qualified_name,
                detail,
            )
            raise RepositoryNotFoundError(
                f"repository not found or inaccessible: "
                f"{repository.qualified_name}; verify the URL and credentials"
            )
        error_type: type[CloneError]
        if self._is_transient_error(detail):
            error_type = TransientCloneError
        else:
            error_type = CloneError
        if operation == "clone":
            message = f"failed to clone {repository.qualified_name}: {detail}"
        else:
            message = (
                f"failed to {operation} for "
                f"{repository.qualified_name}: {detail}"
            )
        raise error_type(message)

    @staticmethod
    def _normalize_output(output: str | bytes | None) -> str:
        if output is None:
            return ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        return " ".join(output.split())

    @staticmethod
    def _is_transient_error(detail: str) -> bool:
        normalized = detail.casefold()
        return any(marker in normalized for marker in _TRANSIENT_ERROR_MARKERS)

    @staticmethod
    def _is_repository_not_found(detail: str) -> bool:
        normalized = detail.casefold()
        return any(
            marker in normalized for marker in _REPOSITORY_NOT_FOUND_MARKERS
        ) or (
            "fatal: repository '" in normalized
            and "' not found" in normalized
        )

    @staticmethod
    def _cleanup_partial(path: Path) -> None:
        try:
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
        except OSError as exc:
            LOGGER.warning("Could not remove partial clone %s: %s", path, exc)


class RetryingRepositoryCloner:
    """Retry transient clone failures with exponential backoff and jitter."""

    def __init__(
        self,
        delegate: RepositoryCloner,
        max_retries: int = 2,
        base_delay_seconds: float = 2.0,
        sleeper: Callable[[float], None] = time.sleep,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if base_delay_seconds < 0:
            raise ValueError("base_delay_seconds cannot be negative")
        self._delegate = delegate
        self._max_retries = max_retries
        self._base_delay_seconds = base_delay_seconds
        self._sleeper = sleeper
        self._random_source = random_source

    def clone(self, repository: GitRepository, destination: Path) -> None:
        for retry_number in range(self._max_retries + 1):
            try:
                self._delegate.clone(repository, destination)
                return
            except TransientCloneError as exc:
                if retry_number >= self._max_retries:
                    raise
                exponential_delay = self._base_delay_seconds * (2**retry_number)
                delay = exponential_delay + (
                    self._base_delay_seconds * self._random_source()
                )
                LOGGER.warning(
                    "Transient failure cloning %s; retry %d/%d in %.1fs: %s",
                    repository.qualified_name,
                    retry_number + 1,
                    self._max_retries,
                    delay,
                    exc,
                )
                self._sleeper(delay)
