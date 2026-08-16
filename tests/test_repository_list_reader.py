import tempfile
import unittest
from pathlib import Path

from exceptions import RepositoryListError
from repository import plan_repository_destinations
from repository_list_reader import RepositoryListReader


class RepositoryListReaderTests(unittest.TestCase):
    def _write_list(self, directory: str, content: str) -> Path:
        file_path = Path(directory) / "repositories.txt"
        file_path.write_text(content, encoding="utf-8")
        return file_path

    def test_ignores_comments_blank_lines_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_path = self._write_list(
                directory,
                "# projects\n\nhttps://github.com/curl/curl\n"
                "https://github.com/CURL/CURL.git\n"
                "https://gitlab.gnome.org/GNOME/libxml2\n",
            )

            repositories = RepositoryListReader().read(file_path)

        self.assertEqual(
            ["github.com/curl/curl", "gitlab.gnome.org/GNOME/libxml2"],
            [repository.qualified_name for repository in repositories],
        )

    def test_scan_keeps_valid_repositories_and_reports_invalid_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_path = self._write_list(
                directory,
                "https://github.com/curl/curl\n"
                "not-a-url\n"
                "https://github.com/CURL/CURL.git\n",
            )

            result = RepositoryListReader().scan(file_path)

        self.assertEqual(1, len(result.repositories))
        self.assertEqual(1, len(result.issues))
        self.assertEqual(2, result.issues[0].line_number)
        self.assertEqual(1, result.duplicate_count)

    def test_reports_invalid_line_number(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_path = self._write_list(
                directory,
                "https://github.com/curl/curl\nnot-a-url\n",
            )

            with self.assertRaisesRegex(RepositoryListError, "line 2"):
                RepositoryListReader().read(file_path)

    def test_rejects_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_path = self._write_list(directory, "# no repositories\n")

            with self.assertRaisesRegex(RepositoryListError, "no repository URLs"):
                RepositoryListReader().read(file_path)

    def test_same_name_from_different_owners_has_unique_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_path = self._write_list(
                directory,
                "https://github.com/owner-one/shared\n"
                "https://github.com/owner-two/shared\n",
            )

            repositories = RepositoryListReader().read(file_path)

        self.assertEqual(2, len(repositories))
        destinations = plan_repository_destinations(repositories)
        self.assertEqual(2, len(set(destinations.values())))
        self.assertTrue(
            all(len(destination.parts) == 1 for destination in destinations.values())
        )

    def test_reports_possible_cross_host_mirrors_without_dropping_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            file_path = self._write_list(
                directory,
                "https://github.com/GNOME/libxml2\n"
                "https://gitlab.gnome.org/GNOME/libxml2\n",
            )

            result = RepositoryListReader().scan(file_path)

        self.assertEqual(2, len(result.repositories))
        self.assertEqual(1, len(result.possible_mirror_groups))
        self.assertEqual(
            "GNOME/libxml2",
            result.possible_mirror_groups[0].logical_name,
        )


if __name__ == "__main__":
    unittest.main()
