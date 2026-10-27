"""Every message the bot can send, what triggers it, and the rules for editing it on the dashboard (Messages tab).

The texts themselves stay in main.py as defaults; a workshop's edited versions live in workshops.texts and are
read through main.text(key). This file is plain data so main, messenger and the dashboard can all import it.
"""
import re

# buttons a message carries (keys of the button-title texts below), shown in the editor's preview
FOLLOW_BUTTONS = ("VISIT_TITLE", "FOLLOWING_TITLE")

GROUPS = (
    ("start", "First contact"),
    ("check", "Follow check"),
    ("link", "Join link"),
    ("faq", "Questions"),
    ("remind", "Reminders (sent by the bot on its own)"),
    ("live", "Live class"),
    ("unfollow", "Unfollowers"),
    ("between", "Between workshops"),
    ("pages", "Link pages (web page shown when a link is opened)"),
    ("buttons", "Button titles"),
)

# kind: "text" (plain DM), "buttons" (text above buttons, 640 chars max), "title" (button, 20 chars, one line),
# "page" (web page: first line is the heading). link: must contain {link}. fb: Messenger gets it too.
CATALOG = (
    dict(key="INTRO_MSG", group="start", label="Welcome", kind="text", fb=True,
         when="Their first DM or button tap in this workshop. Plain text, goes right before the follow buttons."),
    dict(key="INTRO_LIVE_MSG", group="start", label="Welcome while the class is live", kind="text", fb=True,
         when="Same as Welcome, while a class link is set (Go live)."),
    dict(key="RULES_MSG", group="start", label="3 steps", kind="text", fb=True,
         when="Before the follow buttons when they've DMed before. At most once a day per person."),
    dict(key="OPENER_MSG", group="start", label="Opener (follow status unknown)", kind="buttons",
         buttons=FOLLOW_BUTTONS,
         when="Instagram doesn't show yet whether they follow (it does after their first tap). "
              "Also the private reply to a comment in that case."),
    dict(key="FOLLOW_MSG", group="start", label="Follow request", kind="buttons", buttons=FOLLOW_BUTTONS, fb=True,
         when="They don't follow yet. Also the private reply to a comment from a non-follower."),
    dict(key="FOLLOW_TEXT", group="start", label="Follow request without buttons", kind="text",
         when="Instagram refused the buttons (rare). Sent instead of the follow request."),
    dict(key="COMMENT_REPLY", group="start", label="Public reply under a comment", kind="text", fb=True,
         when="Under every comment that triggers the bot, so others see it answered."),
    dict(key="NOT_FOLLOWING_MSG", group="check", label="Follow not found", kind="buttons", buttons=FOLLOW_BUTTONS,
         when="They tapped I'm following ✅ or wrote DONE, but Instagram doesn't show the follow."),
    dict(key="NOT_FOLLOWING_TEXT", group="check", label="Follow not found, without buttons", kind="text",
         when="Instagram refused the buttons of the message above (rare)."),
    dict(key="LINK_MSG", group="link", label="Join link", kind="text", link=True, fb=True,
         when="They follow: their personal WhatsApp link ({link}). Comments get only this one."),
    dict(key="LINK_BUTTON_MSG", group="link", label="Join link as a button", kind="buttons", buttons=("JOIN_TITLE",),
         fb=True, when="Right after the join link in DMs, the same link as a button."),
    dict(key="PAST_MSG", group="link", label="Offer for earlier workshops' people", kind="text", fb=True,
         when="Someone who got a link in an earlier workshop follows again: once, before their new link. "
              "Only while the offer switch (Workshop tab) is on."),
    dict(key="WHEN_MSG", group="faq", label="When is it?", kind="text", fb=True, faq="when",
         when="A DM with one of the keywords below."),
    dict(key="WHEN_LIVE_MSG", group="faq", label="When is it? (while live)", kind="text", fb=True,
         when="Same question while a class link is set."),
    dict(key="CLASS_MSG", group="faq", label="What now? / How is the class?", kind="text", fb=True, faq="class",
         when="A DM with one of the keywords below."),
    dict(key="CLASS_LIVE_MSG", group="faq", label="What now? (while live)", kind="text", fb=True,
         when="Same question while a class link is set."),
    dict(key="RECORDING_MSG", group="faq", label="Recording?", kind="text", fb=True, faq="recording",
         when="A DM with one of the keywords below."),
    dict(key="MATERIAL_MSG", group="faq", label="Material list", kind="text", fb=True, faq="material",
         when="A DM with one of the keywords below. Link holders also get the message below with their link."),
    dict(key="MATERIAL_LINK_MSG", group="faq", label="Material: link for link holders", kind="text", link=True,
         fb=True, when="Added under the material answer for people who already have their link ({link})."),
    dict(key="FREE_MSG", group="faq", label="Is it free?", kind="text", fb=True, faq="free",
         when="A DM with one of the keywords below. Skipped when the material answer already went."),
    dict(key="REMIND_MSG", group="remind", label="Follow reminder", kind="buttons", buttons=FOLLOW_BUTTONS, fb=True,
         when="3 h after the follow request if they still don't follow. Once per workshop, only inside the 24 h window."),
    dict(key="LINK_REMIND_MSG", group="remind", label="Join-link reminder", kind="text", link=True, fb=True,
         when="3 h after the link if they never really opened it. Once per workshop, only inside the 24 h window."),
    dict(key="MEET_MSG", group="live", label="Class link", kind="text", link=True, fb=True,
         when="While live: followers and link holders get the class link ({link}), once per workshop."),
    dict(key="MEET_BUTTON_MSG", group="live", label="Class link as a button", kind="buttons", buttons=("MEET_TITLE",),
         fb=True, when="Right after the class link in DMs."),
    dict(key="UNFOLLOW_WARN_MSG", group="unfollow", label="Unfollow warning", kind="buttons", buttons=FOLLOW_BUTTONS,
         when="You press Send warning in the To remove card."),
    dict(key="REFOLLOW_MSG", group="unfollow", label="Thanks for following again", kind="text",
         when="A warned person follows again and taps I'm following ✅ (or writes DONE)."),
    dict(key="BETWEEN_MSG", group="between", label="No workshop right now", kind="buttons", buttons=("VISIT_TITLE",),
         fb=True, when="After End workshop and before the next one starts: any DM or comment, once a day per person."),
    dict(key="USED_LINK_PAGE_TEXT", group="pages", label="Link already used", kind="page",
         when="Their link is opened after we're sure they joined. First line is the heading."),
    dict(key="PAST_LINK_PAGE_TEXT", group="pages", label="Link from an earlier workshop", kind="page",
         when="An earlier workshop's link is opened before they got the new one. First line is the heading."),
    dict(key="VISIT_TITLE", group="buttons", label="Visit Profile button", kind="title",
         when="Opens the Instagram profile. Messenger shows \"Visit Page\". The welcome and 3 steps texts name it."),
    dict(key="FOLLOWING_TITLE", group="buttons", label="I'm following button", kind="title",
         when="Makes the bot check the follow. The welcome and 3 steps texts name it."),
    dict(key="JOIN_TITLE", group="buttons", label="Join WhatsApp button", kind="title", when="Under the join link."),
    dict(key="MEET_TITLE", group="buttons", label="Join class button", kind="title", when="Under the class link."),
)
BY_KEY = {m["key"]: m for m in CATALOG}

