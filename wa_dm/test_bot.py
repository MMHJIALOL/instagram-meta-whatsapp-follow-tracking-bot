"""Regression coverage for money-related answers and duplicate/pause/send boundaries."""
import copy
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ["WA_DM_NO_BRIDGE"] = "1"
from fastapi.testclient import TestClient
from wa_dm import app as backend
from wa_dm.replies import DEFAULTS, plan_reply


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(DEFAULTS)

    def test_hinglish_price_gets_exact_fee_and_qr(self):
        for question in ("fees kitni hai?", "kitne ka hai", "price please", "how much?", "फीस क्या है?"):
            with self.subTest(question=question):
                answer = plan_reply(question, self.cfg)
                self.assertIn("price", answer["intents"])
                self.assertIn("₹99", answer["actions"][0]["body"])
                self.assertEqual(answer["actions"][1]["asset"], "payment.jpg")

    def test_kit_total_does_not_replace_workshop_price(self):
        answer = plan_reply("kit price aur shipping?", self.cfg)
        self.assertIn("₹1200", answer["actions"][0]["body"])
        self.assertIn("₹1299", answer["actions"][0]["body"])
        self.assertIn("₹1200", answer["actions"][1]["body"])

    def test_multi_question_is_answered_in_one_text(self):
        answer = plan_reply("fees kitni hai aur material kya chahiye?", self.cfg)
        self.assertEqual(set(answer["intents"]), {"price", "materials"})
        self.assertIn("purchase karne ke baad", answer["actions"][0]["body"])
        self.assertNotIn("300 g", answer["actions"][0]["body"])
        self.assertEqual(len(answer["actions"]), 2)

    def test_multiple_facts_and_unknown_policies(self):
        answer = plan_reply("class kab hai aur recording milegi?", self.cfg)
        self.assertEqual(set(answer["intents"]), {"schedule", "recording"})
        self.assertIn("3:00 PM IST", answer["actions"][0]["body"])
        self.assertIn("recording", answer["actions"][0]["body"].lower())
        self.assertTrue(plan_reply("refund milega?", self.cfg)["handoff"])

    def test_payment_claim_is_not_verified_and_negation_is_not_paid(self):
        for text in ("I paid", "payment ho gayi", "pay kar diya"):
            answer = plan_reply(text, self.cfg)
            self.assertTrue(answer["payment_review"])
            self.assertIn("verify", answer["actions"][0]["body"])
            self.assertEqual(len(answer["actions"]), 1)
        for text in ("payment nahi hui", "payment pending", "not paid", "payment failed"):
            self.assertFalse(plan_reply(text,self.cfg)["payment_review"])

    def test_payment_caption_does_not_send_the_qr_again(self):
        answer = plan_reply("payment screenshot", self.cfg, kind="image", has_media=True)
        self.assertTrue(answer["payment_review"])
        self.assertEqual(len(answer["actions"]), 1)

    def test_unknown_and_voice_are_not_invented_answers(self):
        self.assertEqual(plan_reply("Can I use xyz resin brand?",self.cfg)["intents"], ["unknown"])
        for has_media in (False, True):
            self.assertEqual(plan_reply("",self.cfg,kind="ptt",has_media=has_media)["intents"], ["audio"])

    def test_general_details_request_gets_workshop_overview(self):
        for question in ("workshop details", "class info please", "send information"):
            self.assertEqual(plan_reply(question,self.cfg)["intents"], ["overview"])

    def test_ad_prefill_and_info_variants_get_details_without_team_alert(self):
        for question in (
            "Link:\n\n\nHello! Can I get more info on this?",
            "Link: https://example.com/ad\n\nHello! Can I get more info on this?",
            "Link:\nhttps://example.com/ad\nHello! Can I get more info on this?",
            "Hello! Can I get more info on this?", "Can I get more information about this?",
            "Hi, please share workshop details", "could you send me details", "tell me more about this",
            "workshop ki details chahiye", "iske baare mein batao", "details please",
        ):
            with self.subTest(question=question):
                reply=plan_reply(question,self.cfg)
                self.assertEqual(reply["intents"],["overview"])
                self.assertFalse(reply["handoff"])
                self.assertEqual(len(reply["actions"]),1)
                text=reply["actions"][0]["body"]
                for fact in ("25 October 2026", "3:00 PM IST", "3 hours", "₹99", "recording", "purchase ke baad"):
                    self.assertIn(fact,text)

    def test_ad_context_does_not_hide_real_joining_or_policy_requests(self):
        for question in ("joining link nahi mila", "Link:\njoining link nahi mila", "Link:"):
            self.assertIn("joining",plan_reply(question,self.cfg)["intents"])
        reply=plan_reply("Hello! Can I get more info on this? Aur certificate milega?",self.cfg)
        self.assertIn("overview",reply["intents"])
        self.assertIn("certificate",reply["intents"])
        self.assertTrue(reply["handoff"])
        followup=plan_reply("haan",self.cfg,last_intent="overview")
        self.assertEqual(followup["intents"],["registration"])
        self.assertEqual(followup["actions"][1]["asset"],"payment.jpg")

    def test_saved_values_are_used(self):
        self.cfg["workshop_fee"] = 149
        self.assertIn("₹149",plan_reply("price",self.cfg)["actions"][0]["body"])

    def test_expanded_question_and_typo_matrix(self):
        examples = {
            "main beginner hu join kar sakti hu?": "beginner",
            "begginer friendly hai?": "beginner", "recoding milegi?": "recording",
            "fess kitni hai": "price", "prce please": "price",
            "kit compulsory hai?": "kit_optional", "apne materials use kar sakte hain?": "kit_optional",
            "99 mein kit included hai?": "kit_included", "kit free hai?": "kit",
            "dono ka total kitna hai?": "total", "99 mein kya milega?": "included",
            "workshop free ha kya": "free", "syllabus kya hai": "learn",
            "certificate milega?": "certificate", "class hindi mein hai?": "language",
            "phone se join kar sakte hain?": "platform", "zoom link nahi mila": "joining",
            "payment failed": "payment_issue", "paise kat gaye": "payment_issue",
            "installment allowed?": "payment_terms", "can I pay later?": "payment_terms",
            "recording lifetime hai?": "recording_access", "recording download kar sakte?": "recording_access",
            "kit delivery kab hogi?": "delivery", "extra discount milega?": "discount",
            "kids join kar sakte hain?": "age", "can I attend from Dubai?": "international",
            "business kaise start karu": "business", "official website kya hai": "website",
        }
        for question, intent in examples.items():
            with self.subTest(question=question):
                self.assertIn(intent, plan_reply(question,self.cfg)["intents"])

    def test_material_policy_and_pdf_cannot_bypass_purchase(self):
        for question in ("material list", "matrial list please", "kit mein kya kya milega", "how much resin", "सामग्री क्या चाहिए?"):
            answer=plan_reply(question,self.cfg)
            self.assertIn("materials",answer["intents"])
            self.assertIn("purchase",answer["actions"][0]["body"])
            self.assertFalse(any(a.get("kind")=="document" for a in answer["actions"]))
            self.assertNotIn("300 g",str(answer))
        for status in ("new", "payment_review"):
            self.assertEqual(plan_reply("pdf bhejo",self.cfg,payment_status=status)["intents"],["pdf_gate"])
        self.assertEqual(plan_reply("pdf bhejo",self.cfg,payment_status="paid")["actions"][1]["asset"],"workshop.pdf")

    def test_context_followups_and_partial_unknown_are_escalated(self):
        self.assertEqual(plan_reply("haan",self.cfg,last_intent="materials")["intents"],["materials"])
        self.assertEqual(plan_reply("how much?",self.cfg,last_intent="kit")["intents"],["kit"])
        for question in ("Can I use xyz resin brand?", "fees aur certificate milega?", "price? can I use silicone on wood?"):
            self.assertTrue(plan_reply(question,self.cfg)["handoff"])
        self.assertFalse(plan_reply("recording free milegi?",self.cfg)["handoff"])
        self.assertNotIn("price",plan_reply("how much time is the class?",self.cfg)["intents"])

    def test_smalltalk_reactions_and_noise_do_not_ping_the_team(self):
        for question in ("kaise ho aap", "aap kaise ho?", "how are you?", "hello how are you", "kese ho", "kya haal hai", "aur batao", "sab badhiya", "I am fine", "hmmm", "theek hai", "hi mam", "hello sir", "thankyou", "thanks bro", "wow", "nice", "good night", "bye", "👍", "❤️", "???", "test", "testing", "asdf", "qwerty", "aaaaa", "tell me a joke", "you are stupid", "who are you", "aap kaun ho", "bot ho kya", "what is your age", "are you human", "love you"):
            with self.subTest(question=question):
                reply=plan_reply(question,self.cfg)
                self.assertFalse(reply["handoff"])
                self.assertEqual(len(reply["actions"]),1)
                self.assertNotIn("unknown",reply["intents"])
        for question in ("kaise ho aap? fees kitni hai?", "price aur kaise ho aap?"):
            self.assertFalse(plan_reply(question,self.cfg)["handoff"])
        for question in ("kaise ho aap? can I use xyz resin brand?", "thanks but refund chahiye", "hi certificate milega?"):
            self.assertTrue(plan_reply(question,self.cfg)["handoff"])


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data = backend.DATA
        backend.DATA = Path(self.tmp.name)
        self.client = TestClient(backend.app)
        self.client.__enter__()
        backend.bridge_state.update(state="ready",me=DEFAULTS["expected_number"],at=time.time(),qr="",detail="test")
        cfg = backend.config()
        cfg["enabled"] = True
        with backend.db() as c:
            c.execute("UPDATE settings SET value=? WHERE id=1",(json.dumps(cfg),))
        self.internal = {"authorization":"Bearer "+backend.TOKEN}
        self.admin = {"X-Requested-With":"kalakriti-dashboard"}

    def tearDown(self):
        self.client.__exit__(None,None,None)
        backend.DATA = self.old_data
        self.tmp.cleanup()

    def incoming(self, **overrides):
        body = dict(id="in-1", chat_id="919999999999@c.us",name="Test customer",phone="919999999999",body="price?",timestamp=time.time(),kind="chat")
        body.update(overrides)
        r = self.client.post("/internal/incoming",json=body,headers=self.internal)
        self.assertEqual(r.status_code,200,r.text)
        return r.json()

    def test_duplicate_message_only_plans_once(self):
        first = self.incoming()
        self.assertEqual(len(first["actions"]),2)
        self.assertEqual(self.incoming()["actions"],[])
        with backend.db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM outbox").fetchone()[0],2)

    def test_existing_chat_uses_recorded_order_despite_phone_clock_skew(self):
        first = self.incoming(timestamp=time.time()+4)["actions"]
        for action in first:
            self.client.post("/internal/claim",json={"id":action["id"]},headers=self.internal)
            self.client.post("/internal/result",json={"id":action["id"],"status":"sent"},headers=self.internal)
        self.incoming(id="in-2",body="thanks",timestamp=time.time()+3)
        chat = self.client.get("/api/chat",params={"chat_id":"919999999999@c.us"}).json()
        self.assertEqual([m["id"] for m in chat["messages"]], ["in-1",first[0]["id"],first[1]["id"],"in-2"])
        # Original timestamps are retained, even when they contradict causal order.
        self.assertGreater(chat["messages"][0]["at"], chat["messages"][1]["at"])
        self.assertEqual([m["sequence"] for m in chat["messages"]], sorted(m["sequence"] for m in chat["messages"]))

    def test_same_second_and_manual_messages_keep_their_recorded_positions(self):
        self.incoming()
        self.client.post("/internal/manual",json={"id":"phone-1","chat_id":"919999999999@c.us","body":"Let me help","timestamp":time.time()-2},headers=self.internal)
        self.incoming(id="in-2",body="thanks")
        with backend.db() as c:
            c.execute("UPDATE messages SET at=1000")
        messages = self.client.get("/api/chat",params={"chat_id":"919999999999@c.us"}).json()["messages"]
        self.assertEqual([m["id"] for m in messages], ["in-1","phone-1","in-2"])

    def test_team_alert_is_separate_from_chat_and_latest_preview(self):
        actions = self.incoming(body="certificate milega?")["actions"]
        for action in actions:
            self.client.post("/internal/claim",json={"id":action["id"]},headers=self.internal)
            self.client.post("/internal/result",json={"id":action["id"],"status":"sent"},headers=self.internal)
        chat = self.client.get("/api/chat",params={"chat_id":"919999999999@c.us"}).json()
        self.assertEqual([m["direction"] for m in chat["messages"]],["in","out"])
        self.assertEqual(len(chat["team_alerts"]),1)
        # A new contact followed by a late reply in the first chat must reorder the list.
        self.incoming(id="other",chat_id="918888888888@c.us",phone="918888888888")
        self.client.post("/internal/manual",json={"id":"phone-1","chat_id":"919999999999@c.us","body":"I can help","timestamp":time.time()},headers=self.internal)
        rows = self.client.get("/api/chats").json()
        self.assertEqual(rows[0]["chat_id"],"919999999999@c.us")
        self.assertEqual(rows[0]["last_message"],"I can help")
        self.assertEqual(rows[0]["last_direction"],"manual")
        with backend.db() as c:
            c.execute("DELETE FROM messages WHERE id='phone-1'")
        original = next(c for c in self.client.get("/api/chats").json() if c["chat_id"]=="919999999999@c.us")
        self.assertEqual(original["last_message"], actions[0]["body"])

    def test_chat_returns_latest_150_in_recorded_order(self):
        self.incoming()
        with backend.db() as c:
            c.executemany("INSERT INTO messages(id,chat_id,direction,body,at) VALUES (?,?,?,?,?)",[(f"history-{i}","919999999999@c.us","in",str(i),2000-i) for i in range(160)])
        messages = self.client.get("/api/chat",params={"chat_id":"919999999999@c.us"}).json()["messages"]
        self.assertEqual(len(messages),150)
        self.assertEqual(messages[0]["id"],"history-10")
        self.assertEqual(messages[-1]["id"],"history-159")

    def test_default_ad_message_is_answered_locally_not_forwarded_to_team(self):
        reply=self.incoming(body="Link:\n\n\nHello! Can I get more info on this?")
        self.assertEqual(reply["intents"],["overview"])
        self.assertFalse(reply["handoff"])
        self.assertEqual(len(reply["actions"]),1)
        self.assertNotIn("audience",reply["actions"][0])
        self.assertEqual(self.client.get("/internal/pending-alerts",headers=self.internal).json(),[])

    def test_claim_once_and_pause_cancels_remaining_media(self):
        actions = self.incoming()["actions"]
        r = self.client.post("/internal/claim",json={"id":actions[0]["id"]},headers=self.internal)
        self.assertTrue(r.json()["allowed"])
        self.assertFalse(self.client.post("/internal/claim",json={"id":actions[0]["id"]},headers=self.internal).json()["allowed"])
        self.client.post("/api/chat",json={"chat_id":"919999999999@c.us","paused":True},headers=self.admin)
        self.assertFalse(self.client.post("/internal/claim",json={"id":actions[1]["id"]},headers=self.internal).json()["allowed"])

    def test_global_pause_records_without_queueing(self):
        self.client.post("/api/settings",json={"enabled":False},headers=self.admin)
        self.assertEqual(self.incoming()["actions"],[])
        with backend.db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM messages").fetchone()[0],1)
            self.assertEqual(c.execute("SELECT count(*) FROM outbox").fetchone()[0],0)

    def test_wrong_account_and_stale_heartbeat_block_sends(self):
        backend.bridge_state["me"] = "911111111111"
        self.assertEqual(self.incoming()["actions"],[])
        backend.bridge_state.update(me=DEFAULTS["expected_number"],at=time.time()-100)
        self.assertEqual(self.incoming(id="in-2")["actions"],[])

    def test_old_and_group_messages_are_ignored(self):
        self.assertEqual(self.incoming(timestamp=time.time()-300)["actions"],[])
        self.assertEqual(self.incoming(chat_id="12345@g.us")["actions"],[])
        with backend.db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM contacts").fetchone()[0],0)

    def test_phone_reply_pauses_chat(self):
        self.client.post("/internal/manual",json={"id":"manual1","chat_id":"919999999999@c.us","body":"I'll help you","timestamp":time.time()},headers=self.internal)
        self.assertEqual(self.incoming()["actions"],[])

    def test_stop_and_start_respect_customer_choice(self):
        self.assertEqual(self.incoming(body="STOP")["actions"],[])
        self.assertEqual(self.incoming(id="in-2",body="price")["actions"],[])
        self.assertTrue(self.incoming(id="in-3",body="start")["actions"])

    def test_payment_is_review_only_until_owner_verifies(self):
        self.incoming(body="payment ho gayi")
        with backend.db() as c:
            self.assertEqual(c.execute("SELECT payment FROM contacts").fetchone()[0],"payment_review")
        self.client.post("/api/chat",json={"chat_id":"919999999999@c.us","payment":"paid"},headers=self.admin)
        with backend.db() as c:
            self.assertEqual(c.execute("SELECT payment FROM contacts").fetchone()[0],"paid")

    def test_uncertain_send_is_not_reclaimed(self):
        action = self.incoming()["actions"][0]
        self.client.post("/internal/claim",json={"id":action["id"]},headers=self.internal)
        for _ in range(2):
            self.client.post("/internal/result",json={"id":action["id"],"status":"uncertain","detail":"connection interrupted"},headers=self.internal)
        self.assertFalse(self.client.post("/internal/claim",json={"id":action["id"]},headers=self.internal).json()["allowed"])
        with backend.db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM messages WHERE direction='out'").fetchone()[0],1)

    def test_dashboard_boundary_and_qr_secret(self):
        self.assertEqual(self.client.post("/api/settings",json={"enabled":False}).status_code,403)
        self.assertEqual(self.client.post("/internal/status",json={"state":"ready"}).status_code,403)
        self.assertEqual(self.client.post("/api/settings",json={"enabled":False},headers={**self.admin,"Origin":"https://attacker.example"}).status_code,403)
        backend.bridge_state["qr"] = "private login payload"
        self.assertNotIn("private login payload",self.client.get("/api/state").text)
        self.assertEqual(self.client.get("/",headers={"Host":"attacker.example"}).status_code,400)

    def test_unknown_query_creates_one_separate_team_alert_with_number_and_context(self):
        self.incoming(body="materials")
        result=self.incoming(id="in-2",body="Can I use xyz resin brand?")
        self.assertTrue(result["handoff"])
        self.assertEqual(len(result["actions"]),2)
        client,alert=result["actions"]
        self.assertNotIn("Client query",client["body"])
        self.assertIn("team",client["body"].lower())
        self.assertEqual(alert["destination"],"919899676862@c.us")
        for item in ("919999999999", "Can I use xyz resin brand?", "materials", "https://wa.me/"):
            self.assertIn(item,alert["body"])
        self.assertEqual(self.incoming(id="in-2",body="Can I use xyz resin brand?")["actions"],[])
        self.assertTrue(self.client.post("/internal/claim",json={"id":alert["id"]},headers=self.internal).json()["allowed"])
        self.client.post("/internal/result",json={"id":alert["id"],"status":"sent","wa_id":"team-1"},headers=self.internal)
        state=self.client.get("/api/state").json()
        self.assertEqual(state["counts"]["alerts"],1)
        self.assertEqual(state["counts"]["replies"],0)

    def test_team_chat_never_autoreplies_or_creates_an_alert_loop(self):
        for args in ({"chat_id":"919899676862@c.us","phone":"919899676862"}, {"chat_id":"12345@lid","phone":"919899676862"}):
            self.assertEqual(self.incoming(body="unknown question",**args)["actions"],[])
        with backend.db() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM contacts").fetchone()[0],0)

    def test_chitchat_is_not_forwarded_but_a_real_followup_is(self):
        for index, text in enumerate(("kaise ho aap", "👍", "who are you", "testing", "thanks bro")):
            actions=self.incoming(id=f"social-{index}",body=text)["actions"]
            self.assertFalse(any(a.get("audience")=="team" for a in actions))
        actions=self.incoming(id="real-question",body="certificate milega?")["actions"]
        self.assertEqual(actions[-1]["audience"],"team")

    def test_alert_survives_client_pause_and_pending_alerts_require_auth(self):
        actions=self.incoming(body="certificate milega?")["actions"]
        alert=actions[-1]
        self.client.post("/api/chat",json={"chat_id":"919999999999@c.us","paused":True},headers=self.admin)
        self.assertEqual(self.client.get("/internal/pending-alerts").status_code,403)
        self.assertEqual(len(self.client.get("/internal/pending-alerts",headers=self.internal).json()),1)
        self.assertTrue(self.client.post("/internal/claim",json={"id":alert["id"]},headers=self.internal).json()["allowed"])
        self.assertFalse(self.client.post("/internal/claim",json={"id":alert["id"]},headers=self.internal).json()["allowed"])

    def test_alert_destination_is_fixed_and_global_pause_blocks_it(self):
        alert=self.incoming(body="unknown special request")["actions"][-1]
        self.client.post("/api/settings",json={"enabled":False},headers=self.admin)
        self.assertFalse(self.client.post("/internal/claim",json={"id":alert["id"]},headers=self.internal).json()["allowed"])
        self.client.post("/api/settings",json={"enabled":True},headers=self.admin)
        alert=self.incoming(id="in-2",body="certificate?")["actions"][-1]
        with backend.db() as c:
            payload=json.loads(c.execute("SELECT payload FROM outbox WHERE id=?",(alert["id"],)).fetchone()[0])
            payload["destination"]="911111111111@c.us"
            c.execute("UPDATE outbox SET payload=? WHERE id=?",(json.dumps(payload),alert["id"]))
        self.assertFalse(self.client.post("/internal/claim",json={"id":alert["id"]},headers=self.internal).json()["allowed"])

    def test_upgrade_keeps_enabled_prices_and_custom_answers_but_gates_materials(self):
        saved=backend.config()
        saved.pop("reply_revision",None)
        saved["workshop_fee"]=149
        saved["answers"]["materials"]="Old material list: resin 300 g"
        saved["answers"]["unknown"]="Please clarify"
        saved["answers"]["price"]="Custom price ₹{workshop_fee}"
        with backend.db() as c:
            c.execute("UPDATE settings SET value=? WHERE id=1",(json.dumps(saved),))
        backend.init_db()
        cfg=backend.config()
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["workshop_fee"],149)
        self.assertEqual(cfg["answers"]["price"],"Custom price ₹{workshop_fee}")
        self.assertEqual(cfg["answers"]["materials"],DEFAULTS["answers"]["materials"])
        self.assertEqual(cfg["answers"]["unknown"],DEFAULTS["answers"]["unknown"])
        self.assertEqual(cfg["handoff_number"],"919899676862")


if __name__ == "__main__":
    unittest.main()
