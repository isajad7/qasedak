import re


PERSIAN_ARABIC_DIGIT_TRANSLATION = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)

AMOUNT_KEYWORDS = (
    "مبلغ",
    "پرداخت",
    "پرداختی",
    "واریز",
    "واریزی",
    "انتقال",
    "برداشت",
    "خرید",
    "تراکنش",
    "رسید",
    "ریال",
    "تومان",
    "کارت به کارت",
)
NOISE_KEYWORDS = (
    "شماره",
    "پیگیری",
    "مرجع",
    "ارجاع",
    "کارت",
    "حساب",
    "تاریخ",
    "ساعت",
    "موجودی",
    "شناسه",
    "کد",
)
NUMBER_RE = re.compile(r"(?<!\d)([0-9][0-9,\s،٬.]{2,}[0-9]|[0-9]{4,})(?!\d)")


def normalize_receipt_text(value):
    return str(value or "").translate(PERSIAN_ARABIC_DIGIT_TRANSLATION)


def expected_amount_in_rial(amount, currency):
    try:
        amount = int(amount or 0)
    except (TypeError, ValueError):
        amount = 0
    if currency == "TOMAN":
        return amount * 10
    if currency == "IRR":
        return amount
    return None


def _compact_digits(value):
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _context_for(text, start, end, radius=32):
    return text[max(0, start - radius) : min(len(text), end + radius)]


def _looks_like_time_or_date(text, start, end):
    nearby = text[max(0, start - 2) : min(len(text), end + 2)]
    return ":" in nearby or "/" in nearby or "-" in nearby


def _score_candidate(context, digit_count):
    score = 0
    for keyword in AMOUNT_KEYWORDS:
        if keyword in context:
            score += 3 if keyword in {"مبلغ", "ریال", "تومان"} else 2
    for keyword in NOISE_KEYWORDS:
        if keyword in context:
            score -= 2
    if digit_count in {4, 6}:
        score -= 2
    if digit_count >= 12:
        score -= 3
    return score


def extract_receipt_amount_candidates(text):
    normalized = normalize_receipt_text(text)
    candidates = []
    seen = set()

    for match in NUMBER_RE.finditer(normalized):
        raw = match.group(1).strip()
        digits = _compact_digits(raw)
        if not digits:
            continue
        amount = int(digits)
        if amount < 1000:
            continue

        context = _context_for(normalized, match.start(), match.end())
        if _looks_like_time_or_date(normalized, match.start(), match.end()) and not any(
            keyword in context for keyword in AMOUNT_KEYWORDS
        ):
            continue

        amount_in_rial = amount
        if "تومان" in context and "ریال" not in context:
            amount_in_rial = amount * 10

        key = (amount_in_rial, match.start(), match.end())
        if key in seen:
            continue
        seen.add(key)

        score = _score_candidate(context, len(digits))
        candidates.append(
            {
                "raw": raw,
                "amount": amount,
                "amount_irr": amount_in_rial,
                "score": score,
                "context": context.strip(),
            }
        )

    candidates.sort(key=lambda item: (item["score"], len(str(item["amount_irr"]))), reverse=True)
    return candidates


def analyze_receipt_text(text, *, expected_amount, currency):
    expected_irr = expected_amount_in_rial(expected_amount, currency)
    base = {
        "status": "skipped",
        "requires_admin_review": True,
        "expected_amount": int(expected_amount or 0),
        "expected_currency": currency,
        "expected_amount_irr": expected_irr,
        "matched_amount_irr": None,
        "matched_raw": "",
        "candidates": [],
        "warning": "",
    }

    if expected_irr is None:
        base.update(
            {
                "status": "unsupported_currency",
                "warning": "واحد سفارش برای بررسی خودکار رسید پشتیبانی نمی‌شود؛ رسید باید دستی بررسی شود.",
            }
        )
        return base

    if not str(text or "").strip():
        base.update(
            {
                "status": "image_only",
                "warning": "رسید متنی وارد نشده است؛ رسید باید دستی بررسی شود.",
            }
        )
        return base

    candidates = extract_receipt_amount_candidates(text)
    base["candidates"] = candidates[:6]

    exact_match = next((item for item in candidates if item["amount_irr"] == expected_irr), None)
    if exact_match:
        base.update(
            {
                "status": "matched",
                "requires_admin_review": False,
                "matched_amount_irr": exact_match["amount_irr"],
                "matched_raw": exact_match["raw"],
                "warning": "",
            }
        )
        return base

    usable_candidates = [item for item in candidates if item["score"] >= -1]
    if usable_candidates:
        best = usable_candidates[0]
        base.update(
            {
                "status": "mismatch",
                "matched_amount_irr": best["amount_irr"],
                "matched_raw": best["raw"],
                "warning": (
                    "مبلغی که از متن رسید پیدا شد با مبلغ سفارش یکی نیست؛ "
                    "سفارش ثبت می‌شود ولی رسید باید دستی بررسی شود."
                ),
            }
        )
        return base

    base.update(
        {
            "status": "not_found",
            "warning": "مبلغ قابل مقایسه‌ای در متن رسید پیدا نشد؛ سفارش ثبت می‌شود ولی رسید باید دستی بررسی شود.",
        }
    )
    return base