# A reel can have its own offer (dashboard's Reels tab): comments on it get the reel's texts, and so do that
# person's later DMs and taps. These texts don't apply there (no live class, no earlier-workshop offer,
# no unfollow warnings, no used-link tracking).
REEL_SKIP = {"INTRO_LIVE_MSG", "WHEN_LIVE_MSG", "CLASS_LIVE_MSG", "PAST_MSG", "MEET_MSG", "MEET_BUTTON_MSG",
             "MEET_TITLE", "UNFOLLOW_WARN_MSG", "REFOLLOW_MSG", "BETWEEN_MSG", "USED_LINK_PAGE_TEXT",
             "PAST_LINK_PAGE_TEXT"}
REEL_CATALOG = tuple(m for m in CATALOG if m["key"] not in REEL_SKIP)

# the first reel offer: the ₹99 Shubh-Labh Diwali workshop (Kalakriti_Diwali_Workshop.pdf). New reels start from
# a copy of the latest reel's texts, or these.
REEL_99_NAME = "₹99 Shubh-Labh Diwali workshop"
REEL_99_LINK = ("https://wa.me/919899676862?text=" "Hi%21%20Mujhe%20%E2%82%B999%20wala%20Shubh-Labh%20Diwali%20resin%20"
                "workshop%20%2825%20Oct%29%20book%20karna%20hai")
