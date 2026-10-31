"""Synthetic store data for the Shopify mock: a fictional outdoor shop ("Nordvik Outdoor") with ~1500 SKUs
and orders in every state a support bot meets in real life. Deterministic (seeded); dates are relative to start-up."""

import hashlib
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

STORE_URL = "https://nordvik-outdoor.example"

# ------------------------------------------------------------------ catalog

SIZES_APPAREL = ["XS", "S", "M", "L", "XL", "XXL"]
SIZES_SHOE = ["EU 38", "EU 39", "EU 40", "EU 41", "EU 42", "EU 43", "EU 44", "EU 45", "EU 46"]
SIZES_KIDS = ["92", "104", "116", "128", "140", "152"]
SIZES_KIDS_SHOE = ["EU 28", "EU 29", "EU 30", "EU 31", "EU 32", "EU 33", "EU 34", "EU 35"]
SIZES_SHOE_W = ["EU 36", "EU 37", "EU 38", "EU 39", "EU 40", "EU 41", "EU 42"]
COLORS = {
    "jacket": ["Black", "Navy", "Forest Green", "Burnt Orange", "Glacier Blue"],
    "layer": ["Black", "Charcoal", "Sand", "Pine"],
    "pants": ["Black", "Khaki", "Charcoal"],
    "shoe": ["Black", "Stone Grey", "Olive"],
    "acc": ["Black", "Navy", "Red", "Heather Grey"],
    "pack": ["Black", "Forest Green", "Red"],
    "kid": ["Red", "Navy", "Forest Green", "Burnt Orange"],
    "kidshoe": ["Navy", "Red", "Olive"],
}

