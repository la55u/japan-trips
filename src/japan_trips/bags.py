from __future__ import annotations

# --- Baggage fees ------------------------------------------------------------
# Exact per-airline bag fees instead of a flat estimate. Luggage need: ONE
# checked bag SHARED between the travellers plus ONE carry-on per person.
# Google and OTA fares are base fares WITHOUT baggage (verified 2026-09:
# Google prices are identical with the checked_bags query param at 0 or 1 —
# the param only filters availability).
# Fees are online/prepaid rates in EUR: (checked fee per direction for 1 bag,
# cabin fee per person per direction). 0.0 = included in the cheapest
# bookable economy fare. Checked once per direction per charging carrier.
# Sources (checked 2026-09): airline fee charts / ticket-type pages:
#   Scoot fees chart (20kg pre-purchase ~SGD 65 long-haul sector); Wizz Air
#   (20kg ~€25-45/sector, cabin trolley needs WIZZ Priority ~€10-20); Cebu
#   Pacific (intl 20kg prepaid ~PHP 2,000, 7kg cabin incl.); Jeju Air (Basic
#   fare 0kg, 1st bag USD 45 online, 10kg cabin incl.); Eastar Jet (promo
#   fare excludes bags, 15kg KRW 30,000); Jetstar (15kg ~¥3,500/sector);
#   Lufthansa-group Economy Light (1st bag ~€70/route intercontinental);
#   Finnair Economy Light (1st bag €75-100 prepaid); British Airways Basic
#   (1st bag ~£60-75/sector); Condor Economy Zero (20kg from €59.99, cabin
#   8kg from €29.99 — both paid). Unknown carriers are treated as
#   bag-inclusive (most are full-service with a 23kg allowance).
BAG_POLICY = {
    "scoot": (45.0, 0.0),
    "wizz": (35.0, 15.0),
    "cebu": (30.0, 0.0),
    "jeju": (42.0, 0.0),
    "eastar": (21.0, 0.0),
    "jetstar": (25.0, 0.0),
    "lufthansa": (70.0, 0.0),
    "austrian": (70.0, 0.0),
    "swiss": (70.0, 0.0),
    "brussels": (70.0, 0.0),
    "finnair": (75.0, 0.0),
    "british airways": (70.0, 0.0),
    "condor": (60.0, 30.0),
}
BAG_UNKNOWN_POLICY = (0.0, 0.0)


def _bag_fees(carrier):
    """(checked EUR/direction, cabin EUR/person/direction) for one carrier."""
    low = carrier.lower()
    for frag, fees in BAG_POLICY.items():
        if frag in low:
            return fees
    return BAG_UNKNOWN_POLICY


def bag_fees_for_legs(directions, adults):
    """Compute the exact bag cost for an itinerary.

    directions: list of (label, [carrier names]) — one entry per flight
    direction (outbound, return). Baggage need: 1 checked bag shared by the
    party + 1 carry-on per person. Returns (bag_fee_pp, items, all_included)
    where bag_fee_pp is the per-person EUR cost, items is a list of
    (label, per-person EUR) for the detail dialog, and all_included is True
    when every carrier includes both bags in the fare."""
    checked_party = 0.0
    cabin_party = 0.0
    items = []
    for label, carriers in directions:
        carriers = carriers or []
        charging = sorted({c for c in carriers if _bag_fees(c)[0] > 0})
        # Sum of unique charging carriers (conservative for mixed-carrier
        # fares; through-ticketed multi-sector journeys usually charge once).
        for c in charging:
            fee = _bag_fees(c)[0]
            checked_party += fee
            items.append((f"Checked bag · {c} ({label})", fee / adults))
        for c in sorted({c for c in carriers if _bag_fees(c)[1] > 0}):
            cabin_party += _bag_fees(c)[1] * adults
            items.append((f"Cabin bag · {c} ({label})", _bag_fees(c)[1]))
    return (checked_party + cabin_party) / adults, items, not items
