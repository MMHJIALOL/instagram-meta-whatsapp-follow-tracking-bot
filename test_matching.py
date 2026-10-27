"""Offline check of how WhatsApp joins are matched to Instagram taps. python test_matching.py (no network)."""
from datetime import datetime, timedelta, timezone

import dashboard as d

T0 = datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(hours=1)
APP = ("Mozilla/5.0 (Linux; Android 16; {} Build/BP2A; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
       "Chrome/154.0 Mobile Safari/537.36 Instagram 449.0.0.52.84 Android (36/16; 480dpi; {})")
SCANNER = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Mobile Safari/537.36"


def tap(ig, sec, ua=None, ip=None):
    return (ig, T0 + timedelta(seconds=sec), ua or APP.format(ig, ig), ip)  # each person's own phone


def join(who, sec, source="live"):
    return (T0 + timedelta(seconds=sec), who, "join", source)


def run(events, clicks, fixes=(), ig_info=None, wa_names=None):
    events = sorted(events, key=lambda e: e[0])
    return d.match(events, clicks, list(fixes), ig_info or {}, wa_names or {}, NOW)[0]


def check(name, ok):
    assert ok, name
    print("OK ", name)


m = run([join("+91A", 12)], [tap("a", 0)])
check("lone tap, join 12 s later: sure", m["a"]["conf"] == "sure" and m["a"]["who"] == "+91A")

m = run([join("+91A", 12)], [tap("a", 0), tap("b", 11, SCANNER), tap("b", 1, "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                                                          "AppleWebKit/537.36 Chrome/74.0.3729.131")])
check("Meta's scanner hits on someone else's link don't make it ambiguous", m["a"]["conf"] == "sure" and "b" not in m)

m = run([join("+91A", 12)], [tap("a", 15)])
check("a join before the tap is not that person", "a" not in m)

m = run([join("+91A", 12)], [tap("a", 0), tap("b", 2)])
check("two taps 2 s apart, one join: check, not sure", m["a"]["conf"] == "maybe" and m["b"]["conf"] == "maybe")

m = run([join("+91A", 12)], [tap("a", 0), tap("b", 150)])
check("someone tapping 2.5 min later doesn't spoil a match", m["a"]["conf"] == "sure")

ev, cl = [join("+91A", 10), join("+91B", 12)], [tap("a", 0), tap("b", 1)]
m = run(ev, cl)
check("two taps and two joins seconds apart: both check", m["a"]["conf"] == "maybe" and m["b"]["conf"] == "maybe")
m = run(ev, cl, fixes=[("a", "+91A", "yes")])
check("confirming one settles the other", m["a"]["conf"] == "confirmed" and m["b"]["who"] == "+91B"
      and m["b"]["conf"] == "sure")
m = run(ev, cl, fixes=[("a", "+91A", "no")])
check("ruling one out settles both", m["a"]["who"] == "+91B" and m["b"]["who"] == "+91A")
m = run(ev, cl, ig_info={"a": ("rachna.negi.940", "")}, wa_names={"+91A": ("Rachna Negi", "")})
check("a matching WhatsApp name tips it", m["a"]["options"][0]["who"] == "+91A" and m["a"]["p"] > 0.9)
m = run(ev, cl, ig_info={"a": ("priyanka_01", "")}, wa_names={"+91A": ("Priya", "")})
check("a name that only starts the same doesn't count", m["a"]["p"] < 0.9)

# s._.d._.151180 / official__faizaaa3 on 5 Oct: the second tap came under a second before the first join
m = run([join("+91A", 23), join("+91B", 35)], [tap("a", 0), tap("b", 22.8)])
check("a tap too close to a join can't be that join, so the other one is sure",
      m["a"]["who"] == "+91A" and m["b"]["who"] == "+91B")

owner = APP.format("iPhone14,8", "owner")
m = run([join("+91A", 12)], [tap("a", 0), tap("b", 1, owner), tap("c", 2, owner)])
check("one phone opening several people's links (the owner reading DMs) counts for none of them",
      m["a"]["conf"] == "sure" and "b" not in m and "c" not in m)

m = run([join("+91A", 12)], [tap("a", 0), tap("b", 1, ip="157.240.1.1")])
check("a tap from Meta's own IP is a scanner even with an Instagram user agent", m["a"]["conf"] == "sure")
m = run([join("+91A", 12)], [tap("a", 0), tap("b", 1, SCANNER, ip="52.33.194.198")])
check("a phone-like scanner on a cloud server isn't a person", m["a"]["conf"] == "sure" and "b" not in m)

m = run([join("+91A", 12), (T0 + timedelta(seconds=60), "+91A", "leave", "live"), join("+91A", 400)],
        [tap("a", 0), tap("b", 390)])
check("a rejoin is the same person, not whoever tapped before it", m["a"]["who"] == "+91A" and "b" not in m)

m = run([join("+91A", 0, "export-min")], [tap("a", -20)])
check("an export line with only the minute can still match", m["a"]["options"][0]["who"] == "+91A")

m = run([join("+91A", 12)], [tap("a", 0)], fixes=[("a", "", "no")])
check("'didn't join' takes them out", "a" not in m)

# 5 Oct: an existing member opening the "free ... 11th Oct" group isn't someone arriving through their link
SUB = "free resin pooja thali workshop 11th Oct"
ev, state = d.main_chat([(T0 + timedelta(seconds=5), "+91OLD", "join", "live", SUB),
                         (T0 + timedelta(seconds=12), "+91A", "join", "live", d.MAIN_CHAT),
                         (T0 + timedelta(seconds=30), "+91A", "join", "live", SUB),
                         (T0 + timedelta(seconds=90), "+91A", "leave", "live", SUB)])
m = run(ev, [tap("a", 0)])
check("joining another group of the community isn't a new arrival", m["a"]["conf"] == "sure" and m["a"]["who"] == "+91A")
check("leaving only the other group still counts as in the community", state["+91A"][0] == "join"
      and state["+91OLD"][0] == "join")