# (title, type, kind, base price EUR, vendor, description fragments)
LINES = [
    ("Alpine Shell Jacket", "Jackets", "jacket", 249.95, "Nordvik", "3-layer waterproof shell, 20,000 mm water column, fully taped seams, helmet-compatible hood, pit zips. PFC-free DWR finish."),
    ("Fjord Rain Jacket", "Jackets", "jacket", 149.95, "Nordvik", "2.5-layer waterproof jacket, 15,000 mm, packs into its own pocket, adjustable hood. Recycled polyester."),
    ("Glacier Down Jacket", "Jackets", "jacket", 229.95, "Nordvik", "800-fill RDS-certified goose down, water-repellent outer, 2 zipped hand pockets. Warm down to -10 C."),
    ("Ridge Softshell Jacket", "Jackets", "jacket", 139.95, "Nordvik", "Stretch softshell, wind-resistant, brushed inside, 2 zipped pockets. Ideal for cool, dry days."),
    ("Coastal Windbreaker", "Jackets", "jacket", 89.95, "Kustwerk", "Ultralight 90 g windbreaker with DWR finish and reflective details. Packs into the chest pocket."),
    ("Summit Hybrid Jacket", "Jackets", "jacket", 189.95, "Alpenrose", "Insulated hybrid with synthetic insulation in the body and stretch panels at the sides."),
    ("Nordic Insulated Parka", "Jackets", "jacket", 299.95, "Nordvik", "Long waterproof parka with 100 g recycled insulation, fleece-lined collar, storm flap. Built for city winters."),
    ("Trail Fleece Half-Zip", "Fleece & Midlayers", "layer", 79.95, "Nordvik", "Grid fleece 250 g/m2, quick-drying, half-zip, thumb loops. Recycled polyester."),
    ("Merino Base Layer Crew", "Fleece & Midlayers", "layer", 89.95, "Alpenrose", "100% merino wool 190 g/m2, naturally odour-resistant, flatlock seams. Machine washable at 30 C."),
    ("Thermal Grid Hoodie", "Fleece & Midlayers", "layer", 99.95, "Nordvik", "Hooded thermal midlayer with stretch panels and a chest pocket. Pairs under any shell."),
    ("Trek Hiking Pants", "Pants", "pants", 109.95, "Nordvik", "Stretch hiking pants with DWR, articulated knees, 4 pockets, adjustable hem."),
    ("Alpine Zip-Off Pants", "Pants", "pants", 99.95, "Terra Nova", "Zip-off legs turn them into shorts in seconds, UPF 50, quick-drying."),
    ("Rain Overtrousers", "Pants", "pants", 79.95, "Kustwerk", "Waterproof overtrousers with full-length side zips so you can put them on over boots."),
    ("Everyday Chino", "Pants", "pants", 84.95, "Kustwerk", "Stretch organic cotton chino, slim straight fit, for the city and easy walks."),
    ("Trail Tee", "T-Shirts", "layer", 34.95, "Nordvik", "Technical short-sleeve tee in recycled polyester, quick-drying, flatlock seams."),
    ("Merino Tee", "T-Shirts", "layer", 59.95, "Alpenrose", "150 g/m2 merino, soft against the skin, odour-resistant for multi-day trips."),
    ("Trail Runner Pro", "Running Shoes", "shoe", 139.95, "Nordvik", "Trail running shoe with 6 mm lugs, rock plate, 8 mm drop. Runs half a size small, order half a size up."),
    ("Road Racer", "Running Shoes", "shoe", 119.95, "Kustwerk", "Lightweight road shoe, 10 mm drop, responsive foam midsole, breathable mesh upper."),
    ("Summit GTX Hiking Boot", "Boots", "shoe", 199.95, "Alpenrose", "Waterproof leather-and-textile hiking boot, GORE-TEX membrane, Vibram sole, stiff midsole for heavy loads."),
    ("Winter Trek Boot", "Boots", "shoe", 179.95, "Nordvik", "Insulated winter boot rated to -25 C, waterproof, rubber outsole with winter grip."),
    ("City Sneaker", "Sneakers", "shoe", 89.95, "Kustwerk", "Everyday leather sneaker with recycled cotton laces and a cushioned insole."),
    ("Approach Shoe", "Hiking Shoes", "shoe", 149.95, "Terra Nova", "Low-cut approach shoe with sticky rubber, precise toe box for scrambling and via ferratas."),
    ("Camp Sandal", "Sandals", "shoe", 59.95, "Terra Nova", "Adjustable-strap sandal with a cushioned footbed. Dries quickly, ideal for camp and water crossings."),
    ("Merino Socks 3-pack", "Socks", "acc", 39.95, "Alpenrose", "Three pairs of mid-weight merino hiking socks with cushioned heel and toe. Wash at 30 C, do not tumble dry."),
    ("Wool Beanie", "Headwear", "acc", 29.95, "Nordvik", "Double-layer merino wool beanie, soft inner lining. One size."),
    ("Touring Gloves", "Gloves", "acc", 49.95, "Nordvik", "Windproof touring gloves with touchscreen fingertips and a grippy palm."),
    ("Multifunction Buff", "Headwear", "acc", 19.95, "Kustwerk", "Seamless multifunction neck tube, 12 ways to wear it. Recycled polyester."),
    ("Everyday Backpack 20L", "Backpacks", "pack", 79.95, "Kustwerk", "20 litre daypack with laptop sleeve up to 15 inch, water-resistant fabric, chest strap."),
    ("Trail Pack 30L", "Backpacks", "pack", 129.95, "Nordvik", "30 litre hiking pack with ventilated back, hip belt pockets, rain cover included. 1.1 kg."),
    ("Alpine Pack 45L", "Backpacks", "pack", 179.95, "Alpenrose", "45 litre technical pack with adjustable back length, ice-axe loops and removable lid."),
    ("Dry Bag 20L", "Bags", "pack", 24.95, "Terra Nova", "Roll-top waterproof dry bag, welded seams, 20 litres."),
    ("Trekking Poles Carbon", "Trekking Gear", "pack", 99.95, "Alpenrose", "Pair of 3-section carbon poles with cork grips, 105-135 cm, 480 g per pair."),
    ("Headlamp 400", "Camping", "pack", 39.95, "Terra Nova", "400 lumen rechargeable headlamp with red night mode, IPX6 water protection."),
    ("Lightweight Hiker", "Hiking Shoes", "shoe", 129.95, "Terra Nova", "Low-cut breathable hiking shoe with a cushioned midsole and a grippy 4 mm lug outsole. Ideal for summer trails."),
    ("Trail Runner Lite", "Running Shoes", "shoe", 109.95, "Nordvik", "Lightweight everyday trail shoe with a 6 mm drop and a protective toe cap. Fits true to size."),
    ("Hut Slipper", "Sandals", "shoe", 44.95, "Terra Nova", "Insulated hut slipper with a non-slip sole. Packs flat, 280 g per pair."),
    ("Hiking Shorts", "Pants", "pants", 69.95, "Nordvik", "Quick-drying stretch shorts with a zipped thigh pocket and UPF 40 protection."),
    ("Base Layer Leggings", "Fleece & Midlayers", "layer", 64.95, "Alpenrose", "Merino-blend leggings, flatlock seams, warm under hiking pants and ski trousers."),
    ("Sun Hoody", "T-Shirts", "layer", 54.95, "Kustwerk", "Lightweight UPF 50 hooded long-sleeve shirt for sunny days and sparse shade."),
    ("Insulated Vest", "Jackets", "jacket", 119.95, "Nordvik", "Sleeveless synthetic-insulated vest, warm core layer that stays warm when wet. Packs into a pocket."),
    ("Light Rain Pants", "Pants", "pants", 69.95, "Kustwerk", "Light 2.5-layer rain pants with ankle zips, 10,000 mm water column, 230 g."),
    ("Pile Fleece Jacket", "Fleece & Midlayers", "layer", 109.95, "Nordvik", "Retro high-pile fleece jacket, very warm, with two zipped pockets."),
    ("Softshell Pants", "Pants", "pants", 119.95, "Nordvik", "Wind- and water-resistant softshell trousers, articulated knees, reinforced seat."),
    ("Kids Fjord Rain Jacket", "Kids", "kid", 79.95, "Nordvik", "Waterproof kids' rain jacket, taped seams, reflective details, hood fits over a helmet. Sizes grow with the child: sleeves have 2 cm of extra length."),
    ("Kids Trail Fleece", "Kids", "kid", 49.95, "Nordvik", "Soft recycled fleece for kids with chin guard and zipped pocket. Machine washable at 40 C."),
    ("Kids Hiking Boot", "Kids", "kidshoe", 74.95, "Terra Nova", "Waterproof kids' hiking boot with a flexible sole and an easy-pull lacing. Fits true to size, leave 1 cm of room."),
]

