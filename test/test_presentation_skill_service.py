import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import services.presentation_skill_service as service


class PresentationSkillServiceTests(unittest.TestCase):
    def test_catalog_lists_all_upstream_sources(self):
        catalog = service.presentation_skill_catalog()

        self.assertEqual(catalog["version"], "2026-06-21")
        self.assertEqual(len(catalog["sources"]), 7)
        source_ids = {item["id"] for item in catalog["sources"]}
        self.assertIn("baoyu-slide-deck", source_ids)
        self.assertIn("ppt-master", source_ids)
        self.assertTrue(all(item["github"].startswith("https://github.com/") for item in catalog["sources"]))

    def test_scaffold_routes_pptx_to_native_powerpoint_sources(self):
        scaffold = service.build_presentation_scaffold(
            "Robotics weekly update",
            audience="research team",
            format="pptx",
            constraints="editable PowerPoint",
        )

        recommended = [item["id"] for item in scaffold["recommended_sources"]]
        self.assertEqual(recommended[0], "ppt-master")
        self.assertEqual(scaffold["format"], "pptx")
        self.assertIn("deck.pptx", scaffold["artifact_schema"]["pptx_output"])
        self.assertIn("Robotics weekly update", scaffold["agent_prompt"])

    def test_text_formatter_distinguishes_scaffold_from_catalog(self):
        scaffold = service.build_presentation_scaffold("Robotics weekly update", format="pptx")
        text = service.format_presentation_result(scaffold)

        self.assertTrue(text.startswith("Presentation scaffold:"))
        self.assertIn("ppt-master", text)

    def test_generated_skill_markdown_has_valid_frontmatter_and_sources(self):
        content = service.build_presentation_skill_markdown()

        self.assertTrue(content.startswith("---\nname: ai-presentation-maker"))
        self.assertIn("description:", content)
        self.assertIn("category: presentation", content)
        self.assertIn("https://github.com/op7418/guizang-ppt-skill", content)
        self.assertIn("https://github.com/hugohe3/ppt-master/tree/main", content)

    def test_install_uses_temp_skill_store_and_is_idempotent(self):
        import webot.skills as webot_skills

        original_user_files_dir = webot_skills.USER_FILES_DIR
        with tempfile.TemporaryDirectory() as tmp:
            webot_skills.USER_FILES_DIR = Path(tmp)
            try:
                result = service.install_presentation_skill("alice")
                self.assertTrue(result["success"])
                self.assertTrue(result["created"])
                self.assertTrue(Path(result["path"]).is_file())

                second = service.install_presentation_skill("alice")
                self.assertTrue(second["success"])
                self.assertFalse(second["created"])
                self.assertFalse(second["updated"])

                updated = service.install_presentation_skill("alice", overwrite=True)
                self.assertTrue(updated["success"])
                self.assertFalse(updated["created"])
                self.assertTrue(updated["updated"])
            finally:
                webot_skills.USER_FILES_DIR = original_user_files_dir


if __name__ == "__main__":
    unittest.main()
