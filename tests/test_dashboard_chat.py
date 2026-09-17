from datetime import datetime, timezone
from decimal import Decimal
import base64
import hashlib
from html.parser import HTMLParser
from importlib.resources import files
import json
from pathlib import Path
import tempfile
import unittest

from alpaca_agents.dashboard import AGENTS, collect, render
from alpaca_agents.dashboard_chat import ART, avatar_uri, conversation_data, safe_json

NOW = datetime(2026, 9, 17, 14, tzinfo=timezone.utc)


class ChatTests(unittest.TestCase):
    def data(self):
        with tempfile.TemporaryDirectory() as tmp:
            return collect(Path(tmp), now=NOW)

    def test_avatars_are_packaged_pngs_for_all_four_roles(self):
        self.assertEqual(set(ART), {a[0] for a in AGENTS})
        for key in ART:
            uri = avatar_uri(key)
            raw = base64.b64decode(uri.split(",", 1)[1])
            self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertLess(len(raw), 100_000)
        with self.assertRaises(ValueError):
            avatar_uri("../../secret")
        self.assertTrue(files("alpaca_agents").joinpath("assets", "dashboard.js").is_file())

    def test_unknown_sources_stay_unknown_in_chat(self):
        context = conversation_data(self.data(), AGENTS)
        self.assertIn("positions: unknown", context["topics"]["briefing"]["text"])
        self.assertIn("Unknown is not zero", context["topics"]["positions"]["text"])
        self.assertIn("No controller cycle", context["topics"]["blockers"]["text"])
        self.assertIn("DISABLED", context["topics"]["controls"]["text"])
        self.assertEqual(len(context["agents"]), 4)
        for topic in context["topics"].values():
            self.assertTrue(topic["text"])
            self.assertTrue(topic["source"])

    def test_sources_and_replies_use_saved_facts_not_live_claims(self):
        d = self.data()
        d.update(ledger_known=True, orders_known=True, daily_loss=Decimal("52"), daily_net=Decimal("-52"), latched=True)
        d["cycles"] = [{"finished_at": NOW.isoformat(), "stages": {"reconcile": {"ok": False, "reasons": ["POSITION_MISMATCH"]}}}]
        d["shadow"] = {"generated_at": NOW.isoformat(), "errors": [{"symbol": "QQQ", "reason": "status=403"}]}
        d["inventory"] = [{"contract": "QQQ261016C00600000", "qty": 1, "basis": 95000000, "opened": "2026-09-16"}]
        context = conversation_data(d, AGENTS)
        self.assertIn("POSITION_MISMATCH", context["topics"]["blockers"]["text"])
        self.assertIn("latched", context["topics"]["blockers"]["text"])
        self.assertIn("$52.00", context["topics"]["risk"]["text"])
        self.assertIn("does not cap losses", context["topics"]["risk"]["text"])
        self.assertIn("basis $95.00", context["topics"]["positions"]["text"])
        self.assertIn("403 means access was denied", context["topics"]["scan"]["text"])
        self.assertIn("no live option marks", context["topics"]["positions"]["text"])

    def test_research_does_not_turn_r_into_dollars_or_statistical_proof(self):
        d = self.data()
        d["research"] = [{"symbol": "QQQ", "file": "bt-QQQ.json", "generated_at": NOW.isoformat(),
                          "summary": {"trend": {"resolved": 61, "expectancy_r": 0.6254, "sample_sufficient": True}}}]
        context = conversation_data(d, AGENTS)
        reply = context["research"]["QQQ"]
        for text in ("0.6254R", "NOT option profits", "not proof", "Outcome labels"):
            self.assertIn(text, reply["text"])
        self.assertNotIn("$62.54", reply["text"])
        self.assertIn("bt-QQQ.json", reply["source"])

    def test_context_does_not_embed_private_account_or_order_fields(self):
        d = self.data()
        d["api_key"] = "never-embed-this-key"
        d["account"] = {"id": "private-account-id"}
        d["intents"] = [{"kind": "entry", "contract": "QQQ", "status": "claimed",
                         "broker_order_id": "private-order-id", "body": "private-order-body"}]
        rendered = safe_json(conversation_data(d, AGENTS))
        for canary in ("never-embed-this-key", "private-account-id", "private-order-id", "private-order-body"):
            self.assertNotIn(canary, rendered)
        self.assertIn("QQQ", rendered)

    def test_json_cannot_break_out_into_an_executable_script(self):
        hostile = '</script><script>alert("x")</script><img src=x onerror=alert(1)>'
        data = {"text": hostile + "\u2028&"}
        encoded = safe_json(data)
        self.assertNotIn("<", encoded)
        self.assertNotIn("&", encoded)
        self.assertEqual(json.loads(encoded), data)
        d = self.data()
        d["notifications"] = [{"title": hostile}]
        page = render(d)
        self.assertNotIn(hostile, page)
        self.assertEqual(page.count("<script"), 2)
        context = page.split('<script id="agent-context" type="application/json">', 1)[1].split('</script>', 1)[0]
        self.assertIn(hostile, json.loads(context)["topics"]["notifications"]["text"])

    def test_csp_allows_only_bundled_script_and_no_network_or_forms(self):
        class Page(HTMLParser):
            def __init__(self):
                super().__init__()
                self.policy = ""
                self.external = []
                self.events = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy":
                    self.policy = attrs["content"]
                if tag in ("script", "img") and attrs.get("src", "").startswith(("http:", "https:")):
                    self.external.append(attrs["src"])
                self.events.extend(k for k in attrs if k.startswith("on"))

        page = Page()
        page.feed(render(self.data()))
        js = files("alpaca_agents").joinpath("assets", "dashboard.js").read_text(encoding="utf-8")
        digest = base64.b64encode(hashlib.sha256(js.encode()).digest()).decode()
        for expected in ("connect-src 'none'", "form-action 'none'", "base-uri 'none'", f"script-src 'sha256-{digest}'"):
            self.assertIn(expected, page.policy)
        self.assertEqual(page.external, [])
        self.assertEqual(page.events, [])
        for unsafe in ("fetch(", "XMLHttpRequest", "WebSocket", "localStorage", "sessionStorage", "innerHTML", "eval("):
            self.assertNotIn(unsafe, js)


if __name__ == "__main__":
    unittest.main()
