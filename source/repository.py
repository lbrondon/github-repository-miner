import re
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from pathlib import Path
from typing import Sequence
from urllib.parse import unquote, urlsplit

from exceptions import InvalidRepositoryUrlError

_OWNER_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_PATH_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,100}")
_HOST_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
)


class RepositoryProvider(str, Enum):
    GITHUB = "github"
    GITLAB = "gitlab"


@dataclass(frozen=True, slots=True)
class GitRepository:
    """Validated identity of a GitHub or GitLab repository."""

    provider: RepositoryProvider
    host: str
    namespace: str
    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.provider, RepositoryProvider):
            raise InvalidRepositoryUrlError("invalid repository provider")

        normalized_host = self.host.casefold()
        if not _HOST_PATTERN.fullmatch(normalized_host):
            raise InvalidRepositoryUrlError(f"invalid repository host: {self.host}")
        detected_provider = self._provider_for_host(normalized_host, self.host)
        if detected_provider is not self.provider:
            raise InvalidRepositoryUrlError(
                f"provider {self.provider.value} does not match host {self.host}"
            )
        object.__setattr__(self, "host", normalized_host)

        namespace_parts = self.namespace.split("/")
        if not self.namespace or not self.name or not all(namespace_parts):
            raise InvalidRepositoryUrlError(
                "repository namespace and name are required"
            )
        if self.name.lower().endswith(".git"):
            raise InvalidRepositoryUrlError(
                "repository name must not include the .git suffix"
            )
        if self.provider is RepositoryProvider.GITHUB:
            if len(namespace_parts) != 1:
                raise InvalidRepositoryUrlError(
                    "GitHub repositories must have exactly one owner"
                )
            owner = namespace_parts[0]
            if not _OWNER_PATTERN.fullmatch(owner) or owner.endswith("-"):
                raise InvalidRepositoryUrlError(f"invalid GitHub owner: {owner}")

        all_segments = [*namespace_parts, self.name]
        if any(
            not _PATH_SEGMENT_PATTERN.fullmatch(segment)
            or segment in {".", "..", "-"}
            for segment in all_segments
        ):
            raise InvalidRepositoryUrlError(
                "invalid repository namespace or name"
            )

    @classmethod
    def from_url(cls, raw_url: str) -> "GitRepository":
        candidate = raw_url.strip()
        if not candidate:
            raise InvalidRepositoryUrlError("repository URL is empty")

        try:
            parsed = urlsplit(candidate)
            port = parsed.port
        except ValueError as exc:
            raise InvalidRepositoryUrlError(
                f"invalid repository URL: {candidate}"
            ) from exc

        if (
            parsed.scheme.lower() != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise InvalidRepositoryUrlError(
                f"unsupported repository URL: {candidate}"
            )

        host = parsed.hostname.lower()
        provider = cls._provider_for_host(host, candidate)
        path_parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
        minimum_parts = 2
        if len(path_parts) < minimum_parts or not all(path_parts):
            raise InvalidRepositoryUrlError(
                f"expected https://<host>/<namespace>/<repository>: {candidate}"
            )
        if provider is RepositoryProvider.GITHUB and len(path_parts) != 2:
            raise InvalidRepositoryUrlError(
                f"GitHub URLs must contain owner and repository only: {candidate}"
            )

        namespace_parts = path_parts[:-1]
        name = path_parts[-1]
        if name.lower().endswith(".git"):
            name = name[:-4]

        return cls(
            provider=provider,
            host=host,
            namespace="/".join(namespace_parts),
            name=name,
        )

    @staticmethod
    def _provider_for_host(host: str, candidate: str) -> RepositoryProvider:
        if host == "github.com":
            return RepositoryProvider.GITHUB
        if host == "gitlab.com" or host.startswith("gitlab."):
            return RepositoryProvider.GITLAB
        raise InvalidRepositoryUrlError(
            f"unsupported Git hosting provider: {candidate}"
        )

    @property
    def full_name(self) -> str:
        return f"{self.namespace}/{self.name}"

    @property
    def qualified_name(self) -> str:
        return f"{self.host}/{self.full_name}"

    @property
    def clone_url(self) -> str:
        return f"https://{self.host}/{self.full_name}.git"

    @property
    def identity_key(self) -> str:
        return self.qualified_name.casefold()

    @property
    def destination_name(self) -> str:
        return self.name

    @property
    def destination_path(self) -> Path:
        """Return the direct destination used when the name is unambiguous."""
        return Path(self.name)

    @property
    def hierarchical_destination_path(self) -> Path:
        """Return the path used by the previous hierarchical layout."""
        return Path(self.host, *self.namespace.split("/"), self.name)


def plan_repository_destinations(
    repositories: Sequence[GitRepository],
) -> dict[str, Path]:
    """Plan unique, direct children of ``projects`` for every repository.

    Repository names remain unchanged when unique. Repositories sharing a name
    receive a host suffix, and a namespace suffix when they also share a host.
    """
    names = Counter(repository.name.casefold() for repository in repositories)
    names_by_host = Counter(
        (repository.name.casefold(), repository.host.casefold())
        for repository in repositories
    )
    destinations: dict[str, Path] = {}
    reserved_names: set[str] = set()

    for repository in repositories:
        name_key = repository.name.casefold()
        host_key = repository.host.casefold()
        if names[name_key] == 1:
            directory_name = repository.name
        elif names_by_host[(name_key, host_key)] == 1:
            directory_name = f"{repository.name}--{repository.host}"
        else:
            namespace = repository.namespace.replace("/", "--")
            directory_name = (
                f"{repository.name}--{repository.host}--{namespace}"
            )

        normalized_name = directory_name.casefold()
        if len(directory_name.encode("utf-8")) > 240 or (
            normalized_name in reserved_names
        ):
            identity_digest = sha256(
                repository.identity_key.encode("utf-8")
            ).hexdigest()[:12]
            directory_name = f"{repository.name}--{identity_digest}"
            normalized_name = directory_name.casefold()
        if normalized_name in reserved_names:
            raise InvalidRepositoryUrlError(
                "duplicate repository identity in destination plan: "
                f"{repository.qualified_name}"
            )
        reserved_names.add(normalized_name)
        destinations[repository.identity_key] = Path(directory_name)

    return destinations
