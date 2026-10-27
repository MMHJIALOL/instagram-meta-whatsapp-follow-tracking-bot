"""Local conversational FAQ matching. No external AI service or key required."""
import difflib
import re
import unicodedata

DEFAULTS = {
    "business_name": "Kalakriti Studio", "expected_number": "919211664301",
    "handoff_number": "919899676862", "reply_revision": 2, "enabled": False,
    "workshop_fee": 99, "kit_price": 1000, "shipping": 200,
    "date": "25 October 2026", "time": "3:00 PM IST", "duration": "3 hours",
    "upi": "9899676862@pthdfc", "payee": "PARUL AGARWAL", "website": "https://kalakritistudio.com",
    "answers": {
        "overview": "Hi! 🙂 Shubh-Labh Resin Art Workshop ki details:\n\n📅 {date}, {time}\n⏳ {duration} · Live online\n🎨 Shubh-Labh resin wall hanging banana seekhenge\n✅ Step-by-step guidance + recording included\n💰 Workshop fees ₹{workshop_fee}\n\nMaterial kit optional hai aur fees mein included nahi hai. Material list workshop purchase ke baad milegi.\n\nRegister karna chahenge? 🙂",
        "welcome": "Hi! Kalakriti Studio mein welcome 🙂 Shubh-Labh resin art workshop {date}, {time} par hai. Fees ₹{workshop_fee}. Aap fees, timing, recording ya kit ke baare mein pooch sakte ho.",
        "price": "Workshop fees ₹{workshop_fee} hai 🙂 Recording included hai; material kit alag hai. Payment QR neeche hai. Pay karke screenshot bhej dena.",
        "materials": "Material list workshop purchase karne ke baad provide ki jayegi 🙂",
        "kit": "Material kit ₹{kit_price} + ₹{shipping} shipping = ₹{kit_total}. Workshop ₹{workshop_fee} alag hai. Dono saath ₹{combined_total}.",
        "kit_optional": "Kit lena compulsory nahi hai 🙂 Apne materials use kar sakte ho. Material list workshop purchase ke baad milegi.",
        "kit_included": "Workshop fees ₹{workshop_fee} mein kit included nahi hai. Kit ₹{kit_price} + ₹{shipping} shipping alag hai; dono saath ₹{combined_total}.",
        "total": "Workshop + kit + shipping ka total ₹{combined_total} hai 🙂 Sirf workshop ₹{workshop_fee}; sirf kit with shipping ₹{kit_total}.",
        "included": "₹{workshop_fee} mein live online workshop, step-by-step guidance aur recording included hai 🙂 Material kit alag hai; material list purchase ke baad milegi.",
        "optional": "Optional extras ke prices: weighing scale ₹200, blow torch ₹250, golden marker ₹200. Ye kit mein included nahi hain.",
        "schedule": "Live online class {date} ko {time} par hai, duration {duration} 🙂",
        "recording": "Haan, workshop mein recording access included hai 🙂 Live miss ho jaaye toh baad mein dekh sakte ho.",
        "recording_access": "Recording kitne time available rahegi, team confirm karke aapko batayegi 🙂",
        "learn": "Shubh-Labh resin wall hanging banana seekhenge 🙂 Step-by-step guidance, resin techniques, doubt clearing aur business tips included hain.",
        "beginner": "Beginner ho toh bhi step-by-step guidance milegi 🙂 Workshop mein Shubh-Labh resin wall hanging banana sikhayenge.",
        "business": "Workshop mein resin techniques aur business tips included hain 🙂 Earning ya sales guarantee nahi di jaati.",
        "registration": "Register karne ke liye workshop fees ₹{workshop_fee} pay karke screenshot bhej do 🙂 Team payment verify karke joining details share karegi.",
        "payment": "Payment QR neeche hai 👇 UPI: {upi}\nNaam: {payee}\nWorkshop ₹{workshop_fee}. Kit chahiye toh bata dena. Pay karne ke baad screenshot bhej do.",
        "paid": "Payment update mil gaya 🙂 Team verify karke aapko joining details share karegi. Screenshot nahi bheja hai toh bhej do.",
        "payment_issue": "Payment mein issue aa raha hai? Error ka screenshot ya transaction reference bhej do. Team check karke aapko jaldi reply karegi 🙂",
        "payment_terms": "Payment method ya part-payment ki details team confirm karke aapko batayegi 🙂",
        "joining": "Joining link/details ke liye aapki query team ko bhej di hai 🙂 Payment verify karke team aapko details share karegi.",
        "delivery": "Kit delivery/availability team pincode ke hisaab se confirm karegi 🙂 Apna pincode bhej do; team jaldi reply karegi.",
        "discount": "Abhi workshop fees ₹{workshop_fee} hai 🙂 Extra discount ya offer team confirm karke batayegi.",
        "free": "Workshop free nahi hai 🙂 Fees ₹{workshop_fee} hai aur recording included hai. Payment QR neeche hai.",
        "certificate": "Certificate ki availability team confirm karke aapko batayegi 🙂",
        "language": "Class ki teaching language team confirm karke aapko batayegi 🙂",
        "platform": "Class online hai 🙂 Platform/device ki details team confirm karke aapko share karegi.",
        "age": "Age suitability ke liye team aapko guide karegi 🙂 Participant ki age bata dena.",
        "international": "International participation, payment aur kit shipping team confirm karke aapko batayegi 🙂 Apni country bata dena.",
        "refund": "Refund/cancellation ki query team ko bhej di hai 🙂 Team details check karke jaldi reply karegi.",
        "human": "Bilkul 🙂 Aapki query team ko bhej di hai. Hamari team aapko jaldi reply karegi.",
        "pdf": "Workshop brochure neeche hai 👇",
        "pdf_gate": "Material list wali PDF workshop purchase aur payment verification ke baad milegi 🙂 Workshop fees ₹{workshop_fee} hai.",
        "media": "Photo/document mil gaya 🙂 Team check karke aapko jaldi reply karegi.",
        "audio": "Voice note mil gaya 🙂 Hamari team aapko jaldi reply karegi. Ho sake toh question text mein bhi bhej do.",
        "website": "Hamari website: {website} 🙂 Workshop fees ₹{workshop_fee}; payment recipient {payee} hai.",
        "thanks": "You're welcome 🙂 Aur kuch poochna ho toh batao!",
        "smalltalk": "Sab badhiya 🙂 Aap batao! Workshop ke baare mein kuch poochna hai?",
        "identity": "Main Kalakriti Studio ka automated assistant hoon 🙂 Workshop ki details mein help kar sakta hoon.",
        "goodbye": "Theek hai 🙂 Workshop ki details chahiye ho toh yahin message kar dena.",
        "redirect": "Main workshop ki details mein help kar sakta hoon 🙂 Fees, timing, recording ya kit ke baare mein pooch lo.",
        "test": "Message mil raha hai 🙂 Workshop ke baare mein kya jaanna hai?",
        "unknown": "Aapki query team ko bhej di hai 🙂 Hamari team aapko jaldi reply karegi.",
    },
}
PATTERNS = {
    "materials": [r"\bmaterials?\b", r"\b(?:saman|saaman|supplies|ingredients|required|requirements?)\b", r"\b(?:material|shopping|supply) list\b", r"\bkit\b.*(?:kya kya|items|contains|contents?|andar)", r"\bkya kya (?:chahiye|lagega|lagegi)\b", r"\b(?:resin|hardener).*(?:quantity|kitna|kitni|how much)\b", r"how much (?:resin|hardener)", r"सामग्री|सामान|रेजिन.*कितन"],
    "kit": [r"\bkit\b", r"\b(?:shipping|courier)\b", r"किट"],
    "kit_optional": [r"(?:kit|materials?).*(?:compulsory|mandatory|zaruri|zaroori|necessary|optional)", r"(?:own|apne|khud ke).*(?:materials?|saman|saaman)", r"without (?:the )?kit", r"kit (?:nahi|mat) (?:chahiye|lena)"],
    "kit_included": [r"\b(?:kit|materials?)\b.*(?:included|include|fees? mein|99 mein|saath mile)", r"\b(?:included|include)\b.*\b(?:kit|materials?)\b", r"(?:fees?|99)\s*(?:mein|me|ke saath).*\b(?:kit|materials?)\b"],
    "total": [r"\b(?:total|combo|combined|package|all inclusive)\b", r"(?:workshop|class).*(?:kit).*(?:dono|saath|together)", r"\bdono (?:ka|ki|ke|saath)\b"],
    "included": [r"(?:99|fees?|workshop|class).*(?:kya (?:kya )?mileg|what.*(?:get|included))", r"what is included"],
    "optional": [r"\b(?:scale|torch|marker|weighing|extras?)\b"],
    "price": [r"\b(?:price|prices|fees?|cost|charges?|amount|rate|paisa|paise|rupees|rs)\b", r"\bhow much\b", r"\bkitne (?:ka|ki|paise)\b", r"\bkitna (?:hai|hoga|paisa|pay|payment|lagega)\b", r"^kitne[? ]*$", r"₹|फीस|कीमत|दाम"],
    "schedule": [r"\b(?:date|time|timing|when|duration|kab|online|offline|location|venue|hours?)\b", r"\b(?:kitne baje|kaunse din|kis din|kitni der|kitne ghante|how long)\b", r"कब|समय|तारीख|कितने बजे"],
    "recording": [r"\b(?:recording|recorded|record|replay|miss)\b", r"(?:live|class).*(?:attend|join).*(?:nahi|not|can't|cant)", r"baad (?:me|mein|m) (?:dekh|watch)", r"रिकॉर्ड"],
    "recording_access": [r"\b(?:lifetime|expiry|expire|validity)\b", r"(?:recording|recorded|access).*(?:kitne (?:din|time)|kitna time|how long|duration|days|months|download|save|permanent)", r"(?:kitne din|how long).*(?:recording|access)"],
    "learn": [r"\b(?:learn|syllabus|curriculum|sikh|seekh|sikhayenge|seekhayenge|banayenge|banega)\b", r"\bkya (?:banana|bana|sikh|seekh)\b", r"wall hanging|toran|shubh.?labh|शुभ|लाभ|सीख"],
    "beginner": [r"\b(?:beginner|beginners|fresher|experience|newbie)\b", r"first time|pehli baar|bilkul naya|kuch nahi aata|शुरुआत"],
    "business": [r"\b(?:business|earning|income|sell|selling|sales)\b", r"paise kama|paisa kama|home business"],
    "registration": [r"\b(?:register|registration|enrol|enroll|enrollment|join|book|booking|interested|admission|participate)\b", r"रजिस्टर|जुड़"],
    "payment": [r"\b(?:payment|pay|upi|qr|scan|bank|gpay|paytm|phonepe|bhim)\b", r"bhugtan|भुगतान"],
    "paid": [r"\bpaid\b", r"payment (?:done|sent|kar di|kar diya|ho gay[ai]|hogay[ai])", r"(?:pay|transfer) (?:kiya|kar diya|kar di)", r"paisa bhej|paise bhej|पैसे भेज|भुगतान कर"],
    "payment_issue": [r"(?:payment|pay|transaction|qr|upi).*(?:failed|fail|error|problem|issue|nahi ho|not work|deduct|debit|stuck|pending|invalid)", r"(?:failed|fail|error|problem|unable).*(?:payment|pay|upi|qr)", r"(?:paisa|paise|amount).*(?:kat|cut|deduct|debit)", r"double payment|paid twice|wrong payment"],
    "payment_terms": [r"\b(?:cod|cash|installment|installments|emi|credit card|debit card)\b", r"pay later|part payment|partial payment|payment.*baad|advance payment"],
    "joining": [r"\b(?:link|invite|zoom|meeting id|password|passcode|whatsapp group)\b", r"(?:registered|paid).*(?:details|confirmation|join)", r"joining details|confirmation.*(?:nahi|not)"],
    "delivery": [r"\b(?:delivery|deliver|dispatch|pincode|pin code|tracking|shipment)\b", r"kab (?:aay|ay|pahunch)|डिलीवरी"],
    "discount": [r"\b(?:discount|coupon|offer|bargain|negotiable|concession)\b", r"(?:kam|less|sasta).*(?:price|fees?|kar)|fees?.*(?:kam|less)"],
    "free": [r"\b(?:free|muft|foc)\b", r"फ्री|मुफ्त"],
    "certificate": [r"\b(?:certificate|certification|diploma)\b", r"सर्टिफिकेट"],
    "language": [r"\b(?:language|hindi|english|hinglish|tamil|telugu|bengali|marathi|gujarati)\b", r"भाषा|हिंदी|अंग्रेजी"],
    "platform": [r"\b(?:platform|mobile|phone|laptop|tablet|device|app|google meet)\b"],
    "age": [r"\b(?:age|kids|kid|children|child|minor|teenager|safe|safety|pregnant|pregnancy|allergy|allergic|toxic|toxicity)\b", r"(?:saal|years?) (?:old|ka|ki)|बच्च|उम्र"],
    "international": [r"\b(?:international|abroad|overseas|dubai|uae|usa|uk|canada|australia|dollars?|usd|aed|paypal)\b", r"outside india"],
    "refund": [r"\b(?:refund|cancellation|cancel)\b", r"return.*money|paise wapas|रिफंड"],
    "human": [r"\b(?:human|person|call|contact|parul|complaint|complain|support|agent)\b", r"baat kar|speak to|talk to|कॉल|बात करनी"],
    "pdf": [r"\b(?:pdf|brochure|poster|pamphlet)\b"],
    "website": [r"\b(?:website|official|genuine|legit|scam|fraud|trust)\b", r"kalakritistudio\.com"],
}
MANUAL = {"unknown", "human", "refund", "delivery", "payment_issue", "payment_terms", "paid", "media", "audio",
          "recording_access", "joining", "discount", "certificate", "language", "platform", "age", "international"}