_DESC_TAILS = [
    "Shipped from our warehouse in Venlo, the Netherlands.",
    "Free returns within 30 days in Germany and the Netherlands.",
    "Designed in Amsterdam, tested in the Alps.",
]


@dataclass
class Variant:
    id: str
    title: str
    sku: str
    price: str
    compare_at: str | None
    options: list[tuple[str, str]]
    inventory: int
    policy: str = "DENY"

    @property
    def available(self) -> bool:
        return self.inventory > 0 or self.policy == "CONTINUE"


@dataclass
class Product:
    id: str
    title: str
    handle: str
    description: str
    product_type: str
    vendor: str
    tags: list[str]
    status: str
    published: bool
    created_at: datetime
    variants: list[Variant] = field(default_factory=list)

    @property
    def total_inventory(self) -> int:
        return sum(max(v.inventory, 0) for v in self.variants)

    @property
    def min_price(self) -> str:
        return min((v.price for v in self.variants), key=float)

    @property
    def max_price(self) -> str:
        return max((v.price for v in self.variants), key=float)


def _gid(kind: str, n: int) -> str:
    return f"gid://shopify/{kind}/{n}"


def build_products(now: datetime) -> list[Product]:
    rnd = random.Random(20261018)
    products: list[Product] = []
    pid, vid = 8_100_000_000_000, 44_200_000_000_000
    entries = []
    for line in LINES:
        entries.append((line, "", "men"))
        if line[2] in {"jacket", "layer", "pants", "shoe"}:  # like real shops: separate men's and women's products
            entries.append((line, " Women", "women"))
    for (base_title, ptype, kind, price, vendor, desc), suffix, gender in entries:
        title = base_title + suffix
        colorways = rnd.sample(COLORS[kind], k=min(len(COLORS[kind]), rnd.choice([3, 4, 4, 5])))
        if title == "Alpine Shell Jacket":  # 5 colours x 7 sizes = 35 variants: exercises variant pagination (> 30 per page)
            colorways = list(COLORS[kind])
        if kind == "kid":
            sizes = SIZES_KIDS
        elif kind == "kidshoe":
            sizes = SIZES_KIDS_SHOE
        elif kind == "shoe":
            sizes = SIZES_SHOE_W if gender == "women" else SIZES_SHOE
        elif ptype == "Socks":
            sizes = ["S/M", "L/XL"]
        elif kind in {"acc", "pack"}:
            sizes = ["One size"]
        else:
            sizes = SIZES_APPAREL[:5] if gender == "women" else SIZES_APPAREL
        if title == "Alpine Shell Jacket":
            sizes = [*SIZES_APPAREL, "3XL"]
        pid += rnd.randint(1_000, 99_999)
        slug = title.lower().replace(" ", "-").replace("&", "and")
        variants = []
        for ci, color in enumerate(colorways):
            for size in sizes:
                vid += rnd.randint(1, 40)
                sku = f"NV-{hashlib.md5(title.encode()).hexdigest()[:4].upper()}-{color[:3].upper()}-{size.replace(' ', '').replace('/', '')}"  # noqa: S324
                stock = max(0, int(rnd.gauss(9, 7)))
                if rnd.random() < 0.12:
                    stock = 0
                sale = rnd.random() < 0.10 and ci == 0
                p = price * (0.8 if sale else 1)
                variants.append(Variant(
                    id=_gid("ProductVariant", vid), title=f"{color} / {size}" if size != "One size" else color, sku=sku,
                    price=f"{p - 0.05 if p > 20 else p:.2f}", compare_at=f"{price - 0.05:.2f}" if sale else None,
                    options=[("Color", color), ("Size", size)] if size != "One size" else [("Color", color)], inventory=stock,
                ))
        tags = [ptype.lower().replace(" & ", "-").replace(" ", "-"), "womens" if gender == "women" else "mens", "waterproof" if "waterproof" in desc.lower() else "everyday"]
        if "merino" in desc.lower():
            tags.append("merino")
        products.append(Product(
            id=_gid("Product", pid), title=title, handle=slug,
            description=f"{desc} {'Women-specific cut. ' if gender == 'women' else ''}{rnd.choice(_DESC_TAILS)}", product_type=ptype, vendor=vendor, tags=tags,
            status="ACTIVE", published=True, created_at=now - timedelta(days=rnd.randint(30, 700)), variants=variants,
        ))
    # a draft and an archived product: the sync must ignore them; one active product not published to the online store
    products.append(Product(_gid("Product", pid + 11), "Prototype Shell 2027", "prototype-shell-2027", "Not released.", "Jackets", "Nordvik",
                            ["prototype"], "DRAFT", False, now, [Variant(_gid("ProductVariant", vid + 1), "Black / M", "NV-PROTO", "299.00", None,
                                                                         [("Color", "Black"), ("Size", "M")], 0)]))
    products.append(Product(_gid("Product", pid + 12), "Old Season Parka 2022", "old-season-parka-2022", "Discontinued.", "Jackets", "Nordvik",
                            ["archive"], "ARCHIVED", False, now - timedelta(days=900),
                            [Variant(_gid("ProductVariant", vid + 2), "Navy / L", "NV-OLDPARKA", "99.00", None, [("Color", "Navy"), ("Size", "L")], 3)]))
    products.append(Product(_gid("Product", pid + 13), "Wholesale Only Tent", "wholesale-only-tent", "B2B channel only.", "Camping", "Terra Nova",
                            ["b2b"], "ACTIVE", False, now - timedelta(days=100),
                            [Variant(_gid("ProductVariant", vid + 3), "Default Title", "NV-TENT", "349.00", None, [], 5)]))
    return products


