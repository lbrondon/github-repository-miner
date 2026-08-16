import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from exceptions import MiningStateError
from repository import GitRepository, plan_repository_destinations

LOGGER = logging.getLogger(__name__)


class SqliteMiningStateStore:
    """Durable SQLite checkpoint store for large mining executions."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path
        try:
            database_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(database_path)
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._create_schema()
            self._recover_interrupted_attempts()
            LOGGER.debug("Checkpoint opened: path=%s", database_path)
        except (OSError, sqlite3.Error) as exc:
            raise MiningStateError(
                f"cannot prepare mining state database '{database_path}': {exc}"
            ) from exc

    def __enter__(self) -> "SqliteMiningStateStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._connection.close()
        except sqlite3.Error as exc:
            raise MiningStateError(
                f"cannot close mining state database '{self._database_path}': {exc}"
            ) from exc

    def register(
        self,
        repositories: Sequence[GitRepository],
        projects_dir: Path,
    ) -> None:
        now = self._now()
        destination_paths = plan_repository_destinations(repositories)
        rows = [
            (
                repository.identity_key,
                repository.qualified_name,
                repository.clone_url,
                str(projects_dir / destination_paths[repository.identity_key]),
                now,
            )
            for repository in repositories
        ]
        try:
            with self._connection:
                self._connection.executemany(
                    """
                    INSERT INTO repositories (
                        identity_key,
                        qualified_name,
                        clone_url,
                        destination,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(identity_key) DO UPDATE SET
                        qualified_name = excluded.qualified_name,
                        clone_url = excluded.clone_url,
                        destination = excluded.destination,
                        status = CASE
                            WHEN repositories.destination = excluded.destination
                                THEN repositories.status
                            ELSE 'pending'
                        END,
                        attempt_count = CASE
                            WHEN repositories.destination = excluded.destination
                                THEN repositories.attempt_count
                            ELSE 0
                        END,
                        last_error = CASE
                            WHEN repositories.destination = excluded.destination
                                THEN repositories.last_error
                            ELSE 'destination layout changed; scheduled again'
                        END,
                        updated_at = excluded.updated_at
                    """,
                    rows,
                )
        except sqlite3.Error as exc:
            raise self._state_error("register repositories", exc) from exc

    def successful_identities(self) -> set[str]:
        try:
            rows = self._connection.execute(
                "SELECT identity_key, qualified_name, destination "
                "FROM repositories WHERE status = 'succeeded'"
            ).fetchall()
            successful: set[str] = set()
            missing_or_invalid: list[tuple[str, str, str, str]] = []
            now = self._now()
            for identity_key, qualified_name, raw_destination in rows:
                destination = Path(raw_destination)
                if (
                    destination.is_dir()
                    and not destination.is_symlink()
                    and (destination / ".git").is_dir()
                ):
                    successful.add(identity_key)
                    continue
                reason = (
                    "completed clone is missing; scheduled again"
                    if not destination.exists()
                    else "completed destination is not a Git clone; scheduled again"
                )
                missing_or_invalid.append(
                    (reason, now, identity_key, qualified_name)
                )

            if missing_or_invalid:
                with self._connection:
                    self._connection.executemany(
                        """
                        UPDATE repositories
                        SET status = 'pending', last_error = ?, updated_at = ?
                        WHERE identity_key = ? AND status = 'succeeded'
                        """,
                        [
                            (reason, updated_at, identity_key)
                            for reason, updated_at, identity_key, _qualified_name
                            in missing_or_invalid
                        ],
                    )
                examples = ",".join(
                    qualified_name
                    for _reason, _updated_at, _identity_key, qualified_name
                    in missing_or_invalid[:5]
                )
                LOGGER.warning(
                    "Checkpoint reconciliation returned missing or invalid "
                    "clones to pending: count=%d examples=%s",
                    len(missing_or_invalid),
                    examples,
                )
            return successful
        except sqlite3.Error as exc:
            raise self._state_error("read completed repositories", exc) from exc

    def status_counts(
        self,
        repositories: Sequence[GitRepository] | None = None,
    ) -> Mapping[str, int]:
        counts = {
            "pending": 0,
            "running": 0,
            "succeeded": 0,
            "failed": 0,
        }
        try:
            if repositories is None:
                rows = self._connection.execute(
                    "SELECT status, COUNT(*) FROM repositories GROUP BY status"
                )
                counts.update({status: count for status, count in rows})
                return counts

            identities = tuple(
                repository.identity_key for repository in repositories
            )
            # Keep below SQLite builds whose host-parameter limit is 999.
            chunk_size = 900
            for offset in range(0, len(identities), chunk_size):
                chunk = identities[offset : offset + chunk_size]
                placeholders = ",".join("?" for _identity in chunk)
                rows = self._connection.execute(
                    "SELECT status, COUNT(*) FROM repositories "
                    f"WHERE identity_key IN ({placeholders}) GROUP BY status",
                    chunk,
                )
                for status, count in rows:
                    counts[status] += count
            return counts
        except sqlite3.Error as exc:
            raise self._state_error("summarize repository statuses", exc) from exc

    def mark_started(self, repository: GitRepository, destination: Path) -> None:
        self._update_status(
            repository,
            destination,
            status="running",
            error=None,
            increment_attempt=True,
        )

    def mark_succeeded(self, repository: GitRepository, destination: Path) -> None:
        self._update_status(
            repository,
            destination,
            status="succeeded",
            error=None,
            increment_attempt=False,
        )

    def mark_failed(
        self,
        repository: GitRepository,
        destination: Path,
        message: str,
    ) -> None:
        self._update_status(
            repository,
            destination,
            status="failed",
            error=message,
            increment_attempt=False,
        )

    def _create_schema(self) -> None:
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS repositories (
                    identity_key TEXT PRIMARY KEY,
                    qualified_name TEXT NOT NULL,
                    clone_url TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'running', 'succeeded', 'failed')),
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS repositories_status_idx "
                "ON repositories(status)"
            )

    def _recover_interrupted_attempts(self) -> None:
        interrupted = self._connection.execute(
            "SELECT identity_key, destination FROM repositories "
            "WHERE status = 'running'"
        ).fetchall()
        finalized_count = 0
        pending_count = 0
        with self._connection:
            for identity_key, raw_destination in interrupted:
                destination = Path(raw_destination)
                clone_was_finalized = (
                    destination.is_dir() and (destination / ".git").is_dir()
                )
                status = "succeeded" if clone_was_finalized else "pending"
                error = (
                    None
                    if clone_was_finalized
                    else "previous execution was interrupted"
                )
                if clone_was_finalized:
                    finalized_count += 1
                else:
                    pending_count += 1
                self._connection.execute(
                    """
                    UPDATE repositories
                    SET status = ?, last_error = ?, updated_at = ?
                    WHERE identity_key = ?
                    """,
                    (status, error, self._now(), identity_key),
                )
        if interrupted:
            LOGGER.warning(
                "Recovered interrupted checkpoint entries: total=%d "
                "finalized_as_succeeded=%d returned_to_pending=%d",
                len(interrupted),
                finalized_count,
                pending_count,
            )

    def _update_status(
        self,
        repository: GitRepository,
        destination: Path,
        *,
        status: str,
        error: str | None,
        increment_attempt: bool,
    ) -> None:
        attempt_expression = (
            "attempt_count + 1" if increment_attempt else "attempt_count"
        )
        try:
            with self._connection:
                cursor = self._connection.execute(
                    f"""
                    UPDATE repositories
                    SET status = ?,
                        destination = ?,
                        attempt_count = {attempt_expression},
                        last_error = ?,
                        updated_at = ?
                    WHERE identity_key = ?
                    """,
                    (
                        status,
                        str(destination),
                        error,
                        self._now(),
                        repository.identity_key,
                    ),
                )
                if cursor.rowcount != 1:
                    raise MiningStateError(
                        f"repository is not registered in mining state: "
                        f"{repository.qualified_name}"
                    )
        except sqlite3.Error as exc:
            raise self._state_error(
                f"mark {repository.qualified_name} as {status}", exc
            ) from exc

    def _state_error(self, operation: str, exc: sqlite3.Error) -> MiningStateError:
        return MiningStateError(
            f"cannot {operation} in '{self._database_path}': {exc}"
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