CONTEXT_SAFE = {"price", "materials", "kit", "kit_optional", "kit_included", "total", "optional", "schedule", "recording", "learn", "beginner", "business", "registration", "payment", "website"}
SPELLINGS = {"matrial":"material", "metarial":"material", "materal":"material", "materail":"material", "matirial":"material", "samaan":"saaman", "fess":"fees", "fese":"fees", "prize":"price", "prce":"price", "pric":"price", "recoding":"recording", "paymnt":"payment", "ragistration":"registration", "begginer":"beginner", "timings":"timing", "workshp":"workshop", "plz":"please", "pls":"please", "nhi":"nahi", "nahin":"nahi", "nai":"nahi", "milga":"milega"}
FUZZY_WORDS = ("material", "materials", "recording", "registration", "payment", "beginner", "shipping", "certificate", "syllabus", "workshop", "refund", "delivery")

def normalize(text):
    value = unicodedata.normalize("NFKC", text).lower()
    value = re.sub(r"[\u200b-\u200f\u202a-\u202e]", "", value)
    def spelling(match):
        word = match[0]
        if word in SPELLINGS:
            return SPELLINGS[word]
        if len(word) >= 6 and word not in FUZZY_WORDS:
            near = difflib.get_close_matches(word, FUZZY_WORDS, n=1, cutoff=.88)
            if near:
                return near[0]
        return word
    return re.sub(r"\s+", " ", re.sub(r"[a-z]+", spelling, value)).strip()