# ------------------------------------------------------------------ orders

CARRIERS = {
    "DHL": ("https://www.dhl.com/de-de/home/tracking/tracking-parcel.html?submit=1&tracking-id={n}", "0034043416{:010d}"),
    "PostNL": ("https://jouw.postnl.nl/track-and-trace/{n}-NL-{zip}", "3SABCD{:09d}"),
    "DPD": ("https://tracking.dpd.de/status/en_US/parcel/{n}", "0155{:010d}"),
}


@dataclass
class Fulfillment:
    created_at: datetime
    status: str
    display_status: str
    company: str | None
    number: str | None
    url: str | None
    in_transit_at: datetime | None = None
    delivered_at: datetime | None = None
    estimated_delivery_at: datetime | None = None


@dataclass
class Order:
    number: int
    email: str | None
    created_at: datetime
    financial: str
    fulfillment_display: str
    lines: list[tuple[str, str, int, str]]  # title, variant title, qty, sku
    country: str
    city: str
    total: str
    fulfillments: list[Fulfillment] = field(default_factory=list)
    cancelled_at: datetime | None = None
    cancel_reason: str | None = None
    return_status: str = "NO_RETURN"
    test: bool = False
    closed: bool = False

    @property
    def name(self) -> str:
        return f"#{self.number}"


