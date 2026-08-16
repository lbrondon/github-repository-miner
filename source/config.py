from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MiningConfig:
    """Immutable configuration for one repository mining execution."""

    repositories_file: Path
    projects_dir: Path
    clone_depth: int = 1
    state_file: Path | None = None
    workers: int = 4
    clone_timeout_seconds: float = 1800.0
    max_retries: int = 2
    retry_base_delay_seconds: float = 2.0
    progress_interval_seconds: float = 30.0
    batch_size: int | None = None
    c_only: bool = False

    def __post_init__(self) -> None:
        if self.clone_depth < 1:
            raise ValueError("clone_depth must be greater than zero")
        if self.workers < 1:
            raise ValueError("workers must be greater than zero")
        if self.clone_timeout_seconds <= 0:
            raise ValueError("clone_timeout_seconds must be greater than zero")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        if self.retry_base_delay_seconds < 0:
            raise ValueError("retry_base_delay_seconds cannot be negative")
        if self.progress_interval_seconds <= 0:
            raise ValueError("progress_interval_seconds must be greater than zero")
        if self.batch_size is not None and self.batch_size < 1:
            raise ValueError("batch_size must be greater than zero when provided")