def render(key, config):
    values = dict(config)
    values["kit_total"] = config["kit_price"] + config["shipping"]
    values["combined_total"] = values["kit_total"] + config["workshop_fee"]
    template = config["answers"].get(key, DEFAULTS["answers"][key])
    return re.sub(r"\{([a-z_]+)\}", lambda m: str(values.get(m[1], m[0])), template)

def matched(message):
    intents = [name for name, patterns in PATTERNS.items() if any(re.search(p, message) for p in patterns)]
    if any(overview_request(part) for part in re.split(r"[?\n]+|\b(?:and|aur|also)\b", message)):
        intents.insert(0, "overview")
    return intents


def without_ad_header(text):
    """A CTA's Link:/URL header is context, not a request for a joining link."""
    cleaned = re.sub(r"\A\s*link\s*:[ \t]*(?:https?://\S+)?[ \t]*(?:\r?\n|$)", "", text, count=1, flags=re.I)
    if cleaned != text:
        cleaned = re.sub(r"\A\s*https?://\S+[ \t]*(?:\r?\n|$)", "", cleaned, count=1, flags=re.I)
        return cleaned if cleaned.strip() else text
    return text


def overview_request(message):
    text = re.sub(r"^(?:hi+|hello|hey|namaste)\b[!,. ]*(?:(?:mam|maam|sir|ji)\b[!,. ]*)?", "", message.strip()).strip(" .!?,")
    rules = (
        r"(?:(?:can|could|may) i (?:get|have|know)|(?:can|could|would) you (?:please )?(?:send|share|give)(?: me)?|(?:please )?(?:send|share|give)(?: me)?|i (?:want|need|would like)|i.d like)?\s*(?:some |the )?(?:more |full |complete )?(?:(?:workshop|class|course) )?(?:info(?:rmation)?|details?)(?: (?:on|about|of|for) (?:this|it|(?:the )?(?:workshop|class|course)))?(?: please)?",
        r"(?:(?:please|mujhe) )?(?:(?:workshop|class|course|iski|iske) )?(?:(?:ki|ke) )?(?:details?|info(?:rmation)?)(?: chahiye| bhejo| batao| bataiye| please)?",
        r"(?:workshop|class|course|iske)(?: ke)? baare (?:mein|me) (?:batao|bataye|bataiye|jaankari chahiye)",
        r"(?:tell me|can you tell me|can i know)(?: more)? about (?:this|it|(?:the )?(?:workshop|class|course))",
    )
    return any(re.fullmatch(rule, text) for rule in rules)


