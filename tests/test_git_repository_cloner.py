import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from exceptions import (
    CloneError,
    CloneTimeoutError,
    DestinationAlreadyExistsError,
    GitNotAvailableError,
    RepositoryNotFoundError,
    TransientCloneError,
)
from git_repository_cloner import (
    GitRepositoryCloner,
    RetryingRepositoryCloner,
)
from repository import GitRepository


def _create_local_git_repository(root: Path) -> Path:
    origin = root / "origin"
    origin.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(origin)],
        check=True,
        capture_output=True,
    )
    (origin / "source.c").write_text("int main(void) { return 0; }\n")
    (origin / "README.md").write_text("complete checkout fixture\n")
    subprocess.run(
        ["git", "-C", str(origin), "add", "."],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(origin),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    return origin


class GitRepositoryClonerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = GitRepository.from_url("https://github.com/curl/curl")

    def test_builds_gitlab_clone_url(self) -> None:
        repository = GitRepository.from_url(
            "https://gitlab.gnome.org/GNOME/libxml2"
        )

        self.assertEqual(
            "https://gitlab.gnome.org/GNOME/libxml2.git",
            repository.clone_url,
        )

    @patch("git_repository_cloner.subprocess.run")
    @patch("git_repository_cloner.shutil.which")
    def test_executes_atomic_shallow_c_only_clone(self, which, run) -> None:
        which.return_value = "/usr/bin/git"

        def materialize_clone(command, **_kwargs):
            if command[1] == "clone":
                Path(command[-1]).mkdir()
            return subprocess.CompletedProcess(command, 0, "", "")

        run.side_effect = materialize_clone

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "curl"
            GitRepositoryCloner(
                clone_depth=2,
                timeout_seconds=45,
                partial_suffix_factory=lambda: "test",
                sparse_patterns=("*.c",),
            ).clone(self.repository, destination)
            temporary_root = Path(directory) / ".curl.part-test"
            temporary_destination = temporary_root / "repository"

            self.assertTrue(destination.is_dir())
            self.assertFalse(temporary_root.exists())

        calls = run.call_args_list
        clone_command = calls[0].args[0]
        sparse_command = calls[1].args[0]
        checkout_command = calls[2].args[0]
        options = calls[0].kwargs
        self.assertEqual(
            [
                "/usr/bin/git",
                "clone",
                "--depth",
                "2",
                "--single-branch",
                "--no-tags",
                "--filter=blob:none",
                "--no-checkout",
                "--",
                "https://github.com/curl/curl.git",
                str(temporary_destination),
            ],
            clone_command,
        )
        self.assertEqual(
            [
                "/usr/bin/git",
                "-C",
                str(temporary_destination),
                "sparse-checkout",
                "set",
                "--no-cone",
                "--",
                "*.c",
            ],
            sparse_command,
        )
        self.assertEqual(
            [
                "/usr/bin/git",
                "-C",
                str(temporary_destination),
                "checkout",
                "--force",
            ],
            checkout_command,
        )
        self.assertFalse(options["check"])
        self.assertTrue(options["capture_output"])
        self.assertTrue(options["text"])
        self.assertGreater(options["timeout"], 0)
        self.assertLessEqual(options["timeout"], 45)
        self.assertEqual("0", options["env"]["GIT_TERMINAL_PROMPT"])
        self.assertEqual("1", options["env"]["GIT_LFS_SKIP_SMUDGE"])

    @patch("git_repository_cloner.subprocess.run")
    @patch("git_repository_cloner.shutil.which", return_value="/usr/bin/git")
    def test_default_is_full_checkout(self, _which, run) -> None:
        def materialize_clone(command, **_kwargs):
            Path(command[-1]).mkdir()
            return subprocess.CompletedProcess(command, 0, "", "")

        run.side_effect = materialize_clone

        with tempfile.TemporaryDirectory() as directory:
            GitRepositoryCloner().clone(
                self.repository,
                Path(directory) / "curl",
            )

        self.assertEqual(1, run.call_count)
        command = run.call_args.args[0]
        self.assertNotIn("--filter=blob:none", command)
        self.assertNotIn("--no-checkout", command)

    def test_real_local_sparse_checkout_materializes_only_c_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = _create_local_git_repository(root)
            repository = SimpleNamespace(
                qualified_name="local/fixture",
                clone_url=origin.as_uri(),
            )
            destination = root / "result"

            with self.assertLogs(
                "git_repository_cloner",
                level="WARNING",
            ):
                GitRepositoryCloner(sparse_patterns=("*.c",)).clone(
                    repository,
                    destination,
                )

            self.assertTrue((destination / ".git").is_dir())
            self.assertTrue((destination / "source.c").is_file())
            self.assertFalse((destination / "README.md").exists())

    def test_real_local_default_materializes_complete_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = _create_local_git_repository(root)
            repository = SimpleNamespace(
                qualified_name="local/fixture",
                clone_url=origin.as_uri(),
            )
            destination = root / "result"

            GitRepositoryCloner().clone(repository, destination)

            self.assertTrue((destination / ".git").is_dir())
            self.assertTrue((destination / "source.c").is_file())
            self.assertTrue((destination / "README.md").is_file())

    @patch("git_repository_cloner.shutil.which", return_value="/usr/bin/git")
    def test_rejects_existing_destination(self, _which) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "curl"
            destination.mkdir()

            with self.assertRaises(DestinationAlreadyExistsError):
                GitRepositoryCloner().clone(self.repository, destination)

    @patch("git_repository_cloner.shutil.which", return_value=None)
    def test_reports_missing_git_during_initialization(self, _which) -> None:
        with self.assertRaises(GitNotAvailableError):
            GitRepositoryCloner()

    @patch("git_repository_cloner.subprocess.run")
    @patch("git_repository_cloner.shutil.which", return_value="/usr/bin/git")
    def test_reports_git_failure_and_removes_partial_directory(
        self,
        _which,
        run,
    ) -> None:
        def fail_clone(command, **_kwargs):
            Path(command[-1]).mkdir()
            return subprocess.CompletedProcess(
                command,
                128,
                "",
                "remote: Repository not found.\n",
            )

        run.side_effect = fail_clone

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "curl"
            with self.assertRaisesRegex(
                RepositoryNotFoundError,
                "verify the URL and credentials",
            ):
                GitRepositoryCloner(
                    partial_suffix_factory=lambda: "failed"
                ).clone(self.repository, destination)

            self.assertFalse((Path(directory) / ".curl.part-failed").exists())
            self.assertFalse(destination.exists())

    @patch("git_repository_cloner.subprocess.run")
    @patch("git_repository_cloner.shutil.which", return_value="/usr/bin/git")
    def test_classifies_transient_git_failure(self, _which, run) -> None:
        run.return_value = subprocess.CompletedProcess(
            [],
            128,
            "",
            "fatal: early EOF\n",
        )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TransientCloneError):
                GitRepositoryCloner().clone(
                    self.repository,
                    Path(directory) / "curl",
                )

    @patch("git_repository_cloner.subprocess.run")
    @patch("git_repository_cloner.shutil.which", return_value="/usr/bin/git")
    def test_reports_clone_timeout(self, _which, run) -> None:
        run.side_effect = subprocess.TimeoutExpired(
            cmd=["git", "clone"],
            timeout=5,
            stderr="network stalled",
        )

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(CloneTimeoutError, "timed out"):
                GitRepositoryCloner(timeout_seconds=5).clone(
                    self.repository,
                    Path(directory) / "curl",
                )


