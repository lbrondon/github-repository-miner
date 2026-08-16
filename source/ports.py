from pathlib import Path
from typing import Mapping, Protocol, Sequence

from repository import GitRepository


class RepositoryCloner(Protocol):
    """Port implemented by repository cloning infrastructure."""

    def clone(self, repository: GitRepository, destination: Path) -> None:
        """Clone repository into destination or raise CloneError."""
        ...


class MiningStateStore(Protocol):
    """Port for durable repository mining progress."""

    def register(
        self,
        repositories: Sequence[GitRepository],
        projects_dir: Path,
    ) -> None:
        """Register repositories without resetting existing progress."""
        ...

    def successful_identities(self) -> set[str]:
        """Return completed identities whose Git destinations still exist."""
        ...

    def status_counts(
        self,
        repositories: Sequence[GitRepository] | None = None,
    ) -> Mapping[str, int]:
        """Return status counts, optionally scoped to one repository list."""
        ...

    def mark_started(self, repository: GitRepository, destination: Path) -> None:
        """Mark one repository attempt as running."""
        ...

    def mark_succeeded(self, repository: GitRepository, destination: Path) -> None:
        """Mark one repository as successfully cloned."""
        ...

    def mark_failed(
        self,
        repository: GitRepository,
        destination: Path,
        message: str,
    ) -> None:
        """Persist one repository failure."""
        ...
