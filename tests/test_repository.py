import unittest
from pathlib import Path

from exceptions import InvalidRepositoryUrlError
from repository import (
    GitRepository,
    RepositoryProvider,
    plan_repository_destinations,
)


class GitRepositoryTests(unittest.TestCase):
    def test_parses_github_https_url(self) -> None:
        repository = GitRepository.from_url("https://github.com/curl/curl")

        self.assertEqual(RepositoryProvider.GITHUB, repository.provider)
        self.assertEqual("github.com", repository.host)
        self.assertEqual("curl", repository.namespace)
        self.assertEqual("curl", repository.name)
        self.assertEqual("curl/curl", repository.full_name)
        self.assertEqual("https://github.com/curl/curl.git", repository.clone_url)
        self.assertEqual(
            Path("curl"),
            repository.destination_path,
        )
        self.assertEqual(
            Path("github.com/curl/curl"),
            repository.hierarchical_destination_path,
        )

    def test_parses_gitlab_instance_url(self) -> None:
        repository = GitRepository.from_url(
            "https://gitlab.gnome.org/GNOME/libxml2"
        )

        self.assertEqual(RepositoryProvider.GITLAB, repository.provider)
        self.assertEqual("gitlab.gnome.org", repository.host)
        self.assertEqual("GNOME", repository.namespace)
        self.assertEqual("libxml2", repository.name)
        self.assertEqual("gitlab.gnome.org/GNOME/libxml2", repository.qualified_name)
        self.assertEqual(
            "https://gitlab.gnome.org/GNOME/libxml2.git",
            repository.clone_url,
        )

    def test_accepts_gitlab_nested_groups(self) -> None:
        repository = GitRepository.from_url(
            "https://gitlab.com/group/subgroup/project"
        )

        self.assertEqual("group/subgroup", repository.namespace)
        self.assertEqual("group/subgroup/project", repository.full_name)

    def test_plans_direct_unique_directories_for_name_collisions(self) -> None:
        repositories = (
            GitRepository.from_url("https://github.com/GNOME/libxml2"),
            GitRepository.from_url(
                "https://gitlab.gnome.org/GNOME/libxml2"
            ),
            GitRepository.from_url("https://github.com/curl/curl"),
        )

        destinations = plan_repository_destinations(repositories)

        self.assertEqual(Path("curl"), destinations[repositories[2].identity_key])
        self.assertEqual(
            Path("libxml2--github.com"),
            destinations[repositories[0].identity_key],
        )
        self.assertEqual(
            Path("libxml2--gitlab.gnome.org"),
            destinations[repositories[1].identity_key],
        )
        self.assertTrue(
            all(len(destination.parts) == 1 for destination in destinations.values())
        )

    def test_removes_git_suffix_and_trailing_slash(self) -> None:
        repository = GitRepository.from_url(
            "https://github.com/OpenVPN/openvpn.git/"
        )

        self.assertEqual("OpenVPN/openvpn", repository.full_name)

    def test_rejects_unsupported_host(self) -> None:
        with self.assertRaises(InvalidRepositoryUrlError):
            GitRepository.from_url("https://bitbucket.org/curl/curl")

    def test_rejects_non_https_url(self) -> None:
        with self.assertRaises(InvalidRepositoryUrlError):
            GitRepository.from_url("http://github.com/curl/curl")

    def test_rejects_github_extra_path_segments(self) -> None:
        with self.assertRaises(InvalidRepositoryUrlError):
            GitRepository.from_url("https://github.com/curl/curl/tree/main")

    def test_rejects_gitlab_url_without_namespace(self) -> None:
        with self.assertRaises(InvalidRepositoryUrlError):
            GitRepository.from_url("https://gitlab.gnome.org/libxml2")

    def test_rejects_gitlab_web_navigation_url(self) -> None:
        with self.assertRaises(InvalidRepositoryUrlError):
            GitRepository.from_url(
                "https://gitlab.com/group/project/-/tree/main"
            )

    def test_rejects_malformed_gitlab_host(self) -> None:
        with self.assertRaises(InvalidRepositoryUrlError):
            GitRepository.from_url("https://gitlab..invalid/group/project")

    def test_direct_constructor_enforces_path_invariants(self) -> None:
        with self.assertRaises(InvalidRepositoryUrlError):
            GitRepository(
                provider=RepositoryProvider.GITHUB,
                host="github.com",
                namespace="safe",
                name="../outside",
            )


if __name__ == "__main__":
    unittest.main()
