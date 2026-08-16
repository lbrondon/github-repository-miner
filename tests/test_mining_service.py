import tempfile
import threading
import time
import unittest
from pathlib import Path

from exceptions import CloneError
from mining_service import RepositoryMiningService
from repository import GitRepository


class RecordingCloner:
    def __init__(self, failing_repository: str | None = None) -> None:
        self.failing_repository = failing_repository
        self.calls: list[tuple[str, Path]] = []

    def clone(self, repository: GitRepository, destination: Path) -> None:
        self.calls.append((repository.full_name, destination))
        if repository.full_name == self.failing_repository:
            raise CloneError(f"failed to clone {repository.full_name}")


class RepositoryMiningServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repositories = (
            GitRepository.from_url("https://github.com/curl/curl"),
            GitRepository.from_url("https://gitlab.gnome.org/GNOME/libxml2"),
        )

    def test_clones_every_repository_into_projects_directory(self) -> None:
        cloner = RecordingCloner()
        service = RepositoryMiningService(cloner)

        with tempfile.TemporaryDirectory() as directory:
            projects_dir = Path(directory) / "output" / "projects"
            result = service.mine(self.repositories, projects_dir)

            self.assertTrue(projects_dir.is_dir())
            self.assertEqual(
                [
                    (
                        "curl/curl",
                        projects_dir / "curl",
                    ),
                    (
                        "GNOME/libxml2",
                        projects_dir / "libxml2",
                    ),
                ],
                cloner.calls,
            )

        self.assertTrue(result.succeeded)
        self.assertEqual(2, result.total)

    def test_continues_after_clone_failure(self) -> None:
        cloner = RecordingCloner(failing_repository="curl/curl")
        service = RepositoryMiningService(cloner)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertLogs("mining_service", level="ERROR"):
                result = service.mine(
                    self.repositories,
                    Path(directory) / "projects",
                )

        self.assertFalse(result.succeeded)
        self.assertEqual(1, len(result.cloned))
        self.assertEqual(1, len(result.failures))
        self.assertEqual(2, len(cloner.calls))

    def test_limits_concurrent_clone_count(self) -> None:
        class ConcurrentRecordingCloner:
            def __init__(self) -> None:
                self._lock = threading.Lock()
                self.active = 0
                self.maximum_active = 0

            def clone(self, _repository, _destination) -> None:
                with self._lock:
                    self.active += 1
                    self.maximum_active = max(self.maximum_active, self.active)
                time.sleep(0.02)
                with self._lock:
                    self.active -= 1

        repositories = tuple(
            GitRepository.from_url(
                f"https://github.com/owner/repository-{index}"
            )
            for index in range(8)
        )
        cloner = ConcurrentRecordingCloner()
        service = RepositoryMiningService(cloner, max_workers=3)

        with tempfile.TemporaryDirectory() as directory:
            result = service.mine(repositories, Path(directory) / "projects")

        self.assertTrue(result.succeeded)
        self.assertGreater(cloner.maximum_active, 1)
        self.assertLessEqual(cloner.maximum_active, 3)

    def test_heartbeat_reports_active_repository_and_free_disk(self) -> None:
        class SlowCloner:
            def clone(self, _repository, _destination) -> None:
                time.sleep(0.02)

        service = RepositoryMiningService(
            SlowCloner(),
            progress_interval_seconds=0.005,
        )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertLogs("mining_service", level="INFO") as captured:
                service.mine(self.repositories[:1], Path(directory) / "projects")

        log_text = "\n".join(captured.output)
        self.assertIn("oldest_repository=github.com/curl/curl", log_text)
        self.assertIn("disk_free=", log_text)

    def test_name_collisions_are_cloned_as_distinct_direct_children(self) -> None:
        repositories = (
            GitRepository.from_url("https://github.com/GNOME/libxml2"),
            GitRepository.from_url(
                "https://gitlab.gnome.org/GNOME/libxml2"
            ),
        )
        cloner = RecordingCloner()

        with tempfile.TemporaryDirectory() as directory:
            projects_dir = Path(directory) / "output" / "projects"
            result = RepositoryMiningService(cloner).mine(
                repositories,
                projects_dir,
            )

        self.assertTrue(result.succeeded)
        self.assertEqual(
            {
                projects_dir / "libxml2--github.com",
                projects_dir / "libxml2--gitlab.gnome.org",
            },
            {destination for _name, destination in cloner.calls},
        )

    def test_warns_when_hierarchical_layout_is_present(self) -> None:
        service = RepositoryMiningService(RecordingCloner())

        with tempfile.TemporaryDirectory() as directory:
            projects_dir = Path(directory) / "projects"
            (projects_dir / "github.com" / "curl" / "curl").mkdir(
                parents=True
            )
            with self.assertLogs("mining_service", level="WARNING") as captured:
                service.mine(self.repositories[:1], projects_dir)

        self.assertIn("hierarchical-layout", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