def _fulfil(created: datetime, carrier: str, seq: int, *, display: str, transit_after_h: int | None = 18,
            delivered_after_d: float | None = None, eta_days: float = 3, zip_: str = "1011AB") -> Fulfillment:
    url_tpl, num_tpl = CARRIERS[carrier]
    number = num_tpl.format(seq)
    return Fulfillment(
        created_at=created, status="SUCCESS", display_status=display, company=carrier, number=number,
        url=url_tpl.format(n=number, zip=zip_),
        in_transit_at=created + timedelta(hours=transit_after_h) if transit_after_h is not None else None,
        delivered_at=created + timedelta(days=delivered_after_d) if delivered_after_d is not None else None,
        estimated_delivery_at=created + timedelta(days=eta_days),
    )


def build_orders(now: datetime) -> list[Order]:
    d = lambda days, hours=0: now - timedelta(days=days, hours=hours)  # noqa: E731
    o: list[Order] = []
    # 1001-1004 keep their identity (used by tests and docs)
    o.append(Order(1001, "anna.schmidt@example.de", d(12), "PAID", "FULFILLED", [("Trail Runner Pro", "Black / EU 42", 1, "NV-4F2A-BLA-EU42")], "DE", "Köln", "139.95",
                   [_fulfil(d(10), "DHL", 4161094042557 % 10**10, display="DELIVERED", delivered_after_d=2.5, eta_days=3)], closed=True))
    o.append(Order(1002, "jan.devries@example.nl", d(5), "PAID", "FULFILLED",
                   [("Alpine Shell Jacket", "Navy / M", 1, "NV-9C1D-NAV-M"), ("Merino Socks 3-pack", "Heather Grey / S/M", 2, "NV-77AA-HEA-SM")], "NL", "Utrecht", "329.85",
                   [_fulfil(d(2), "PostNL", 123456789, display="IN_TRANSIT", eta_days=3, zip_="3511AB")]))
    o.append(Order(1003, "emma.jones@example.com", d(1), "PAID", "UNFULFILLED", [("City Sneaker", "Stone Grey / EU 40", 1, "NV-3B7E-STO-EU40")], "BE", "Antwerpen", "89.95"))
    o.append(Order(1004, "lukas.meier@example.de", d(9), "REFUNDED", "UNFULFILLED", [("Everyday Backpack 20L", "Black", 1, "NV-1D55-BLA")], "DE", "Berlin", "79.95",
                   cancelled_at=d(8, 20), cancel_reason="CUSTOMER", closed=True))
    # delivered / in transit / delayed / out for delivery / attempted / pickup point
    o.append(Order(1005, "sophie.martin@example.com", d(20), "PAID", "FULFILLED", [("Glacier Down Jacket", "Black / L", 1, "NV-5E10-BLA-L")], "AT", "Wien", "229.95",
                   [_fulfil(d(18), "DHL", 5000000005, display="DELIVERED", delivered_after_d=4, eta_days=4)], closed=True))
    o.append(Order(1006, "mark.visser@example.nl", d(4), "PAID", "FULFILLED", [("Summit GTX Hiking Boot", "Black / EU 44", 1, "NV-8A92-BLA-EU44")], "NL", "Rotterdam", "199.95",
                   [_fulfil(d(1), "PostNL", 234567890, display="OUT_FOR_DELIVERY", eta_days=1.0, zip_="3011AB")]))
    o.append(Order(1007, "lena.fischer@example.de", d(8), "PAID", "FULFILLED", [("Trail Pack 30L", "Forest Green", 1, "NV-6C03-FOR")], "DE", "Hamburg", "129.95",
                   [_fulfil(d(6), "DHL", 7000000007, display="DELAYED", eta_days=3)]))
    o.append(Order(1008, "tom.janssen@example.nl", d(7), "PAID", "FULFILLED", [("Fjord Rain Jacket", "Black / L", 1, "NV-2B44-BLA-L")], "BE", "Gent", "149.95",
                   [_fulfil(d(5), "PostNL", 345678901, display="ATTEMPTED_DELIVERY", eta_days=2, zip_="9000")]))
    o.append(Order(1009, "clara.weber@example.de", d(10), "PAID", "FULFILLED", [("Trail Fleece Half-Zip", "Charcoal / M", 1, "NV-0F12-CHA-M")], "DE", "München", "79.95",
                   [_fulfil(d(8), "DHL", 9000000009, display="READY_FOR_PICKUP", eta_days=3, delivered_after_d=None)]))
    # partially fulfilled, two shipments, split carriers
    o.append(Order(1010, "peter.bakker@example.nl", d(6), "PAID", "PARTIALLY_FULFILLED",
                   [("Alpine Pack 45L", "Black", 1, "NV-AA45-BLA"), ("Headlamp 400", "Black", 1, "NV-HL40-BLA"), ("Trekking Poles Carbon", "Black", 1, "NV-TP10-BLA")],
                   "NL", "Eindhoven", "319.85", [_fulfil(d(1), "PostNL", 456789012, display="IN_TRANSIT", eta_days=3, zip_="5611AB")]))
    o.append(Order(1011, "julia.hoffmann@example.de", d(14), "PAID", "FULFILLED",
                   [("Merino Base Layer Crew", "Black / S", 1, "NV-ME90-BLA-S"), ("Merino Tee", "Sand / S", 1, "NV-MT59-SAN-S")], "DE", "Stuttgart", "149.90",
                   [_fulfil(d(12), "DHL", 1100000011, display="DELIVERED", delivered_after_d=2, eta_days=3),
                    _fulfil(d(11), "DHL", 1100000012, display="DELIVERED", delivered_after_d=3, eta_days=3)], closed=True))
    # money situations
    o.append(Order(1012, "oliver.smith@example.com", d(16), "PARTIALLY_REFUNDED", "FULFILLED", [("Everyday Chino", "Khaki / L", 2, "NV-EC85-KHA-L")], "FR", "Lyon", "169.90",
                   [_fulfil(d(14), "DPD", 1200000012, display="DELIVERED", delivered_after_d=5, eta_days=5)], return_status="RETURNED", closed=True))
    o.append(Order(1013, "eva.dekker@example.nl", d(11), "PAID", "FULFILLED", [("Winter Trek Boot", "Black / EU 41", 1, "NV-WT17-BLA-EU41")], "NL", "Groningen", "179.95",
                   [_fulfil(d(9), "PostNL", 567890123, display="DELIVERED", delivered_after_d=1.5, eta_days=2)], return_status="IN_PROGRESS"))
    o.append(Order(1014, "karl.brandt@example.de", d(2), "PENDING", "ON_HOLD", [("Nordic Insulated Parka", "Navy / XL", 1, "NV-NP30-NAV-XL")], "DE", "Frankfurt", "299.95"))
    o.append(Order(1015, "marie.dubois@example.com", d(3), "AUTHORIZED", "UNFULFILLED", [("Ridge Softshell Jacket", "Black / M", 1, "NV-RS14-BLA-M")], "FR", "Paris", "139.95"))
    o.append(Order(1016, "noah.mulder@example.nl", d(30), "PAID", "FULFILLED", [("Camp Sandal", "Olive / EU 43", 1, "NV-CS60-OLI-EU43")], "NL", "Maastricht", "59.95",
                   [_fulfil(d(28), "PostNL", 678901234, display="DELIVERED", delivered_after_d=2, eta_days=2)], closed=True))
    o.append(Order(1017, "hannah.klein@example.de", d(0, 5), "PAID", "UNFULFILLED", [("Wool Beanie", "Black / One size", 1, "NV-WB30-BLA"), ("Touring Gloves", "Black", 1, "NV-TG50-BLA")], "AT", "Graz", "79.90"))
    # an order older than 60 days: invisible without the read_all_orders scope (like the real API)
    o.append(Order(950, "legacy.customer@example.com", d(75), "PAID", "FULFILLED", [("Trail Tee", "Black / M", 1, "NV-TT35-BLA-M")], "DE", "Bonn", "34.95",
                   [_fulfil(d(73), "DHL", 9500000950, display="DELIVERED", delivered_after_d=2, eta_days=3)], closed=True))
    # guest checkout without an email on the order (the bot can never verify it)
    o.append(Order(1018, None, d(5), "PAID", "FULFILLED", [("City Sneaker", "Stone Grey / EU 42", 1, "NV-CS89-STO-EU42")], "DE", "Leipzig", "89.95",
                   [_fulfil(d(1), "DHL", 1800000018, display="IN_TRANSIT", eta_days=3)]))
    # a test order created by the merchant
    o.append(Order(1019, "qa@nordvik-outdoor.example", d(1), "PAID", "UNFULFILLED", [("Trail Tee", "Black / M", 1, "NV-TT35-BLA-M")], "NL", "Amsterdam", "34.95", test=True))
    return o


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)
