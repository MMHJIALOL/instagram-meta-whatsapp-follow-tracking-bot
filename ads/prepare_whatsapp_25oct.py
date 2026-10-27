"""Prepare the requested WhatsApp ad, always paused; never activate delivery."""
import json
from pathlib import Path
import sys
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import admanager as a

OUT = a.HERE / "25oct-whatsapp"
OUT.mkdir(exist_ok=True)
POSTER = Path(r"C:\Users\ishaa\Downloads\IMG_0876.PNG")
NUMBER = "919211664301"
MESSAGE = "Hi! Mujhe Rs 99 Shubh-Labh resin workshop book karni hai."
LINK = "https://wa.me/" + NUMBER + "?text=" + urllib.parse.quote(MESSAGE)
spec = {
    "name": "25 Oct Shubh-Labh Rs99 - WhatsApp 9211664301",
    "objective": "engagement", "location": "messages", "perf": "CONVERSATIONS",
    "page_id": a.PAGE_ID, "destinations": ["whatsapp"],
    "caption": "🪔 Is Diwali, apne haathon se Shubh-Labh resin hanging banana seekhein!\n\n"
               "Online Resin Art Workshop — sirf ₹99\n"
               "📅 25 October | ⏰ 3 PM IST | 3 ghante\n"
               "🎥 Video recording available\n\n"
               "Workshop details aur booking ke liye neeche WhatsApp button par tap karke message bhejein.\n"
               "Materials/kit alag se hain.",
    "headline": "₹99 Shubh-Labh Workshop | WhatsApp par book karein",
    "budget_type": "daily", "budget": 300, "start": "", "end": "2026-10-25T14:00",
    "geo": [{"type": "country", "key": "IN", "name": "India"}],
    "age_min": 18, "age_max": 65, "gender": "all", "advantage": True,
    "placements": {"auto": True}, "value_rule_set_id": "", "cost_goal": 0,
    "carousel": False, "greeting": "", "questions": [],
    "intended_whatsapp_number": NUMBER, "suggested_customer_message": MESSAGE,
    "chat_link": LINK, "source_poster": str(POSTER),
}
(OUT / "ad-spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
upload_file = OUT / "upload.json"
if upload_file.exists():
    uploaded = json.loads(upload_file.read_text(encoding="utf-8"))
else:
    job = a.new_job("upload", name=POSTER.name, type="image", phase="receiving", pct=0)
    a.send_image(job, POSTER)
    uploaded = {k: job[k] for k in ("id", "name", "type", "phase", "hash", "width", "height") if k in job}
    upload_file.write_text(json.dumps(uploaded, indent=2), encoding="utf-8")
print("Image uploaded:", json.dumps(uploaded))
creative = {
    "name": spec["name"],
    "object_story_spec": {
        "page_id": a.PAGE_ID, "instagram_user_id": a.IG_ID,
        "link_data": {
            "image_hash": uploaded["hash"],
            "link": "https://api.whatsapp.com/send?phone=" + NUMBER + "&text=" + urllib.parse.quote(MESSAGE),
            "message": spec["caption"], "name": spec["headline"],
            "call_to_action": {"type": "WHATSAPP_MESSAGE", "value": {"app_destination": "WHATSAPP"}},
        },
    },
}
(OUT / "creative.json").write_text(json.dumps(creative, ensure_ascii=False, indent=2), encoding="utf-8")
try:
    validation = a.graph("POST", f"{a.ACT}/adcreatives", retries=0,
                         data=a.form({**creative, "execution_options": ["validate_only"]}))
    (OUT / "validation.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
    print("Validation:", json.dumps(validation))
except a.MetaError as e:
    (OUT / "validation.json").write_text(json.dumps(e.info(), ensure_ascii=False, indent=2), encoding="utf-8")
    print("Validation error:", json.dumps(e.info()))