def social_reply(message):
    """Only complete trivial messages qualify; a real question must never be hidden."""
    text = message.strip(" .!?,।🙂😊👍🙏❤️")
    patterns = {
        "smalltalk": [r"(?:(?:hi|hello|hey)[ ,!.]*)?(?:(?:aap|ap|tum) )?(?:kaise ho(?: (?:aap|ap|tum))?|kaisi ho|kaise hain(?: aap)?|kaise hai|kese ho(?: aap)?|kese hain|kya haal(?: (?:hai|hain|chaal))?|kya hal(?: hai)?|how (?:are|r) (?:you|u)(?: doing)?|how.s it going|what.s up|whatsup|wassup|sup|aur batao|or batao|sab badhiya)", r"(?:i am|i.m|im|main|mai) (?:fine|good|great|theek|thik|badhiya)(?: hu| hoon| thanks| thank you)?", r"(?:hmm+|hm+|haan ji|achha ji|acha ji|theek hai|thik hai|ji ok|okay ji|ok ji|wow|awesome|amazing|cool|super|mast|beautiful)"],
        "identity": [r"(?:aap|ap|tum) (?:kaun|kon|koun) ho", r"(?:aapka|apka|tumhara|your) (?:naam|name)(?: kya hai| please)?", r"what(?:.s| is) your name", r"who (?:are|r) (?:you|u)", r"(?:are you|aap|tum|ye|yah) (?:a |an )?(?:bot|ai|robot|chatbot)(?: ho| hai)?", r"(?:bot|ai|robot|chatbot) (?:ho|hai|h|kya)(?: kya)?", r"(?:what is|what.s) your age", r"(?:aapki|tumhari) (?:age|umar) kya hai", r"are you (?:a )?(?:boy|girl|male|female|married|human)"],
        "welcome": [r"(?:hi+|hello|helo|hlo|hey+|namaste|namaskar|good (?:morning|afternoon|evening))(?: (?:ji|mam|maam|ma.am|madam|sir|bro|didi))?"],
        "thanks": [r"(?:thank you|thankyou|thanku|thanks|thank you so much|thanks a lot|thnx|thx|ty|nice|great|ok|okay|done|love you)(?: (?:ji|mam|maam|sir|bro))?"],
        "goodbye": [r"(?:bye|bye bye|byee+|goodbye|good night|goodnight|gn|take care|see you|talk later|baad mein baat karte|baad me baat karte)"],
        "redirect": [r"(?:tell me|send|sunao) (?:a |one |ek )?(?:joke|jokes|song|poem|shayari|kahani)", r"(?:weather|cricket score|ipl score|tell me a story)", r"(?:(?:you|u|tu|tum|aap) (?:are |is |ho )?)?(?:stupid|idiot|pagal|bekar|useless)(?: bot| ho| hai)?"],
        "test": [r"(?:test|testing|test message|just testing|ping|hello test)", r"(?:asdf+|asd+|qwerty+|zxcv+|abc+|xyz|blah(?: blah)*|[a-z])", r"(.)\1{3,}"],
    }
    for intent, rules in patterns.items():
        if any(re.fullmatch(pattern, text) for pattern in rules):
            return intent
    # Pure emoji/punctuation reactions are acknowledgements, not support tickets.
    if message and not any(unicodedata.category(c)[0] in ("L", "N") for c in message):
        return "thanks"
    return ""

