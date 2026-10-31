"""Deterministic input analysis: language, email/order extraction, prompt-injection and escalation triggers."""

import re

from langdetect import DetectorFactory, LangDetectException, detect_langs

from app.agent.i18n import SUPPORTED

DetectorFactory.seed = 0

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
ORDER_RE = re.compile(r"(?:#|\b(?:order|bestellung|bestelling|bestellnr|ordernummer|nr|nummer|number)\.?\s*(?:nr\.?|number|nummer)?\s*:?\s*#?)(\d{4,10})\b", re.I)
BARE_NUMBER_RE = re.compile(r"\b(\d{4,10})\b")


def extract_email(text: str) -> str | None:
    m = EMAIL_RE.search(text)
    return m.group(0).lower() if m else None


def extract_order_number(text: str) -> str | None:
    m = ORDER_RE.search(text) or BARE_NUMBER_RE.search(EMAIL_RE.sub("", text))
    return m.group(1) if m else None


def detect_lang(text: str, fallback: str = "en") -> str:
    """DE/EN/NL only. Very short messages keep the conversation language (langdetect is unreliable on them)."""
    if len(text.split()) < 3:
        return fallback if fallback in SUPPORTED else "en"
    try:
        for cand in detect_langs(text):
            if cand.lang in SUPPORTED and cand.prob >= 0.5:
                return cand.lang
    except LangDetectException:
        pass
    return fallback if fallback in SUPPORTED else "en"


_INJECTION = [
    r"ignor\w*\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|earlier|system)?\s*(?:instructions?|rules?|prompts?)",
    r"disregard\s+(?:all\s+|any\s+|the\s+|your\s+)?(?:previous|prior|above|system)?\s*(?:instructions?|rules?|prompts?)",
    r"(?:reveal|show|print|repeat|output|tell me|leak|display)\s+(?:me\s+)?(?:your|the)\s+(?:system\s+|initial\s+|hidden\s+)?(?:prompt|instructions?|rules)",
    r"system\s*prompt",
    r"systemprompt|systeemprompt|system-prompt",
    r"you are now\b|act as\b.*\b(?:dan|unrestricted|jailbroken)|developer mode|jailbreak|\bdan mode\b",
    r"vergiss\s+(?:alle\s+|deine\s+)?(?:vorherigen\s+)?(?:anweisungen|regeln)|ignorier\w*\s+(?:alle\s+|deine\s+)?(?:vorherigen\s+)?(?:anweisungen|regeln)",
    r"(?:zeig|nenn|verrate|gib)\w*\s+(?:mir\s+)?(?:deine|die)\s+(?:system\s*)?(?:anweisungen|regeln|prompt)",
    r"negeer\s+(?:alle\s+|je\s+)?(?:vorige\s+|eerdere\s+)?(?:instructies|regels)|vergeet\s+(?:alle\s+|je\s+)?(?:instructies|regels)",
    r"(?:toon|geef|onthul)\s+(?:me\s+)?(?:je|de)\s+(?:systeem\s*)?(?:instructies|prompt|regels)",
    r"(?:orders?|data)\s+of\s+(?:other|another|different|all)\s+(?:customers?|users?|clients?)|(?:bestellungen|daten)\s+(?:anderer|fremder|aller)\s+kunden|(?:bestellingen|gegevens)\s+van\s+(?:andere|alle)\s+(?:klanten|gebruikers)",
    r"(?:orders?|data)\s+of\s+(?:other|another|different|all)\s+(?:customers?|users?|clients?)",
    r"(?:bestellungen|daten)\s+(?:anderer|fremder|aller)\s+kunden|(?:bestellingen|gegevens)\s+van\s+(?:andere|alle)\s+(?:klanten|gebruikers)",
    r"(?:other|another|different|someone else'?s?|andere[rn]?)\s+(?:customer'?s?|kunden|klant(?:en)?)\s*(?:'s)?\s*(?:orders?|bestellung(?:en)?|bestelling(?:en)?|data|daten|gegevens)",
]
_INJECTION_RE = re.compile("|".join(f"(?:{p})" for p in _INJECTION), re.I | re.S)


def looks_like_injection(text: str) -> bool:
    return bool(_INJECTION_RE.search(text))


def _lex(words: list[str]) -> re.Pattern:
    return re.compile(r"(?<!\w)(?:" + "|".join(words) + r")", re.I)


_HUMAN = _lex([
    r"human", r"real person", r"live agent", r"live chat", r"operator", r"speak (?:to|with) (?:a |an |some)\w*", r"talk to (?:a |an |some)\w*",
    r"customer service agent", r"support agent", r"representative",
    r"mensch", r"mitarbeiter", r"berater", r"echte[nr]? person", r"mit jemandem sprechen", r"sprechen mit einem",
    r"medewerker", r"echt persoon", r"iemand spreken", r"menselijk", r"echte mens",
])
_COMPLAINT = _lex([
    r"terrible", r"unacceptable", r"furious", r"angry", r"scam", r"worst", r"complaint", r"disappointed", r"ridiculous",
    r"useless", r"fed up", r"outrageous", r"rip-?off", r"never again", r"awful", r"horrible",
    r"schrecklich", r"inakzeptabel", r"wütend", r"beschwerde", r"unverschämt", r"enttäuscht", r"betrug", r"katastrophe", r"frechheit", r"abzocke", r"nutzlos",
    r"vreselijk", r"onacceptabel", r"boos", r"klacht", r"teleurgesteld", r"oplichting", r"waardeloos", r"schandalig", r"belachelijk",
])
# Hard signals: an actual payment problem or dispute, always handed to a human.
_MONEY_HARD = _lex([
    r"chargeback", r"charge-?back", r"dispute", r"charged twice", r"double[- ]charged",
    r"doppelt abgebucht", r"zahlungsstreit", r"rückbuchung",
    r"dubbel afgeschreven", r"betalingsgeschil",
])
# Soft signals: refund words. A plain informational question ("how long does a refund take?") is answered
# from the help center; a statement about the customer's own refund goes to a human.
_MONEY_SOFT = _lex([
    r"refund", r"reimburse", r"money back", r"erstattung", r"rückerstattung", r"rückzahlung", r"erstatten", r"geld zurück",
    r"terugbetal\w*", r"restitutie", r"geld terug",
])
_INFO_QUESTION = re.compile(
    r"^\W*(?:how|when|what|can|could|do|does|is|are|wie|wann|was|kann|kann man|gibt|hoe|wanneer|wat|kan|kun|is er)\b", re.I
)


_PERSONAL = re.compile(r"\b(?:my|i|i'm|i've|me|mine|ich|mein\w*|mir|mich|mijn|ik|mij)\b", re.I)


def _is_informational(text: str) -> bool:
    """'How long does a refund take?' / 'and the refund?' ask about the policy; 'where is MY refund?' is the customer's case."""
    return bool(_INFO_QUESTION.match(text)) or ("?" in text and not _PERSONAL.search(text))


def escalation_trigger(text: str) -> str | None:
    """Returns the reason code if the message itself demands a human (F4 triggers)."""
    if _HUMAN.search(text):
        return "human_request"
    if _MONEY_HARD.search(text) or (_MONEY_SOFT.search(text) and not _is_informational(text)):
        return "money"
    if _COMPLAINT.search(text):
        return "complaint"
    return None
