from copy import deepcopy


class ExperimentEngine:
    """Build safe A/B variants from the rule-engine decision."""

    VARIANT_SPECS = {
        "upsell": [
            {
                "variant": "A",
                "label": "10% discount",
                "fields": {"discount": 10},
                "message": "🎯 پیشنهاد تست A: 10٪ تخفیف برای ارتقا فعال است.",
            },
            {
                "variant": "B",
                "label": "1GB bonus",
                "fields": {"bonus_gb": 1},
                "message": "🎁 پیشنهاد تست B: 1GB حجم هدیه به خرید اضافه می‌شود.",
            },
            {
                "variant": "C",
                "label": "speed boost",
                "fields": {"add_on": "speed_boost"},
                "message": "🚀 پیشنهاد تست C: اولویت سرعت برای تجربه روان‌تر.",
            },
        ],
        "renewal": [
            {
                "variant": "A",
                "label": "current renewal discount",
                "fields": {},
                "message": "⏳ پیشنهاد تست A: تمدید سریع با همین پیشنهاد فعال است.",
            },
            {
                "variant": "B",
                "label": "renewal bonus",
                "fields": {"bonus_gb": 1},
                "message": "🎁 پیشنهاد تست B: 1GB حجم هدیه برای تمدید.",
            },
            {
                "variant": "C",
                "label": "renewal reassurance",
                "fields": {"support_priority": True},
                "message": "✅ پیشنهاد تست C: تمدید با پشتیبانی اولویت‌دار.",
            },
        ],
        "retention": [
            {
                "variant": "A",
                "label": "winback discount",
                "fields": {},
                "message": "💙 پیشنهاد تست A: پیشنهاد بازگشت برای شما فعال است.",
            },
            {
                "variant": "B",
                "label": "winback bonus",
                "fields": {"bonus_gb": 1},
                "message": "🎁 پیشنهاد تست B: 1GB هدیه برای بازگشت.",
            },
            {
                "variant": "C",
                "label": "support led winback",
                "fields": {"support_priority": True},
                "message": "🛟 پیشنهاد تست C: بازگشت با کمک پشتیبانی.",
            },
        ],
    }

    def generate_variants(self, offer, offer_type=None):
        if not offer:
            return []

        normalized_type = self.normalize_offer_type(offer_type or offer.get("optimization_offer_type") or offer.get("type"))
        specs = self.VARIANT_SPECS.get(normalized_type, self.VARIANT_SPECS["renewal"])
        return [self._variant_from_spec(offer, normalized_type, spec) for spec in specs]

    def normalize_offer_type(self, offer_type):
        raw = str(offer_type or "").strip().lower()
        if "upsell" in raw:
            return "upsell"
        if "retention" in raw or "support_check_in" in raw:
            return "retention"
        if "renewal" in raw or "near_expiry" in raw or "expired" in raw or "high_usage" in raw:
            return "renewal"
        return raw or "renewal"

    def _variant_from_spec(self, offer, offer_type, spec):
        variant = deepcopy(offer)
        base_message = str(variant.get("message") or "").strip()
        variant["optimization_offer_type"] = offer_type
        variant["experiment_variant"] = spec["variant"]
        variant["experiment_label"] = spec["label"]
        variant["experiment_id"] = f"{offer_type}:{variant.get('type', 'offer')}"

        for field, value in spec.get("fields", {}).items():
            if field == "discount":
                variant[field] = max(int(variant.get(field) or 0), int(value))
            elif field == "bonus_gb":
                variant[field] = max(int(variant.get(field) or 0), int(value))
            elif field == "add_on" and variant.get("add_on"):
                variant.setdefault("experiment_add_on", value)
            else:
                variant[field] = value

        variant_message = spec.get("message") or ""
        if base_message and variant_message:
            variant["message"] = f"{base_message}\n\n{variant_message}"
        elif variant_message:
            variant["message"] = variant_message

        return variant
