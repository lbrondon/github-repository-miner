from dataclasses import dataclass
from pathlib import Path

from exceptions import (
    InvalidRepositoryUrlError,
    RepositoryListError,
)
from repository import GitRepository


@dataclass(frozen=True, slots=True)
class RepositoryListIssue:
    line_number: int
    message: str


@dataclass(frozen=True, slots=True)
class PossibleMirrorGroup:
    logical_name: str
    repositories: tuple[GitRepository, ...]


@dataclass(frozen=True, slots=True)
class RepositoryListResult:
    repositories: tuple[GitRepository, ...]
    issues: tuple[RepositoryListIssue, ...]
    duplicate_count: int
    possible_mirror_groups: tuple[PossibleMirrorGroup, ...]


class RepositoryListReader:
    """Read, validate and remove duplicate GitHub and GitLab URLs."""

    def read(self, file_path: Path) -> tuple[GitRepository, ...]:
        """Read a repository list strictly, rejecting any invalid line."""
        result = self.scan(file_path)
        if result.issues:
            details = "\n".join(
                f"line {issue.line_number}: {issue.message}"
                for issue in result.issues
            )
            raise RepositoryListError(f"invalid repository list:\n{details}")
        if not result.repositories:
            raise RepositoryListError(
                f"repository list '{file_path}' contains no repository URLs"
            )
        return result.repositories

    def scan(self, file_path: Path) -> RepositoryListResult:
        """Read all valid entries and report invalid lines independently."""
        try:
            lines = file_path.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError) as exc:
            raise RepositoryListError(
                f"cannot read repository list '{file_path}': {exc}"
            ) from exc

        repositories: list[GitRepository] = []
        identities: set[str] = set()
        issues: list[RepositoryListIssue] = []
        duplicate_count = 0

        for line_number, raw_line in enumerate(lines, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            try:
                repository = GitRepository.from_url(line)
            except InvalidRepositoryUrlError as exc:
                issues.append(
                    RepositoryListIssue(line_number=line_number, message=str(exc))
                )
                continue

            if repository.identity_key in identities:
                duplicate_count += 1
                continue

            identities.add(repository.identity_key)
            repositories.append(repository)

        repositories_by_name: dict[str, list[GitRepository]] = {}
        for repository in repositories:
            repositories_by_name.setdefault(
                repository.full_name.casefold(), []
            ).append(repository)
        possible_mirror_groups = tuple(
            PossibleMirrorGroup(
                logical_name=group[0].full_name,
                repositories=tuple(group),
            )
            for group in repositories_by_name.values()
            if len({repository.host for repository in group}) > 1
        )

        return RepositoryListResult(
            repositories=tuple(repositories),
            issues=tuple(issues),
            duplicate_count=duplicate_count,
            possible_mirror_groups=possible_mirror_groups,
        )
