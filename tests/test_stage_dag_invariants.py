import unittest

from story_agent_runtime import (
    STAGE_BRANCHES,
    STAGE_ESTIMATES_MINUTES,
    STAGE_WRITE_SETS,
    STORY_STAGE_DEPENDENCIES,
    STORY_STAGE_SEQUENCE,
)


class StageDagInvariantTests(unittest.TestCase):
    def test_current_dag_is_complete_acyclic_and_metadata_bound(self) -> None:
        stages = tuple(STORY_STAGE_SEQUENCE)
        self.assertEqual(len(stages), 38)
        self.assertEqual(len(stages), len(set(stages)))
        self.assertEqual(set(STORY_STAGE_DEPENDENCIES), set(stages))

        positions = {stage: index for index, stage in enumerate(stages)}
        for stage, dependencies in STORY_STAGE_DEPENDENCIES.items():
            for dependency in dependencies:
                self.assertIn(dependency, positions)
                self.assertLess(positions[dependency], positions[stage])

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(stage: str) -> None:
            self.assertNotIn(stage, visiting, f"cycle includes {stage}")
            if stage in visited:
                return
            visiting.add(stage)
            for dependency in STORY_STAGE_DEPENDENCIES[stage]:
                visit(dependency)
            visiting.remove(stage)
            visited.add(stage)

        for stage in stages:
            visit(stage)
        self.assertEqual(visited, set(stages))

        self.assertEqual(set(STAGE_ESTIMATES_MINUTES), set(stages))
        self.assertEqual(set(STAGE_BRANCHES), set(stages))
        self.assertEqual(set(STAGE_WRITE_SETS), set(stages))
        self.assertIn("product_package_review", STORY_STAGE_DEPENDENCIES["release_preview"])


if __name__ == "__main__":
    unittest.main()