def plan_reply(text, config, *, kind="chat", has_media=False, last_intent="", payment_status="new"):
    message = normalize(without_ad_header(text))
    social = social_reply(message) if kind == "chat" and not has_media else ""
    intents = [social] if social else matched(message)
    review = ""
    negative_paid = bool(re.search(r"(?:nahi|not|haven.t|didn.t|pending|failed|fail|unable|नहीं).{0,18}(?:paid|pay|payment)|(?:paid|pay|payment).{0,18}(?:nahi|not|pending|failed|fail|नहीं)", message))
    if negative_paid:
        intents = [i for i in intents if i != "paid"]
        if "payment" not in intents and "payment_issue" not in intents:
            intents.append("payment")
    if "human" in intents or "refund" in intents:
        intents = ["human" if "human" in intents else "refund"]
    elif "payment_issue" in intents:
        intents = ["payment_issue"]
    elif "payment_terms" in intents:
        intents = ["payment_terms"]
    elif "paid" in intents:
        intents = ["paid"]
    elif kind in ("ptt", "audio"):
        intents = ["audio"]
    elif has_media or kind in ("image", "document", "video", "sticker"):
        intents = ["paid"] if "payment" in intents else ["media"]
    elif kind in ("location", "vcard", "multi_vcard"):
        intents = ["unknown"]
    elif "human" in intents or "refund" in intents:
        intents = ["human" if "human" in intents else "refund"]
    else:
        if "recording_access" in intents:
            intents = [i for i in intents if i not in ("recording", "schedule", "price")]
        if "joining" in intents:
            intents = [i for i in intents if i not in ("registration", "payment", "platform")]
        if "delivery" in intents:
            intents = [i for i in intents if i not in ("schedule", "kit")]
        if "optional" in intents:
            intents = [i for i in intents if i not in ("price", "kit")]
        for specific in ("kit_optional", "kit_included", "total"):
            if specific in intents:
                intents = [i for i in intents if i not in ("kit", "price")]
        if "kit" in intents and "price" in intents and not re.search(r"(?:workshop|class) (?:fees?|price|cost)", message):
            intents.remove("price")
        if "schedule" in intents and re.search(r"how (?:much|long) (?:time|hours?)", message):
            intents = [i for i in intents if i != "price"]
        if "materials" in intents and re.search(r"(?:how much|quantity|kitna|kitni).*(?:resin|hardener)|(?:resin|hardener).*(?:quantity|kitna|kitni)", message):
            intents = [i for i in intents if i != "price"]
        if "beginner" in intents:
            intents = [i for i in intents if i not in ("learn", "registration")]
        if "free" in intents and any(i in intents for i in ("recording", "kit", "kit_included")):
            intents.remove("free")
        if "free" in intents:
            intents = [i for i in intents if i != "price"]
        if "price" in intents:
            intents = [i for i in intents if i not in ("payment", "registration")]
        if "registration" in intents and "payment" in intents:
            intents.remove("payment")
        if message.rstrip("? ") in ("how much", "kitna", "kitne", "kitna hai") and last_intent in ("kit", "kit_optional", "kit_included"):
            intents = ["kit"]
        parts = re.split(r"[?\n]+|\b(?:and|aur|also)\b", message)
        if intents and any(len(p.split()) >= 3 and re.search(r"\b(?:what|which|can|does|is|will|kya|kaise|milegi|milega)\b", p) and not matched(p) and not social_reply(p.strip()) for p in parts):
            intents.append("unknown")
    if not intents and re.fullmatch(r"(?:yes|haan|han|ha|ji|sure|batao|bhejo|send|interested)[.!? ]*", message):
        intents = ["registration" if last_intent == "overview" else last_intent if last_intent in CONTEXT_SAFE else "welcome"]
    if not intents and re.fullmatch(r"(?:hi|hii+|hey|hello|helo|namaste|namaskar|good (?:morning|evening|afternoon)|start|resume|shuru karo)[.!? ]*", message):
        intents = ["welcome"]
    if not intents and re.fullmatch(r"(?:please |send |share |mujhe )?(?:(?:workshop|class) )?(?:details?|info|information)(?: please| bhejo| chahiye)?[.!? ]*", message):
        intents = ["welcome"]
    if not intents and re.fullmatch(r"(?:thank(?:s| you)|thnx|thx|ok(?:ay)?|acha|achha|theek|great|nice|done)[.!? 🙂👍]*", message):
        intents = ["thanks"]
    if not intents and re.fullmatch(r"\d{6}", message) and last_intent == "delivery":
        intents = ["human"]
        review = "pincode received"
    if not intents:
        social = social_reply(message)
        if social:
            intents = [social]
    if not intents:
        intents = ["unknown"]
    if "pdf" in intents and payment_status != "paid":
        intents = ["pdf_gate" if i == "pdf" else i for i in intents]
    intents = list(dict.fromkeys(intents))
    if len(intents) > 5:
        intents = ["unknown"]
    handoff = any(i in MANUAL for i in intents)
    if handoff and not review:
        review = "payment" if "paid" in intents else "unanswered question" if "unknown" in intents else ", ".join(i.replace("_", " ") for i in intents if i in MANUAL)
    actions = [{"kind": "text", "body": "\n\n".join(render(i, config) for i in intents)}]
    kit_payment = "kit" in intents and bool(re.search(r"\b(?:price|cost|pay|payment|qr|fees?)\b", message))
    if any(i in intents for i in ("price", "payment", "registration", "free")) or kit_payment:
        fee = f"Kit + shipping ₹{config['kit_price']+config['shipping']}. Workshop ₹{config['workshop_fee']} alag." if kit_payment else f"Workshop fee ₹{config['workshop_fee']}"
        actions.append({"kind": "image", "asset": "payment.jpg", "body": f"{fee} · {config['payee']}\nUPI: {config['upi']}"})
    if "pdf" in intents:
        actions.append({"kind": "document", "asset": "workshop.pdf", "body": "Workshop details"})
    return {"intents": intents, "actions": actions, "review": review, "payment_review": "paid" in intents, "handoff": handoff}