REEL_99_TEXTS = {
    "INTRO_MSG": ("🪔 Shubh-Labh Diwali Resin Workshop, sirf ₹99! (regular ₹999)\n"
                  "📅 25 Oct (Sunday), dopahar 3 se shaam 6 baje tak (3 ghante)\n"
                  "💻 100% online, ghar se · 🎥 Recording bhi milegi\n\n"
                  "Booking link ke liye: \"Visit Profile\" → Follow karo → wapas aake \"I'm following ✅\" dabao"),
    "RULES_MSG": ("Booking link paane ke 3 simple steps 👇\n"
                  "1️⃣ Neeche \"Visit Profile\" button dabao\n"
                  "2️⃣ Profile pe \"Follow\" button dabao\n"
                  "3️⃣ Wapas yahan aao aur \"I'm following ✅\" dabao (ya bas DONE likh do)\n"
                  "Bas! ₹99 workshop ka booking link turant aa jayega ✨"),
    "OPENER_MSG": ("Hey! 🪔 ₹99 Shubh-Labh Diwali resin workshop mein aapka swagat hai 😊\n\n"
                   "Profile follow karo aur neeche button dabao, booking link turant aa jayega ✨"),
    "FOLLOW_MSG": "Bas ek step baaki hai! Profile pe jaake Follow karo, phir neeche wala button dabao, booking link aa jayega 😄",
    "LINK_MSG": ("Thanks for following! 🎉\n\n"
                 "₹99 Shubh-Labh Diwali workshop book karne ke liye WhatsApp pe message karo, "
                 "payment link wahin mil jayega 👇\n{link}"),
    "LINK_BUTTON_MSG": "Ya phir neeche button dabao 👇",
    "JOIN_TITLE": "Book on WhatsApp",
    "WHEN_MSG": ("📅 Workshop 25 October (Sunday) ko dopahar 3 baje shuru hoga, shaam 6 baje tak (3 ghante) 🪔\n"
                 "💻 Online hai, ghar se join karo!"),
    "CLASS_MSG": ("💻 Workshop 100% online hai, ghar baithe join karo! Booking ke baad class ki saari details "
                  "WhatsApp pe mil jayengi 😊"),
    "RECORDING_MSG": ("Haan, recording access bhi milega 🎥\n"
                      "Live miss ho jaye toh baad mein apne time pe dekh lena 😊"),
    "MATERIAL_MSG": ("🛍️ Material kit: ₹1,000 + ₹200 shipping (₹99 fee mein included nahi hai)\n"
                     "Kit mein: 4 MDF base (2 pairs), 3D flower mould, 4 Shubh-Labh acrylic cutouts, "
                     "300 g resin + hardener, 2 pigments, 4 latkans, 10 moti flowers, cups aur sticks\n"
                     "Optional: weighing scale ₹200, blow torch ₹250, golden marker ₹200\n"
                     "Kit ke liye 9899676862 pe WhatsApp karo 😊"),
    "MATERIAL_LINK_MSG": "Booking ka link 👇\n{link}",
    "FREE_MSG": ("Workshop fee sirf ₹99 hai 🎉 (regular ₹999, 90% off)\n"
                 "Material kit alag se: ₹1,000 + ₹200 shipping\n"
                 "Book karne ke liye 9899676862 pe WhatsApp karo, payment link wahin milega 😊"),
    "REMIND_MSG": ("Reminder 🪔 ₹99 Shubh-Labh Diwali resin workshop, 25 Oct dopahar 3 baje! "
                   "Booking link ke liye profile follow karo, phir neeche button dabao 👇"),
    "LINK_REMIND_MSG": ("Booking ho gayi? 🪔 ₹99 Shubh-Labh workshop 25 Oct, dopahar 3 baje hai. "
                        "WhatsApp pe message karke seat book karo 👇\n{link}"),
}

