from dataclasses import dataclass
from pathlib import Path

from repository import GitRepository


@dataclass(frozen=True, slots=True)
class ClonedRepository:
    repository: GitRepository
    destination: Path


@dataclass(frozen=True, slots=True)
class MiningFailure:
    repository: GitRepository
    message: str


@dataclass(frozen=True, slots=True)
class MiningResult:
    cloned: tuple[ClonedRepository, ...]
    failures: tuple[MiningFailure, ...]
    skipped_count: int = 0
    remaining_count: int = 0

    @property
    def succeeded(self) -> bool:
        return not self.failures

    @property
    def total(self) -> int:
        return self.processed + self.remaining_count

    @property
    def attempted(self) -> int:
        return len(self.cloned) + len(self.failures)

    @property
    def processed(self) -> int:
        return self.attempted + self.skipped_count

    @property
    def complete(self) -> bool:
        return self.succeeded and self.remaining_count == 0
