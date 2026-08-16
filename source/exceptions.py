class RepositoryMinerError(Exception):
    """Base exception for expected mining failures."""


class InvalidRepositoryUrlError(RepositoryMinerError):
    """Raised when a repository URL is not a supported Git host URL."""


class RepositoryListError(RepositoryMinerError):
    """Raised when the repository list cannot be read or validated."""


class OutputDirectoryError(RepositoryMinerError):
    """Raised when the projects output directory cannot be prepared."""


class CloneError(RepositoryMinerError):
    """Raised when Git cannot clone a repository."""


class TransientCloneError(CloneError):
    """Raised for clone failures that may succeed on a later attempt."""


class CloneTimeoutError(TransientCloneError):
    """Raised when a Git clone exceeds its configured timeout."""


class GitNotAvailableError(CloneError):
    """Raised when the Git executable is unavailable."""


class DestinationAlreadyExistsError(CloneError):
    """Raised when cloning would overwrite an existing path."""


class RepositoryNotFoundError(CloneError):
    """Raised when a remote repository URL is missing or inaccessible."""


class MiningStateError(RepositoryMinerError):
    """Raised when durable mining state cannot be read or updated."""