# questions answered on top of the follow-gate (also for people who already have the link). The built-in ones
# answer with a catalog text (faq=...); questions added on the dashboard keep their answer under their own id.
FAQ_DEFAULTS = (
    dict(id="when", label="When is it?", on=True,
         keywords=["when", "schedule", "date", "time", "timing", "kab", "kitne baje", "कब"]),
    dict(id="recording", label="Recording?", on=True, keywords=["record*", "replay", "baad mein dekh*", "रिकॉर्ड"]),
    dict(id="material", label="Material list", on=True,
         keywords=["material", "materials", "saman", "samaan", "saaman", "saamaan", "samaaan", "list", "items",
                   "requirement", "requirements", "kya kya", "सामान", "मटेरियल"]),
    dict(id="free", label="Is it free?", on=True,
         keywords=["free", "fee", "fees", "charge", "charges", "cost", "price", "paid", "paise", "फ्री"]),
    dict(id="class", label="What now? / How is the class?", on=True,
         keywords=["ab kya", "kya karna hai", "kya karna h", "kya karna he", "kya karu", "kya karun", "kya kare",
                   "what now", "now what", "what next", "what to do", "what should i do", "kaise hogi", "kaise hoga",
                   "kaise hoyega", "kese hogi", "kese hoga", "kaise join", "online", "offline", "google meet", "zoom",
                   "अब क्या", "क्या करना", "कैसे होगी", "कैसे होगा"]),
)
FAQ_TEXT = {m["faq"]: m["key"] for m in CATALOG if m.get("faq")}  # built-in question id -> text key
# on the paid ₹99 offer, "Is it free?" (the price answer) also catches booking and rupee words
REEL_99_FAQS = [dict(f, label="Price / booking?", keywords=f["keywords"] + [
    "99", "₹99", "rs", "rupees", "rupaye", "kitne", "kitna", "book*", "payment", "pay"])
                if f["id"] == "free" else dict(f) for f in FAQ_DEFAULTS]

TITLE_MAX, BUTTONS_TEXT_MAX, TEXT_MAX_BYTES = 20, 640, 1000


def keyword_pattern(keywords):
    """Keywords -> one regex: Latin words match as whole words (any case), `*` at the end = any ending
    (record* -> recording); other scripts (Hindi) match anywhere, since \\b doesn't work with their vowel signs."""
    latin, other = [], []
    for k in keywords:
        k = " ".join(str(k).split())
        if not k:
            continue
        (latin if k.isascii() else other).append(re.escape(k).replace(r"\*", r"\w*" if k.isascii() else r"\S*"))
    parts = ([r"\b(" + "|".join(latin) + r")\b"] if latin else []) + other
    return re.compile("|".join(parts) or r"(?!)", re.I)


def check(key, text):
    """Why this text can't be saved for this message, or None if it's fine."""
    m = BY_KEY.get(key)
    if not m:
        return "Unknown message"
    text = text or ""
    if not text.strip():
        return "Can't be empty"
    kind = m["kind"]
    if kind == "title":
        if "\n" in text:
            return "Button titles are one line"
        if len(text) > TITLE_MAX:
            return f"Instagram cuts button titles at {TITLE_MAX} characters ({len(text)} now)"
    if kind == "buttons" and len(text) > BUTTONS_TEXT_MAX:
        return f"Text above buttons can be at most {BUTTONS_TEXT_MAX} characters ({len(text)} now)"
    if kind == "text" and len(text.encode()) > TEXT_MAX_BYTES - (60 if m.get("link") else 0):
        return f"Too long for Instagram (max about {TEXT_MAX_BYTES} bytes; emoji and Hindi count double or more)"
    if m.get("link") and "{link}" not in text and "{}" not in text:
        return "Must contain {link} (where the link goes)"
    if not m.get("link") and "{link}" in text:
        return "{link} only works in the link messages; here it would be sent as it is"
    return None


def check_custom(f, answer):
    """Why a custom reply (Messages tab → Custom replies) can't be saved, or None. f has the optional
    button_title + button_url (a link button under the reply) and stop (no follow step after it)."""
    title, url = f.get("button_title") or "", f.get("button_url") or ""
    if not answer.strip():
        return "Write the reply"
    if "{link}" in answer:
        return "{link} only works in the bot's own link messages"
    if bool(title) != bool(url):
        return "A button needs both a title and a link"
    if title and ("\n" in title or len(title) > TITLE_MAX):
        return f"Button title: one line, at most {TITLE_MAX} characters"
    if url and not re.fullmatch(r"https://\S+", url):
        return "Button link must start with https://"
    if title and len(answer) > BUTTONS_TEXT_MAX:
        return f"With a button the reply can be at most {BUTTONS_TEXT_MAX} characters ({len(answer)} now)"
    if len(answer.encode()) > TEXT_MAX_BYTES:
        return f"Too long for Instagram (max about {TEXT_MAX_BYTES} bytes; emoji and Hindi count double or more)"
    return None


def check_faq(f):
    if not str(f.get("label") or "").strip():
        return "Give it a name (only you see it)"
    if not [k for k in f.get("keywords") or [] if str(k).strip()]:
        return "Add at least one word to watch for"
    return None
