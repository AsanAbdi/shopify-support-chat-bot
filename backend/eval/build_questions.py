"""Generates eval/questions.jsonl: 20 in-KB topics x 3 languages (= 60 questions, 20 per language) + 10 out-of-KB questions.

A question passes only if the answer cites one of `sources` (cross-lingual: a German question may be answered from the
English article or the PDF) and, with a real LLM, contains one of `keywords`. Run: python eval/build_questions.py
"""

import json
from pathlib import Path

EN_ = "en-"
RET = ["en-return-policy.html", "de-rueckgabe-30-tage.html", "nl-retourbeleid-30-dagen.html", "de-widerrufsrecht.html", "nl-herroepingsrecht.html", "returns-and-withdrawal-policy.pdf", "terms-and-conditions.pdf"]
SHIPCOST = ["en-shipping-costs.html", "de-versandkosten.html", "nl-verzendkosten.html", "shipping-policy.pdf"]
DELIVERY = ["en-delivery-times.html", "de-lieferzeiten.html", "nl-levertijden.html", "shipping-policy.pdf"]
PAY = ["en-payment-methods.html", "de-zahlungsarten.html", "nl-betaalmethoden.html"]
WARRANTY = ["en-warranty.html", "de-gewaehrleistung-und-garantie.html", "nl-wettelijke-garantie.html", "terms-and-conditions.pdf"]
CONTACT = ["en-contact-and-opening-hours.html", "de-kontakt-und-servicezeiten.html", "nl-contact-en-openingstijden.html"]
FOOT = ["en-size-guide-footwear.html", "de-schuhgroessen-tabelle.html", "nl-maattabel-schoenen.html"]

