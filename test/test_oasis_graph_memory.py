import os
import tempfile
import unittest
from unittest import mock

from oasis.forum import DiscussionForum, TimelineEvent
from oasis.graph_memory import GraphMemoryService
from oasis.swarm_engine import build_pending_swarm


class OasisGraphMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "oasis_graph_memory.db")
        self.env_patch = mock.patch.dict(
            os.environ,
            {
                "OASIS_GRAPHRAG_PROVIDER": "local",
                "OASIS_GRAPHRAG_DB_PATH": self.db_path,
            },
            clear=False,
        )
        self.env_patch.start()
        self.service = GraphMemoryService()
        await self.service.initialize()

        self.forum = DiscussionForum(topic_id="topic01", question="预测 AI 产品发布后的开发者与市场联动", user_id="alice")
        self.forum.swarm_mode = "prediction"
        self.forum.swarm = build_pending_swarm(self.forum.question, mode="prediction")

    async def asyncTearDown(self):
        self.env_patch.stop()
        self.tmpdir.cleanup()

    async def test_sync_blueprint_persists_living_graph_payload(self):
        payload = await self.service.sync_blueprint(self.forum, self.forum.swarm)

        self.assertEqual(payload["graphrag"]["provider"], "local")
        self.assertGreaterEqual(payload["graphrag"]["memory_count"], 1)
        self.assertTrue(any(node["type"] == "objective" for node in payload["graph"]["nodes"]))
        self.assertTrue(any(edge["source"] and edge["target"] for edge in payload["graph"]["edges"]))

    async def test_ingest_post_and_timeline_event_enrich_retrieval_graph(self):
        await self.service.sync_blueprint(self.forum, self.forum.swarm)

        self.forum.current_round = 2
        post = await self.forum.publish(author="Alice", content="开发者担心 pricing pressure，会拖慢 adoption。")
        await self.service.ingest_post(self.forum, post)
        await self.service.ingest_timeline_event(
            self.forum,
            TimelineEvent(seq=1, elapsed=6.2, event="agent_callback", agent="Alice", detail="raised pricing risk"),
        )

        retrieval = await self.service.retrieve(self.forum, "pricing adoption 为什么会放缓", limit=6)
        node_ids = {node["id"] for node in retrieval["graph"]["nodes"]}

        self.assertTrue(retrieval["evidence"])
        self.assertTrue(any(item["kind"] in {"memory", "node", "edge"} for item in retrieval["evidence"]))
        self.assertTrue(any(node_id.startswith("memory:post:") for node_id in node_ids))

    @mock.patch("oasis.swarm_engine.create_chat_model", side_effect=RuntimeError("boom"))
    async def test_ask_report_falls_back_to_graph_evidence_when_llm_fails(self, _mock_create_chat_model):
        await self.service.sync_blueprint(self.forum, self.forum.swarm)
        post = await self.forum.publish(author="Analyst", content="竞品降价会压缩新产品的留存和口碑扩散。")
        await self.service.ingest_post(self.forum, post)

        report = await self.service.ask_report(self.forum, "为什么当前预测偏保守？", limit=6)

        self.assertIn(report["confidence"], {"low", "medium", "high"})
        self.assertTrue(report["answer"])
        self.assertTrue(report["because"])
        self.assertTrue(report["evidence"])

    async def test_conclusion_node_keeps_full_text_for_graph_inspector(self):
        await self.service.sync_blueprint(self.forum, self.forum.swarm)

        self.forum.status = "concluded"
        self.forum.conclusion = (
            "Final Outlook\n"
            + "这是一段很长的结论文本。"
            + ("为了验证 Graph inspector 不再只显示截断摘要，" * 24)
        )
        await self.service.ingest_conclusion(self.forum)

        payload = await self.service.build_swarm_payload(self.forum)
        final_node = next(node for node in payload["graph"]["nodes"] if node["id"] == "scenario:final-outlook")

        self.assertEqual(final_node["meta"]["full_text"], self.forum.conclusion)
        self.assertEqual(final_node["full_text"], self.forum.conclusion)
        self.assertLess(len(final_node["summary"]), len(self.forum.conclusion))


if __name__ == "__main__":
    unittest.main()
