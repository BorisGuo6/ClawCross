import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import services.arxiv_collision_service as svc


ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2605.00001v1</id>
    <updated>2026-05-31T12:00:00Z</updated>
    <published>2026-05-31T12:00:00Z</published>
    <title>Layered World Models for Robot Manipulation with Wrist Cameras</title>
    <summary>We learn object contact layers and action-conditioned rollouts for tabletop robot manipulation.</summary>
    <author><name>A. Researcher</name></author>
    <arxiv:primary_category term="cs.RO" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.RO" scheme="http://arxiv.org/schemas/atom"/>
    <category term="cs.LG" scheme="http://arxiv.org/schemas/atom"/>
    <link href="http://arxiv.org/abs/2605.00001v1" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/2605.00001v1" rel="related" type="application/pdf"/>
  </entry>
</feed>
"""

RSS = """<?xml version='1.0' encoding='UTF-8'?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
  <channel>
    <item>
      <title>Layered World Models for Robot Manipulation (arXiv:2605.00001v1 [cs.RO])</title>
      <link>https://arxiv.org/abs/2605.00001v1</link>
      <description>&lt;p&gt;Object contact layers for robot manipulation.&lt;/p&gt;</description>
      <pubDate>Sun, 31 May 2026 04:00:00 +0000</pubDate>
      <dc:creator>A. Researcher, B. Builder</dc:creator>
    </item>
  </channel>
</rss>
"""


class ArxivCollisionAgentTests(unittest.TestCase):
    def test_parse_arxiv_atom_extracts_metadata(self):
        papers = svc.parse_arxiv_atom(ATOM)

        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].arxiv_id, "2605.00001")
        self.assertEqual(papers[0].primary_category, "cs.RO")
        self.assertIn("cs.LG", papers[0].categories)
        self.assertEqual(papers[0].authors, ["A. Researcher"])
        self.assertTrue(papers[0].pdf_url.endswith(".pdf"))

    def test_fetch_arxiv_rss_parses_items_from_fallback_feed(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return RSS.encode("utf-8")

        def fake_urlopen(_request, timeout=30):
            return FakeResponse()

        original = svc.urllib.request.urlopen
        svc.urllib.request.urlopen = fake_urlopen
        try:
            start, end = svc.date_window_utc("2026-05-31")
            papers = svc.fetch_arxiv_rss(category="cs.RO", start_dt=start, end_dt=end, max_results=5)
        finally:
            svc.urllib.request.urlopen = original

        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0].arxiv_id, "2605.00001")
        self.assertEqual(papers[0].authors, ["A. Researcher", "B. Builder"])
        self.assertIn("Object contact layers", papers[0].summary)

    def test_load_dashboard_projects_includes_project_and_task_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dashboard = self._write_dashboard(Path(tmpdir))

            projects = svc.load_dashboard_projects(dashboard)

            self.assertEqual({p.project_id for p in projects}, {"umi-world-model", "real-robot-infra"})
            umi = next(p for p in projects if p.project_id == "umi-world-model")
            self.assertIn("layer", umi.token_counts)
            self.assertIn("wrist", umi.token_counts)
            self.assertIn("contact", umi.token_counts)

    def test_find_collisions_scores_relevant_project_above_unrelated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dashboard = self._write_dashboard(Path(tmpdir))
            projects = svc.load_dashboard_projects(dashboard)
            paper = svc.parse_arxiv_atom(ATOM)[0]

            collisions = svc.find_collisions([paper], projects, threshold=0.12)

            self.assertTrue(collisions)
            self.assertEqual(collisions[0].project.project_id, "umi-world-model")
            self.assertGreater(collisions[0].score, 0.12)
            self.assertIn("manipulation", collisions[0].overlap_terms)

    def test_update_collision_state_dedupes_seen_collisions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dashboard = self._write_dashboard(Path(tmpdir) / "dashboard")
            projects = svc.load_dashboard_projects(dashboard)
            paper = svc.parse_arxiv_atom(ATOM)[0]
            collisions = svc.find_collisions([paper], projects, threshold=0.12)
            state_path = Path(tmpdir) / "state.json"

            first, _ = svc.update_collision_state(collisions, state_path=state_path)
            second, _ = svc.update_collision_state(collisions, state_path=state_path)

            self.assertEqual(len(first), 1)
            self.assertEqual(second, [])

    def test_render_report_contains_project_and_arxiv_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dashboard = self._write_dashboard(Path(tmpdir))
            projects = svc.load_dashboard_projects(dashboard)
            paper = svc.parse_arxiv_atom(ATOM)[0]
            collisions = svc.find_collisions([paper], projects, threshold=0.12)
            report = svc.render_report(
                start_dt=svc.date_window_utc("2026-05-31")[0],
                end_dt=svc.date_window_utc("2026-05-31")[1],
                category="cs.RO",
                papers=[paper],
                projects=projects,
                collisions=collisions,
                new_collisions=collisions,
                threshold=0.12,
            )

            self.assertIn("arXiv Robotics Collision Report", report)
            self.assertIn("Tri-View Layered Manipulation World Model", report)
            self.assertIn("http://arxiv.org/abs/2605.00001v1", report)

    def _write_dashboard(self, root: Path) -> Path:
        dashboard = root / "dashboard" if root.name != "dashboard" else root
        projects_dir = dashboard / "state" / "projects"
        projects_dir.mkdir(parents=True)
        (dashboard / "state" / "portfolio.json").write_text(
            json.dumps(
                {
                    "projects": [
                        {
                            "project_id": "umi-world-model",
                            "title": "Tri-View Layered Manipulation World Model",
                            "bucket": "research",
                            "status": "ongoing",
                            "state_path": "dashboard/state/projects/umi-world-model.json",
                        },
                        {
                            "project_id": "real-robot-infra",
                            "title": "Real-Robot Lab Infra",
                            "bucket": "engineering",
                            "status": "ongoing",
                            "state_path": "dashboard/state/projects/real-robot-infra.json",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        (projects_dir / "umi-world-model.json").write_text(
            json.dumps(
                {
                    "project_id": "umi-world-model",
                    "title": "Tri-View Layered Manipulation World Model",
                    "description": "Layered robot manipulation world model with wrist cameras, D435, contact heads, reward evaluation.",
                    "summary": "Action-conditioned view and layer rollouts for tabletop manipulation.",
                    "references": [{"title": "iWorld-Bench", "notes": "interactive world models"}],
                }
            ),
            encoding="utf-8",
        )
        (projects_dir / "real-robot-infra.json").write_text(
            json.dumps(
                {
                    "project_id": "real-robot-infra",
                    "title": "Real-Robot Lab Infra",
                    "description": "Franka, Wuji glove, camera brackets, hardware inventory.",
                    "summary": "Physical robot lab setup and data collection wrappers.",
                }
            ),
            encoding="utf-8",
        )
        (dashboard / "state" / "tasks.json").write_text(
            json.dumps(
                {
                    "tasks": [
                        {
                            "task_id": "task_umi_layer_heads",
                            "project_id": "umi-world-model",
                            "title": "Implement object contact layer heads",
                            "description": "Evaluate contact consistency for manipulation world models.",
                            "status": "todo",
                        },
                        {
                            "task_id": "task_robot_inventory",
                            "project_id": "real-robot-infra",
                            "title": "Update hardware asset table",
                            "description": "Keep robot and glove hardware inventory current.",
                            "status": "todo",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        return dashboard


if __name__ == "__main__":
    unittest.main()