# (sources, keywords, {lang: question})
TOPICS = [
    (RET, ["30"], {"en": "How long do I have to return an item?", "de": "Wie lange habe ich Zeit, einen Artikel zurückzusenden?", "nl": "Hoe lang heb ik de tijd om een artikel terug te sturen?"}),
    (RET + ["en-return-shipping-costs.html"], ["free", "kostenlos", "gratis", "6.90", "6,90"],
     {"en": "Do I have to pay for returning an item?", "de": "Muss ich die Rücksendung selbst bezahlen?", "nl": "Moet ik de retourkosten zelf betalen?"}),
    (["en-refund-timing.html", "de-wann-kommt-meine-erstattung.html", "nl-wanneer-krijg-ik-mijn-terugbetaling.html", "returns-and-withdrawal-policy.pdf"], ["5"],
     {"en": "How long until I get my money back after sending the parcel back?", "de": "Wie lange dauert es, bis ich mein Geld nach der Rücksendung zurückbekomme?", "nl": "Hoe lang duurt het voordat ik mijn geld terugkrijg na het terugsturen?"}),
    (["en-exchange-and-size-swap.html", "de-umtausch-groesse.html", "nl-omruilen-andere-maat.html"], ["exchange", "swap", "umtausch", "tausch", "omruil", "ruil"],
     {"en": "Can I swap an item for a different size?", "de": "Kann ich einen Artikel in eine andere Größe umtauschen?", "nl": "Kan ik een artikel omruilen voor een andere maat?"}),
    (SHIPCOST, ["4.90", "4,90"], {"en": "How much does shipping to Germany cost?", "de": "Was kostet der Versand nach Deutschland?", "nl": "Wat kost verzending naar Duitsland?"}),
    (SHIPCOST, ["60"], {"en": "From what order value is shipping free in the Netherlands?", "de": "Ab welchem Bestellwert ist der Versand nach Deutschland kostenlos?", "nl": "Vanaf welk bedrag is verzending naar Nederland gratis?"}),
    (DELIVERY, ["1 to 2", "1 bis 2", "1 tot 2", "1-2", "1–2"], {"en": "How many days does delivery to the Netherlands take?", "de": "Wie viele Werktage dauert die Lieferung in die Niederlande?", "nl": "Hoeveel werkdagen duurt de levering naar Nederland?"}),
    (["en-shipping-countries.html", "de-lieferlaender.html", "nl-verzendlanden.html", "de-versandkosten.html", "shipping-policy.pdf"], ["EU"], {"en": "Do you ship to Switzerland or the UK?", "de": "Liefern Sie auch in die Schweiz oder nach Großbritannien?", "nl": "Verzenden jullie ook naar Zwitserland of het Verenigd Koninkrijk?"}),
    (PAY, ["PayPal"], {"en": "Which payment methods do you accept?", "de": "Welche Zahlungsarten akzeptieren Sie?", "nl": "Welke betaalmethoden accepteren jullie?"}),
    (["en-klarna-and-sepa.html", "en-when-is-my-payment-charged.html", "de-zahlungsarten.html", "nl-betaalmethoden.html"], ["30"],
     {"en": "When do I have to pay if I choose Klarna?", "de": "Wann muss ich bei Klarna Rechnung bezahlen?", "nl": "Wanneer moet ik betalen als ik voor Klarna kies?"}),
    (["en-change-delivery-address.html", "en-change-or-cancel-an-order.html", "de-bestellung-aendern-oder-stornieren.html", "nl-bestelling-wijzigen-of-annuleren.html"],
     ["dispatch", "shipped", "versend", "versand", "verzend", "verzonden"],
     {"en": "Can I still change the delivery address after placing my order?", "de": "Kann ich die Lieferadresse nach der Bestellung noch ändern?", "nl": "Kan ik het afleveradres nog wijzigen nadat ik heb besteld?"}),
    (["en-parcel-delayed-or-lost.html", "de-paket-verspaetet-oder-verloren.html", "nl-pakket-vertraagd-of-kwijt.html", "de-bestellstatus-und-sendungsverfolgung.html", "nl-orderstatus-en-track-and-trace.html", "shipping-policy.pdf"], ["5"],
     {"en": "My parcel has not arrived yet, what should I do?", "de": "Mein Paket ist noch nicht angekommen, was soll ich tun?", "nl": "Mijn pakket is nog niet aangekomen, wat moet ik doen?"}),
    (["en-delivery-attempt-failed.html", "de-zustellung-fehlgeschlagen.html", "nl-bezorging-mislukt.html", "shipping-policy.pdf", "en-pickup-points-and-packstation.html"], ["7"],
     {"en": "Nobody was home when the carrier came. How long is the parcel held?", "de": "Bei der Zustellung war niemand zu Hause. Wie lange wird das Paket aufbewahrt?", "nl": "Er was niemand thuis toen de bezorger kwam. Hoe lang wordt het pakket bewaard?"}),
    (["en-care-down-jackets.html", "de-daunenjacke-waschen.html", "nl-donsjas-wassen.html", "product-care-guide.docx"], ["tennis"], {"en": "How do I wash my down jacket?", "de": "Wie wasche ich meine Daunenjacke?", "nl": "Hoe was ik mijn donsjas?"}),
    (["en-reproofing-water-repellency.html", "en-care-waterproof-jackets.html", "product-care-guide.docx"], ["20"],
     {"en": "Water no longer beads off my jacket. How often should I re-proof it?", "de": "Das Wasser perlt von meiner Jacke nicht mehr ab. Wie oft sollte ich sie nachimprägnieren?", "nl": "Het water parelt niet meer af van mijn jas. Hoe vaak moet ik hem opnieuw impregneren?"}),
    (FOOT, ["half", "halbe", "halve"], {"en": "My trail running shoes feel small. Should I order a bigger size?", "de": "Fallen die Trailrunning-Schuhe klein aus, soll ich eine Nummer größer bestellen?", "nl": "Vallen de trailrunningschoenen klein uit, moet ik een maat groter bestellen?"}),
    (["size-chart-footwear.csv", "en-size-guide-footwear.html"], ["26.5", "26,5"], {"en": "What is the foot length for EU size 42?", "de": "Welche Fußlänge entspricht der EU-Größe 42?", "nl": "Welke voetlengte hoort bij EU-maat 42?"}),
    (WARRANTY, ["2"], {"en": "How long is the warranty on your products?", "de": "Wie lange gilt die Gewährleistung auf Ihre Produkte?", "nl": "Hoe lang is de garantie op jullie producten?"}),
    (CONTACT, ["9"], {"en": "What are your customer support hours?", "de": "Wie sind die Servicezeiten Ihres Kundendienstes?", "nl": "Wat zijn de openingstijden van jullie klantenservice?"}),
    (["en-discount-codes.html", "de-rabattcodes.html", "nl-kortingscodes.html"], ["10"], {"en": "How does the newsletter discount work?", "de": "Wie funktioniert der Newsletter-Rabatt?", "nl": "Hoe werkt de nieuwsbriefkorting?"}),
]

OUT_OF_KB = [
    ("en", "Do you sell tents or sleeping bags?", "near"), ("en", "Can I rent hiking equipment from you?", "near"),
    ("en", "Who is the CEO of your company?", "far"), ("en", "Do you plan to open a store in Berlin?", "near"),
    ("de", "Bieten Sie Ratenzahlung über 12 Monate an?", "near"), ("de", "Welche Hauptstadt hat Frankreich?", "far"),
    ("de", "Bieten Sie einen Geschenkverpackungs-Service an?", "near"),
    ("nl", "Hoeveel medewerkers hebben jullie?", "far"), ("nl", "Verkopen jullie ook laptops?", "far"), ("nl", "Hebben jullie een app voor iOS?", "near"),
]


def main() -> None:
    rows = []
    for i, (sources, keywords, qs) in enumerate(TOPICS, 1):
        for lang, q in qs.items():
            rows.append({"id": f"{lang}-{i:02d}", "lang": lang, "question": q, "sources": sources, "keywords": keywords, "type": "in_kb"})
    for i, (lang, q, kind) in enumerate(OUT_OF_KB, 1):
        rows.append({"id": f"oos-{i:02d}", "lang": lang, "question": q, "sources": [], "keywords": [], "type": "out_of_kb", "kind": kind})
    out = Path(__file__).with_name("questions.jsonl")
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    print(f"{len(rows)} questions -> {out}")


if __name__ == "__main__":
    main()
