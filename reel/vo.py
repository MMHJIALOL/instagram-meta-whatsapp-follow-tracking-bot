import json, base64, os, sys, urllib.request
KEY = os.environ["ELEVEN_KEY"]
VOICE = sys.argv[1] if len(sys.argv) > 1 else "gHu9GtaHOXcSqFTK06ux"
TEXT = ("इस festive season, घर बैठे कुछ नया try करो! "
        "करवा चौथ special... FREE resin art live workshop! "
        "Resin से बनाओ पूजा थाली, और decorate करो छलनी भी. "
        "ग्यारह October को join करो, बिल्कुल free! "
        "Details के लिए comment करो, interested.")
body = json.dumps({"text": TEXT, "model_id": "eleven_multilingual_v2",
                   "voice_settings": {"stability": 0.4, "similarity_boost": 0.8, "style": 0.35, "speed": 1.1}}).encode()
req = urllib.request.Request(f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}/with-timestamps",
                             data=body, headers={"xi-api-key": KEY, "Content-Type": "application/json"})
d = json.load(urllib.request.urlopen(req))
open("vo.mp3", "wb").write(base64.b64decode(d["audio_base64"]))
a = d["alignment"]
# word start times, to sync on-screen beats
w, t0 = "", None
for ch, s in zip(a["characters"], a["character_start_times_seconds"]):
    if ch == " ":
        if w: print(f"{t0:6.2f} {w}")
        w, t0 = "", None
    else:
        t0 = s if t0 is None else t0; w += ch
print(f"{t0:6.2f} {w}  end {a['character_end_times_seconds'][-1]:.2f}")
