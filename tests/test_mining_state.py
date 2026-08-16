import sqlite3
import tempfile
import unittest
from pathlib import Path

from mining_service import RepositoryMiningService
from mining_state import SqliteMiningStateStore
from repository import GitRepository


class _MaterializingCloner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def clone(self, repository: GitRepository, destination: Path) -> None:
        self.calls.append(repository.qualified_name)
        (destination / ".git").mkdir(parents=True)


class SqliteMiningStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repositories = (
            GitRepository.from_url("https://github.com/curl/curl"),
            GitRepository.from_url("https://github.com/OpenVPN/openvpn"),
        )

    def test_persists_success_and_skips_existing_completed_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects_dir = root / "projects"
            database = root / "state.sqlite3"
            first_cloner = _MaterializingCloner()

            with SqliteMiningStateStore(database) as state:
                first_result = RepositoryMiningService(
                    first_cloner,
                    state_store=state,
                ).mine(self.repositories, projects_dir)

            second_cloner = _MaterializingCloner()
            with SqliteMiningStateStore(database) as state:
                second_result = RepositoryMiningService(
                    second_cloner,
                    state_store=state,
                ).mine(self.repositories, projects_dir)

        self.assertEqual(2, len(first_result.cloned))
        self.assertEqual([], second_cloner.calls)
        self.assertEqual(2, second_result.skipped_count)
        self.assertTrue(second_result.complete)

    def test_missing_completed_repository_is_returned_to_pending_and_recloned(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects_dir = root / "projects"
            database = root / "state.sqlite3"

            with SqliteMiningStateStore(database) as state:
                RepositoryMiningService(
                    _MaterializingCloner(),
                    state_store=state,
                ).mine(self.repositories, projects_dir)

            for repository in self.repositories:
                destination = projects_dir / repository.destination_path
                for child in (destination / ".git").iterdir():
                    self.fail(f"unexpected fixture content: {child}")
                (destination / ".git").rmdir()
                destination.rmdir()

            second_cloner = _MaterializingCloner()
            with self.assertLogs("mining_state", level="WARNING") as captured:
                with SqliteMiningStateStore(database) as state:
                    result = RepositoryMiningService(
                        second_cloner,
                        state_store=state,
                    ).mine(self.repositories, projects_dir)

        self.assertEqual(2, len(second_cloner.calls))
        self.assertEqual(0, result.skipped_count)
        self.assertEqual(2, len(result.cloned))
        self.assertIn("returned missing or invalid", "\n".join(captured.output))

    def test_batch_processes_only_selected_pending_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects_dir = root / "projects"
            database = root / "state.sqlite3"
            first_cloner = _MaterializingCloner()

            with SqliteMiningStateStore(database) as state:
                first_result = RepositoryMiningService(
                    first_cloner,
                    state_store=state,
                    batch_size=1,
                ).mine(self.repositories, projects_dir)

                second_cloner = _MaterializingCloner()
                second_result = RepositoryMiningService(
                    second_cloner,
                    state_store=state,
                    batch_size=1,
                ).mine(self.repositories, projects_dir)

        self.assertEqual(1, len(first_result.cloned))
        self.assertEqual(1, first_result.remaining_count)
        self.assertEqual(1, len(second_result.cloned))
        self.assertEqual(1, second_result.skipped_count)
        self.assertEqual(0, second_result.remaining_count)

    def test_destination_change_resets_success_to_pending(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            repository = self.repositories[0]
            old_projects_dir = root / "old-projects"
            new_projects_dir = root / "output" / "projects"

            with SqliteMiningStateStore(database) as state:
                state.register((repository,), old_projects_dir)
                state.mark_succeeded(
                    repository,
                    old_projects_dir / repository.destination_path,
                )
                state.register((repository,), new_projects_dir)

                self.assertNotIn(
                    repository.identity_key,
                    state.successful_identities(),
                )
                self.assertEqual(1, state.status_counts()["pending"])

    def test_recovers_running_status_after_interrupted_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            projects_dir = root / "projects"
            repository = self.repositories[0]
            destination = projects_dir / repository.destination_path

            with SqliteMiningStateStore(database) as state:
                state.register((repository,), projects_dir)
                state.mark_started(repository, destination)

            with self.assertLogs("mining_state", level="WARNING"):
                with SqliteMiningStateStore(database):
                    pass

            connection = sqlite3.connect(database)
            try:
                status, last_error = connection.execute(
                    "SELECT status, last_error FROM repositories"
                ).fetchone()
            finally:
                connection.close()

        self.assertEqual("pending", status)
        self.assertIn("interrupted", last_error)

    def test_recovers_finalized_clone_after_checkpoint_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            projects_dir = root / "projects"
            repository = self.repositories[0]
            destination = projects_dir / repository.destination_path

            with SqliteMiningStateStore(database) as state:
                state.register((repository,), projects_dir)
                state.mark_started(repository, destination)
                (destination / ".git").mkdir(parents=True)

            with self.assertLogs("mining_state", level="WARNING"):
                with SqliteMiningStateStore(database) as recovered_state:
                    identities = recovered_state.successful_identities()

        self.assertEqual({repository.identity_key}, identities)

    def test_reports_checkpoint_status_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "state.sqlite3"
            projects_dir = root / "projects"

            with SqliteMiningStateStore(database) as state:
                state.register(self.repositories, projects_dir)
                state.mark_succeeded(
                    self.repositories[0],
                    projects_dir / self.repositories[0].destination_path,
                )
                state.mark_failed(
                    self.repositories[1],
                    projects_dir / self.repositories[1].destination_path,
                    "test failure",
                )
                counts = state.status_counts()

        self.assertEqual(
            {"pending": 0, "running": 0, "succeeded": 1, "failed": 1},
            counts,
        )

    def test_scopes_checkpoint_counts_to_current_repository_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects_dir = root / "projects"

            with SqliteMiningStateStore(root / "state.sqlite3") as state:
                state.register(self.repositories, projects_dir)
                state.mark_failed(
                    self.repositories[1],
                    projects_dir / self.repositories[1].destination_path,
                    "stale failure",
                )
                counts = state.status_counts(self.repositories[:1])

        self.assertEqual(
            {"pending": 1, "running": 0, "succeeded": 0, "failed": 0},
            counts,
        )


if __name__ == "__main__":
    unittest.main()
