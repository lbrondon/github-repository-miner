import io
import logging
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import main
from exceptions import CloneError
from repository import plan_repository_destinations
from repository_list_reader import RepositoryListReader


class MainTests(unittest.TestCase):
    def test_project_root_is_parent_of_source(self) -> None:
        expected_root = Path(main.__file__).resolve().parent.parent

        self.assertEqual(expected_root, main.PROJECT_ROOT)

    def test_default_repository_file_matches_project_input(self) -> None:
        args = main.build_parser().parse_args([])

        self.assertEqual(
            main.PROJECT_ROOT / "input" / "65_cs_repositories.txt",
            args.repositories,
        )
        self.assertEqual(
            main.PROJECT_ROOT / "output" / "projects",
            args.projects_dir,
        )
        self.assertEqual(
            main.PROJECT_ROOT / ".repository-miner" / "mining-state.sqlite3",
            args.state_file,
        )
        self.assertIsNone(args.log_file)

    def test_project_input_has_65_entries_and_current_bash_mirror(self) -> None:
        result = RepositoryListReader().scan(main.DEFAULT_REPOSITORIES_FILE)
        names = {repository.qualified_name for repository in result.repositories}

        self.assertEqual(65, len(result.repositories))
        self.assertEqual(0, len(result.issues))
        self.assertIn("github.com/mirror/bash", names)
        self.assertNotIn("github.com/bminor/bash", names)
        self.assertEqual(4, len(result.possible_mirror_groups))
        destinations = plan_repository_destinations(result.repositories)
        self.assertEqual(65, len(set(destinations.values())))
        self.assertTrue(
            all(len(destination.parts) == 1 for destination in destinations.values())
        )

    def test_resolves_relative_path_from_project_root(self) -> None:
        resolved = main.resolve_project_path(Path("input/repositories.txt"))

        self.assertEqual(
            main.PROJECT_ROOT / "input" / "repositories.txt",
            resolved,
        )

    def test_help_returns_success(self) -> None:
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main.build_parser().parse_args(["--help"])

        self.assertEqual(0, raised.exception.code)

    def test_scaling_defaults_are_bounded(self) -> None:
        args = main.build_parser().parse_args([])

        self.assertEqual(4, args.workers)
        self.assertEqual(1800.0, args.clone_timeout)
        self.assertEqual(2, args.max_retries)
        self.assertIsNone(args.batch_size)
        self.assertFalse(args.c_only)

    @patch("main.GitRepositoryCloner")
    def test_main_continues_with_valid_entries_and_resumes_from_state(
        self,
        cloner_type,
    ) -> None:
        def materialize(_repository, destination: Path) -> None:
            (destination / ".git").mkdir(parents=True)

        cloner_type.return_value.clone.side_effect = materialize
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories_file = root / "repositories.txt"
            repositories_file.write_text(
                "https://github.com/curl/curl\ninvalid-url\n",
                encoding="utf-8",
            )
            arguments = [
                "--repositories",
                str(repositories_file),
                "--projects-dir",
                str(root / "projects"),
                "--state-file",
                str(root / "state.sqlite3"),
                "--log-file",
                str(root / "miner.log"),
                "--workers",
                "2",
            ]

            console = io.StringIO()
            with redirect_stderr(console):
                first_exit_code = main.main(arguments)
                second_exit_code = main.main(arguments)
            log_content = (root / "miner.log").read_text(encoding="utf-8")
            console_content = console.getvalue()

        logging.shutdown()
        logging.basicConfig(handlers=[], force=True)
        self.assertEqual(0, first_exit_code)
        self.assertEqual(0, second_exit_code)
        self.assertEqual(1, cloner_type.return_value.clone.call_count)
        self.assertIn("Invalid input line 2", log_content)
        self.assertIn("already_completed=1", log_content)
        self.assertIn("[run=", log_content)
        self.assertIn("Clone started: repository=github.com/curl/curl", log_content)
        self.assertIn("status=succeeded repository=github.com/curl/curl", log_content)
        self.assertIn("checkout_mode=full", log_content)
        self.assertIn(
            "status=succeeded repository=github.com/curl/curl",
            console_content,
        )
        self.assertIn("Finished: status=success", console_content)

    @patch("main.GitRepositoryCloner")
    def test_main_materializes_all_65_as_direct_project_children(
        self,
        cloner_type,
    ) -> None:
        def materialize(_repository, destination: Path) -> None:
            (destination / ".git").mkdir(parents=True)

        cloner_type.return_value.clone.side_effect = materialize
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            projects_dir = root / "output" / "projects"
            with redirect_stderr(io.StringIO()):
                exit_code = main.main(
                    [
                        "--projects-dir",
                        str(projects_dir),
                        "--state-file",
                        str(root / "state.sqlite3"),
                    ]
                )
            project_directories = tuple(
                path for path in projects_dir.iterdir() if path.is_dir()
            )
            output_entry_names = tuple(
                sorted(path.name for path in (root / "output").iterdir())
            )
            all_have_git_metadata = all(
                (project / ".git").is_dir()
                for project in project_directories
            )

        logging.shutdown()
        logging.basicConfig(handlers=[], force=True)
        self.assertEqual(0, exit_code)
        self.assertEqual(65, len(project_directories))
        self.assertEqual(("projects",), output_entry_names)
        self.assertTrue(all_have_git_metadata)

    @patch("main.GitRepositoryCloner")
    def test_console_highlights_partial_failure_and_repository(
        self,
        cloner_type,
    ) -> None:
        cloner_type.return_value.clone.side_effect = CloneError("test failure")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories_file = root / "repositories.txt"
            repositories_file.write_text(
                "https://github.com/curl/curl\n",
                encoding="utf-8",
            )
            console = io.StringIO()

            with redirect_stderr(console):
                exit_code = main.main(
                    [
                        "--repositories",
                        str(repositories_file),
                        "--projects-dir",
                        str(root / "projects"),
                        "--state-file",
                        str(root / "state.sqlite3"),
                        "--log-file",
                        str(root / "miner.log"),
                    ]
                )

        logging.shutdown()
        logging.basicConfig(handlers=[], force=True)
        console_content = console.getvalue()
        self.assertEqual(1, exit_code)
        self.assertIn("Finished: status=partial_failure", console_content)
        self.assertIn("Repositories requiring attention", console_content)
        self.assertIn("github.com/curl/curl", console_content)

    @patch("main.GitRepositoryCloner")
    def test_keyboard_interrupt_returns_shell_interrupt_code(
        self,
        cloner_type,
    ) -> None:
        cloner_type.return_value.clone.side_effect = KeyboardInterrupt
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories_file = root / "repositories.txt"
            repositories_file.write_text(
                "https://github.com/curl/curl\n",
                encoding="utf-8",
            )

            with redirect_stderr(io.StringIO()):
                exit_code = main.main(
                    [
                        "--repositories",
                        str(repositories_file),
                        "--projects-dir",
                        str(root / "projects"),
                        "--state-file",
                        str(root / "state.sqlite3"),
                        "--log-file",
                        str(root / "miner.log"),
                    ]
                )
            log_content = (root / "miner.log").read_text(encoding="utf-8")

        logging.shutdown()
        logging.basicConfig(handlers=[], force=True)
        self.assertEqual(130, exit_code)
        self.assertIn("Execution interrupted by user", log_content)

    @patch("main.GitRepositoryCloner")
    def test_strict_input_stops_before_cloner_initialization(
        self,
        cloner_type,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repositories_file = root / "repositories.txt"
            repositories_file.write_text(
                "https://github.com/curl/curl\ninvalid-url\n",
                encoding="utf-8",
            )

            with redirect_stderr(io.StringIO()):
                exit_code = main.main(
                    [
                        "--repositories",
                        str(repositories_file),
                        "--log-file",
                        str(root / "miner.log"),
                        "--strict-input",
                    ]
                )

        logging.shutdown()
        logging.basicConfig(handlers=[], force=True)
        self.assertEqual(2, exit_code)
        cloner_type.assert_not_called()


if __name__ == "__main__":
    unittest.main()