class _FlakyCloner:
    def __init__(self, transient_failures: int) -> None:
        self.transient_failures = transient_failures
        self.calls = 0

    def clone(self, _repository, _destination) -> None:
        self.calls += 1
        if self.calls <= self.transient_failures:
            raise TransientCloneError("temporary network error")


class RetryingRepositoryClonerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = GitRepository.from_url("https://github.com/curl/curl")

    def test_retries_transient_failures_with_exponential_backoff(self) -> None:
        delegate = _FlakyCloner(transient_failures=2)
        delays: list[float] = []
        cloner = RetryingRepositoryCloner(
            delegate,
            max_retries=2,
            base_delay_seconds=1,
            sleeper=delays.append,
            random_source=lambda: 0,
        )

        with self.assertLogs("git_repository_cloner", level="WARNING"):
            cloner.clone(self.repository, Path("curl"))

        self.assertEqual(3, delegate.calls)
        self.assertEqual([1, 2], delays)

    def test_does_not_retry_permanent_failure(self) -> None:
        class PermanentFailureCloner:
            calls = 0

            def clone(self, _repository, _destination) -> None:
                self.calls += 1
                raise CloneError("repository not found")

        delegate = PermanentFailureCloner()
        cloner = RetryingRepositoryCloner(delegate, max_retries=3)

        with self.assertRaises(CloneError):
            cloner.clone(self.repository, Path("curl"))

        self.assertEqual(1, delegate.calls)


if __name__ == "__main__":
    unittest.main()
