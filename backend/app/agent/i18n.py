"""Fixed customer-facing texts (DE/EN/NL). Handoff texts are templates on purpose: they must never be improvised."""

SUPPORTED = ("de", "en", "nl")


def norm_lang(lang: str | None) -> str:
    return lang if lang in SUPPORTED else "en"


T: dict[str, dict[str, str]] = {
    "ask_email": {
        "en": "I'll hand this over to a colleague. Please tell me your email address so they can reply to you.",
        "de": "Ich übergebe das gern an einen Kollegen. Bitte nennen Sie mir Ihre E-Mail-Adresse, damit er Ihnen antworten kann.",
        "nl": "Ik draag dit graag over aan een collega. Laat me je e-mailadres weten, zodat hij of zij kan antwoorden.",
    },
    "handoff": {
        "en": "I've passed this on to a colleague. You'll get a reply by email ({email}) within {hours} hours.",
        "de": "Ich habe das an einen Kollegen weitergegeben. Die Antwort kommt innerhalb von {hours} Stunden per E-Mail ({email}).",
        "nl": "Ik heb dit doorgegeven aan een collega. Je krijgt binnen {hours} uur antwoord per e-mail ({email}).",
    },
    "handoff_failed": {
        "en": "I couldn't forward this automatically right now. Please write to {support_email} and mention your question.",
        "de": "Die automatische Weiterleitung hat gerade nicht geklappt. Bitte schreiben Sie an {support_email}.",
        "nl": "Doorsturen lukt nu even niet automatisch. Mail alsjeblieft naar {support_email}.",
    },
    "already_escalated": {
        "en": "Your request has already been passed to a colleague (ticket #{ticket}). You'll get a reply by email within {hours} hours.",
        "de": "Ihre Anfrage wurde bereits an einen Kollegen weitergegeben (Ticket #{ticket}). Sie erhalten innerhalb von {hours} Stunden eine Antwort per E-Mail.",
        "nl": "Je vraag is al doorgegeven aan een collega (ticket #{ticket}). Je krijgt binnen {hours} uur antwoord per e-mail.",
    },
    "no_answer": {
        "en": "I couldn't find this in our help center, so I don't want to guess. Would you like me to pass your question to a colleague?",
        "de": "Dazu habe ich in unserem Hilfecenter nichts gefunden und möchte nichts raten. Soll ich Ihre Frage an einen Kollegen weitergeben?",
        "nl": "Hierover vind ik niets in ons helpcentrum en ik wil niet gokken. Zal ik je vraag doorgeven aan een collega?",
    },
    "injection": {
        "en": "I can't help with that. I'm happy to answer questions about orders, products, shipping and returns.",
        "de": "Dabei kann ich nicht helfen. Gern beantworte ich Fragen zu Bestellungen, Produkten, Versand und Rückgabe.",
        "nl": "Daar kan ik niet mee helpen. Ik beantwoord graag vragen over bestellingen, producten, verzending en retouren.",
    },
    "budget": {
        "en": "The assistant is temporarily unavailable. Please contact {support_email}.",
        "de": "Der Assistent ist vorübergehend nicht verfügbar. Bitte wenden Sie sich an {support_email}.",
        "nl": "De assistent is tijdelijk niet beschikbaar. Neem contact op via {support_email}.",
    },
    "error": {
        "en": "Something went wrong on our side. Please try again in a moment.",
        "de": "Bei uns ist etwas schiefgelaufen. Bitte versuchen Sie es gleich noch einmal.",
        "nl": "Er ging iets mis aan onze kant. Probeer het zo nog eens.",
    },
    # --- offline fake-LLM compositions (LLM_PROVIDER=fake) ---
    "order_ask_email": {
        "en": "To look up your order I also need the email address used for the order.",
        "de": "Für die Bestellabfrage benötige ich zusätzlich die E-Mail-Adresse der Bestellung.",
        "nl": "Om je bestelling op te zoeken heb ik ook het e-mailadres van de bestelling nodig.",
    },
    "order_not_found": {
        "en": "I couldn't find an order with this number and email. Please check both details.",
        "de": "Zu dieser Nummer und E-Mail-Adresse habe ich keine Bestellung gefunden. Bitte prüfen Sie beide Angaben.",
        "nl": "Ik kan geen bestelling vinden met dit nummer en e-mailadres. Controleer beide gegevens.",
    },
    "order_unavailable": {
        "en": "I can't reach the order system right now. Please try again shortly.",
        "de": "Das Bestellsystem ist gerade nicht erreichbar. Bitte versuchen Sie es gleich noch einmal.",
        "nl": "Het bestelsysteem is nu niet bereikbaar. Probeer het zo nog eens.",
    },
    "order_status": {
        "en": "Order #{number}: status {status}.",
        "de": "Bestellung #{number}: Status {status}.",
        "nl": "Bestelling #{number}: status {status}.",
    },
    "order_shipped": {
        "en": " Shipped on {date} with {carrier}, tracking number {tracking}.",
        "de": " Versendet am {date} mit {carrier}, Sendungsnummer {tracking}.",
        "nl": " Verzonden op {date} met {carrier}, trackingnummer {tracking}.",
    },
    "order_delivered": {"en": " Delivered on {date}.", "de": " Zugestellt am {date}.", "nl": " Bezorgd op {date}."},
    "order_eta": {
        "en": " Estimated delivery: {date}.", "de": " Voraussichtliche Zustellung: {date}.", "nl": " Verwachte bezorging: {date}.",
    },
    "products_found": {
        "en": "Here is what I found:",
        "de": "Das habe ich gefunden:",
        "nl": "Dit heb ik gevonden:",
    },
    "products_none": {
        "en": "I couldn't find matching products. Could you describe what you're looking for in more detail?",
        "de": "Ich habe keine passenden Produkte gefunden. Können Sie genauer beschreiben, wonach Sie suchen?",
        "nl": "Ik heb geen passende producten gevonden. Kun je preciezer omschrijven wat je zoekt?",
    },
    "in_stock": {"en": "in stock", "de": "verfügbar", "nl": "op voorraad"},
    "out_of_stock": {"en": "out of stock", "de": "nicht verfügbar", "nl": "niet op voorraad"},
}


def t(key: str, lang: str | None, **kwargs: object) -> str:
    return T[key][norm_lang(lang)].format(**kwargs)
