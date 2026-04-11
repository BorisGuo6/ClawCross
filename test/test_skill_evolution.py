import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import webot.skill_evolution as skill_evolution
import webot.skills as webot_skills
import webot.trajectory as trajectory


class SkillEvolutionTests(unittest.TestCase):
    def test_analyze_and_apply_skill_evolution_from_failed_trajectory(self):
        with TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            original_skills_user_files = webot_skills.USER_FILES_DIR
            original_evolution_user_files = skill_evolution.USER_FILES_DIR
            original_trajectory_dir = trajectory.DATA_DIR
            webot_skills.USER_FILES_DIR = tmp_root / "user_files"
            skill_evolution.USER_FILES_DIR = tmp_root / "user_files"
            trajectory.DATA_DIR = tmp_root / "data" / "trajectories"

            try:
                content = """---
name: ops-playbook
description: Startup and test workflow
---

# Ops Playbook

Always start from the repo root.
"""
                created = webot_skills.create_skill("alice", name="ops-playbook", content=content)
                self.assertTrue(created["success"])

                trajectory.save_trajectory(
                    user_id="alice",
                    session_id="default",
                    messages=[
                        {"role": "user", "content": "Run the regression check."},
                        {"role": "assistant", "content": "pytest timed out after the import step."},
                    ],
                    model="gpt-test",
                    completed=False,
                    metadata={
                        "command": "pytest test/test_skill_evolution.py",
                        "error": "TimeoutError: pytest timed out after import. ModuleNotFoundError: app",
                        "stderr": "TimeoutError: pytest timed out after import. ModuleNotFoundError: app",
                    },
                )

                digest_path = tmp_root / "user_files" / "alice" / "skill_evolution" / "latest_failure_digest.md"
                self.assertTrue(digest_path.is_file())

                report = skill_evolution.analyze_skill_evolution(
                    "alice",
                    name="ops-playbook",
                    command="pytest test/test_skill_evolution.py",
                )
                self.assertTrue(report["success"])
                self.assertGreaterEqual(report["failure_count"], 1)
                self.assertTrue(report["frontier"])
                self.assertIn("summary", report)

                applied = skill_evolution.apply_skill_evolution(
                    "alice",
                    name="ops-playbook",
                    command="pytest test/test_skill_evolution.py",
                    error_text="TimeoutError: pytest timed out after import. ModuleNotFoundError: app",
                    source="unit-test",
                )
                self.assertTrue(applied["success"])
                self.assertTrue(Path(applied["path"]).is_file())
                self.assertTrue(Path(applied["latest_report_path"]).is_file())
                self.assertTrue(Path(applied["frontier_path"]).is_file())

                updated_skill = webot_skills.get_skill("alice", name="ops-playbook")
                self.assertIn(skill_evolution.EVOLUTION_BEGIN, updated_skill["content"])
                self.assertIn("Self-Evolution Loop", updated_skill["content"])
                self.assertIn("Latest Trigger Command", updated_skill["content"])
            finally:
                webot_skills.USER_FILES_DIR = original_skills_user_files
                skill_evolution.USER_FILES_DIR = original_evolution_user_files
                trajectory.DATA_DIR = original_trajectory_dir

    def test_update_markdown_skill_document_writes_managed_block_and_report(self):
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            (repo_root / ".git").mkdir(parents=True, exist_ok=True)
            skill_path = repo_root / "SKILL.md"
            skill_path.write_text(
                "# Demo Skill\n\nThis is a repo-level skill document.\n",
                encoding="utf-8",
            )

            result = skill_evolution.update_markdown_skill_document(
                skill_path=skill_path,
                command="pytest test/test_demo.py",
                stderr="ModuleNotFoundError: demo_app",
                exit_code=1,
            )

            self.assertTrue(result["success"])
            self.assertTrue(result["updated"])
            updated = skill_path.read_text(encoding="utf-8")
            self.assertIn(skill_evolution.EVOLUTION_BEGIN, updated)
            self.assertIn("Self-Evolution Loop", updated)
            self.assertTrue(Path(result["report_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
