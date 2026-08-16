import unittest
from pathlib import Path

from config import MiningConfig


class MiningConfigTests(unittest.TestCase):
    def test_accepts_positive_clone_depth(self) -> None:
        config = MiningConfig(Path("repositories.txt"), Path("projects"), 2)

        self.assertEqual(2, config.clone_depth)

    def test_rejects_non_positive_clone_depth(self) -> None:
        with self.assertRaisesRegex(ValueError, "greater than zero"):
            MiningConfig(Path("repositories.txt"), Path("projects"), 0)

    def test_rejects_invalid_scaling_options(self) -> None:
        with self.assertRaisesRegex(ValueError, "workers"):
            MiningConfig(Path("repositories.txt"), Path("projects"), workers=0)
        with self.assertRaisesRegex(ValueError, "timeout"):
            MiningConfig(
                Path("repositories.txt"),
                Path("projects"),
                clone_timeout_seconds=0,
            )
        with self.assertRaisesRegex(ValueError, "batch_size"):
            MiningConfig(Path("repositories.txt"), Path("projects"), batch_size=0)


if __name__ == "__main__":
    unittest.main()
