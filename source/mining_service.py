import logging
import shutil
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from exceptions import CloneError, OutputDirectoryError
from mining_result import ClonedRepository, MiningFailure, MiningResult
from ports import MiningStateStore, RepositoryCloner
from repository import GitRepository, plan_repository_destinations

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _MiningJob:
    index: int
    repository: GitRepository
    destination: Path


@dataclass(frozen=True, slots=True)
class _ActiveJob:
    job: _MiningJob
    started_at: float


class RepositoryMiningService:
    """Coordinate bounded concurrent cloning with durable progress."""

    def __init__(
        self,
        cloner: RepositoryCloner,
        *,
        max_workers: int = 1,
        state_store: MiningStateStore | None = None,
        progress_interval_seconds: float = 30.0,
        batch_size: int | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be greater than zero")
        if progress_interval_seconds <= 0:
            raise ValueError("progress_interval_seconds must be greater than zero")
        if batch_size is not None and batch_size < 1:
            raise ValueError("batch_size must be greater than zero when provided")
        self._cloner = cloner
        self._max_workers = max_workers
        self._state_store = state_store
        self._progress_interval_seconds = progress_interval_seconds
        self._batch_size = batch_size

    def mine(
        self,
        repositories: Sequence[GitRepository],
        projects_dir: Path,
    ) -> MiningResult:
        try:
            projects_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OutputDirectoryError(
                f"cannot prepare projects directory '{projects_dir}': {exc}"
            ) from exc

        repository_sequence = tuple(repositories)
        destination_paths = plan_repository_destinations(repository_sequence)
        disambiguated_count = sum(
            destination_paths[repository.identity_key].name != repository.name
            for repository in repository_sequence
        )
        LOGGER.info(
            "Destination plan: root=%s direct_children=%d "
            "disambiguated_names=%d",
            projects_dir,
            len(destination_paths),
            disambiguated_count,
        )
        self._warn_about_hierarchical_layout(repository_sequence, projects_dir)
        successful_identities: set[str] = set()
        if self._state_store is not None:
            self._state_store.register(repository_sequence, projects_dir)
            successful_identities = self._state_store.successful_identities()
            status_counts = self._state_store.status_counts(repository_sequence)
            LOGGER.info(
                "Checkpoint state for current input: pending=%d running=%d "
                "succeeded=%d failed=%d",
                status_counts["pending"],
                status_counts["running"],
                status_counts["succeeded"],
                status_counts["failed"],
            )

        pending_jobs: list[_MiningJob] = []
        skipped_count = 0
        for index, repository in enumerate(repository_sequence):
            destination = projects_dir / destination_paths[repository.identity_key]
            if repository.identity_key in successful_identities:
                skipped_count += 1
            else:
                pending_jobs.append(_MiningJob(index, repository, destination))

        selected_jobs = pending_jobs
        remaining_count = 0
        if self._batch_size is not None:
            selected_jobs = pending_jobs[: self._batch_size]
            remaining_count = len(pending_jobs) - len(selected_jobs)

        LOGGER.info(
            "Mining started: total=%d selected=%d already_completed=%d "
            "remaining_after_batch=%d workers=%d disk_free=%s",
            len(repository_sequence),
            len(selected_jobs),
            skipped_count,
            remaining_count,
            self._max_workers,
            self._disk_free_text(projects_dir),
        )

        cloned_by_index: dict[int, ClonedRepository] = {}
        failures_by_index: dict[int, MiningFailure] = {}
        if selected_jobs:
            self._execute_jobs(
                selected_jobs,
                cloned_by_index,
                failures_by_index,
                projects_dir,
            )

        return MiningResult(
            cloned=tuple(cloned_by_index[index] for index in sorted(cloned_by_index)),
            failures=tuple(
                failures_by_index[index] for index in sorted(failures_by_index)
            ),
            skipped_count=skipped_count,
            remaining_count=remaining_count,
        )

    def _execute_jobs(
        self,
        jobs: Sequence[_MiningJob],
        cloned_by_index: dict[int, ClonedRepository],
        failures_by_index: dict[int, MiningFailure],
        projects_dir: Path,
    ) -> None:
        queue = deque(jobs)
        active: dict[Future[float], _ActiveJob] = {}
        started_at = time.monotonic()
        last_progress_at = started_at
        completed_count = 0

        with ThreadPoolExecutor(
            max_workers=self._max_workers,
            thread_name_prefix="repository-cloner",
        ) as executor:
            while queue or active:
                while queue and len(active) < self._max_workers:
                    job = queue.popleft()
                    if self._state_store is not None:
                        self._state_store.mark_started(
                            job.repository,
                            job.destination,
                        )
                    future = executor.submit(self._clone_one, job)
                    active[future] = _ActiveJob(job, time.monotonic())

                done, _ = wait(
                    active,
                    timeout=self._progress_interval_seconds,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    oldest_seconds, oldest_repository = self._oldest_active(active)
                    last_progress_at = self._log_progress(
                        completed_count,
                        len(jobs),
                        len(active),
                        len(queue),
                        len(cloned_by_index),
                        len(failures_by_index),
                        started_at,
                        oldest_seconds,
                        oldest_repository,
                        self._disk_free_text(projects_dir),
                    )
                    continue

                for future in done:
                    active_job = active.pop(future)
                    job = active_job.job
                    duration_seconds = time.monotonic() - active_job.started_at
                    completion_number = completed_count + 1
                    try:
                        duration_seconds = future.result()
                    except CloneError as exc:
                        failure = MiningFailure(
                            repository=job.repository,
                            message=str(exc),
                        )
                        failures_by_index[job.index] = failure
                        if self._state_store is not None:
                            self._state_store.mark_failed(
                                job.repository,
                                job.destination,
                                failure.message,
                            )
                        LOGGER.error(
                            "Repository completed: progress=%d/%d status=failed "
                            "repository=%s duration_seconds=%.3f error=%s",
                            completion_number,
                            len(jobs),
                            job.repository.qualified_name,
                            duration_seconds,
                            failure.message,
                        )
                    except Exception as exc:  # Defensive worker boundary.
                        message = (
                            f"unexpected failure cloning "
                            f"{job.repository.qualified_name}: {exc}"
                        )
                        failures_by_index[job.index] = MiningFailure(
                            repository=job.repository,
                            message=message,
                        )
                        if self._state_store is not None:
                            self._state_store.mark_failed(
                                job.repository,
                                job.destination,
                                message,
                            )
                        LOGGER.exception(
                            "Repository completed: progress=%d/%d "
                            "status=unexpected_failure repository=%s "
                            "duration_seconds=%.3f error=%s",
                            completion_number,
                            len(jobs),
                            job.repository.qualified_name,
                            duration_seconds,
                            message,
                        )
                    else:
                        cloned_by_index[job.index] = ClonedRepository(
                            repository=job.repository,
                            destination=job.destination,
                        )
                        if self._state_store is not None:
                            self._state_store.mark_succeeded(
                                job.repository,
                                job.destination,
                            )
                        LOGGER.info(
                            "Repository completed: progress=%d/%d "
                            "status=succeeded repository=%s "
                            "duration_seconds=%.3f destination=%s",
                            completion_number,
                            len(jobs),
                            job.repository.qualified_name,
                            duration_seconds,
                            job.destination,
                        )
                    completed_count = completion_number

                now = time.monotonic()
                if (
                    now - last_progress_at >= self._progress_interval_seconds
                    or completed_count == len(jobs)
                ):
                    oldest_seconds, oldest_repository = self._oldest_active(active)
                    last_progress_at = self._log_progress(
                        completed_count,
                        len(jobs),
                        len(active),
                        len(queue),
                        len(cloned_by_index),
                        len(failures_by_index),
                        started_at,
                        oldest_seconds,
                        oldest_repository,
                        self._disk_free_text(projects_dir),
                    )

    def _clone_one(self, job: _MiningJob) -> float:
        started_at = time.monotonic()
        LOGGER.debug(
            "Clone started: repository=%s destination=%s",
            job.repository.qualified_name,
            job.destination,
        )
        self._cloner.clone(job.repository, job.destination)
        return time.monotonic() - started_at

    @staticmethod
    def _oldest_active(
        active: dict[Future[float], _ActiveJob],
    ) -> tuple[float, str]:
        if not active:
            return 0.0, "none"
        oldest = min(active.values(), key=lambda item: item.started_at)
        elapsed = max(time.monotonic() - oldest.started_at, 0.0)
        return elapsed, oldest.job.repository.qualified_name

    @staticmethod
    def _disk_free_text(path: Path) -> str:
        try:
            free_bytes = shutil.disk_usage(path).free
        except OSError:
            return "unknown"
        return f"{free_bytes / (1024**3):.1f}GiB"

    @staticmethod
    def _warn_about_hierarchical_layout(
        repositories: Sequence[GitRepository],
        projects_dir: Path,
    ) -> None:
        hierarchical_paths: set[Path] = set()
        for repository in repositories:
            old_destination = (
                projects_dir / repository.hierarchical_destination_path
            )
            if old_destination.is_dir():
                hierarchical_paths.add(old_destination)

        if not hierarchical_paths:
            return
        examples = ", ".join(
            str(path) for path in sorted(hierarchical_paths)[:3]
        )
        LOGGER.warning(
            "Previous hierarchical-layout clones detected: count=%d "
            "examples=%s. They are not reused by the direct-child layout",
            len(hierarchical_paths),
            examples,
        )

    @staticmethod
    def _log_progress(
        completed: int,
        total: int,
        active: int,
        queued: int,
        succeeded: int,
        failed: int,
        started_at: float,
        oldest_active_seconds: float = 0.0,
        oldest_active_repository: str = "none",
        disk_free: str = "unknown",
    ) -> float:
        now = time.monotonic()
        elapsed = max(now - started_at, 0.001)
        rate_per_minute = completed / elapsed * 60
        remaining = total - completed
        eta_seconds = remaining / (completed / elapsed) if completed else None
        eta_text = (
            f"{eta_seconds / 60:.1f}min" if eta_seconds is not None else "unknown"
        )
        completion_percentage = completed / total * 100 if total else 100.0
        LOGGER.info(
            "Progress: completed=%d/%d percentage=%.1f%% succeeded=%d "
            "failed=%d active=%d queued=%d rate=%.2f repositories/min "
            "elapsed=%.1fmin eta=%s oldest_active=%.1fs "
            "oldest_repository=%s disk_free=%s",
            completed,
            total,
            completion_percentage,
            succeeded,
            failed,
            active,
            queued,
            rate_per_minute,
            elapsed / 60,
            eta_text,
            oldest_active_seconds,
            oldest_active_repository,
            disk_free,
        )
        return now
