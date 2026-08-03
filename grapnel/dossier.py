"""Vessel dossiers: everything an analyst needs to leave this tool and keep working.

GRAPNEL is a tripwire, not a conclusion. When it flags a hull, the useful
output is not a verdict - it is a complete, unedited copy of what that hull
self-reported, plus every pivot needed to check it against sources that are
not AIS. The self-reported fields are the *claim*. The pivots are how the claim
gets tested.

Three things are computed locally rather than looked up, because they are
cheap, deterministic, and immediately decisive:

  MID decoding. The first three digits of an MMSI are the Maritime
  Identification Digits and encode the administration that issued the station
  licence. A hull broadcasting a Cameroon MID while claiming a Panama flag in
  a registry has a discrepancy worth explaining.

  IMO check digit. IMO numbers carry a modulo-10 checksum. A hull broadcasting
  an IMO that fails the checksum is broadcasting a number that was never
  issued. In the January 2025 Taiwan case the suspect vessel was reported as
  not existing in IMO records at all, and was later assessed to be alternating
  between two transponder identities.

  Identity churn. If the same MMSI reports more than one name, callsign or IMO
  inside one observation window, that is recorded verbatim. It is one of the
  few AIS-internal signals that cannot be explained away by receiver coverage.
"""

from __future__ import annotations

import re
from urllib.parse import quote

import pandas as pd

# Maritime Identification Digits -> issuing administration.
# Not exhaustive; weighted toward flags that recur in seabed-infrastructure
# casework and toward the North European and East Asian littorals.
MID = {
    "201": "Albania", "202": "Andorra", "203": "Austria", "204": "Azores (PT)",
    "205": "Belgium", "206": "Belarus", "207": "Bulgaria", "208": "Vatican",
    "209": "Cyprus", "210": "Cyprus", "211": "Germany", "212": "Cyprus",
    "213": "Georgia", "214": "Moldova", "215": "Malta", "216": "Armenia",
    "218": "Germany", "219": "Denmark", "220": "Denmark", "224": "Spain",
    "225": "Spain", "226": "France", "227": "France", "228": "France",
    "229": "Malta", "230": "Finland", "231": "Faroe Islands", "232": "United Kingdom",
    "233": "United Kingdom", "234": "United Kingdom", "235": "United Kingdom",
    "236": "Gibraltar", "237": "Greece", "238": "Croatia", "239": "Greece",
    "240": "Greece", "241": "Greece", "242": "Morocco", "243": "Hungary",
    "244": "Netherlands", "245": "Netherlands", "246": "Netherlands",
    "247": "Italy", "248": "Malta", "249": "Malta", "250": "Ireland",
    "251": "Iceland", "252": "Liechtenstein", "253": "Luxembourg", "254": "Monaco",
    "255": "Madeira (PT)", "256": "Malta", "257": "Norway", "258": "Norway",
    "259": "Norway", "261": "Poland", "262": "Montenegro", "263": "Portugal",
    "264": "Romania", "265": "Sweden", "266": "Sweden", "267": "Slovakia",
    "268": "San Marino", "269": "Switzerland", "270": "Czechia", "271": "Turkey",
    "272": "Ukraine", "273": "Russian Federation", "274": "North Macedonia",
    "275": "Latvia", "276": "Estonia", "277": "Lithuania", "278": "Slovenia",
    "279": "Serbia", "301": "Anguilla", "303": "Alaska (US)", "304": "Antigua & Barbuda",
    "305": "Antigua & Barbuda", "306": "Curacao / Sint Maarten / BES",
    "307": "Aruba", "308": "Bahamas", "309": "Bahamas", "310": "Bermuda",
    "311": "Bahamas", "312": "Belize", "314": "Barbados", "316": "Canada",
    "319": "Cayman Islands", "321": "Costa Rica", "323": "Cuba",
    "325": "Dominica", "327": "Dominican Republic", "329": "Guadeloupe",
    "330": "Grenada", "331": "Greenland", "332": "Guatemala", "334": "Honduras",
    "336": "Haiti", "338": "United States", "339": "Jamaica", "341": "St Kitts & Nevis",
    "343": "St Lucia", "345": "Mexico", "347": "Martinique", "348": "Montserrat",
    "350": "Nicaragua", "351": "Panama", "352": "Panama", "353": "Panama",
    "354": "Panama", "355": "Panama", "356": "Panama", "357": "Panama",
    "358": "Puerto Rico", "359": "El Salvador", "361": "St Pierre & Miquelon",
    "362": "Trinidad & Tobago", "364": "Turks & Caicos", "366": "United States",
    "367": "United States", "368": "United States", "369": "United States",
    "370": "Panama", "371": "Panama", "372": "Panama", "373": "Panama",
    "374": "Panama", "375": "St Vincent & the Grenadines",
    "376": "St Vincent & the Grenadines", "377": "St Vincent & the Grenadines",
    "378": "British Virgin Islands", "379": "US Virgin Islands", "401": "Afghanistan",
    "403": "Saudi Arabia", "405": "Bangladesh", "408": "Bahrain", "410": "Bhutan",
    "412": "China", "413": "China", "414": "China", "416": "Taiwan",
    "417": "Sri Lanka", "419": "India", "422": "Iran", "423": "Azerbaijan",
    "425": "Iraq", "428": "Israel", "431": "Japan", "432": "Japan",
    "434": "Turkmenistan", "436": "Kazakhstan", "437": "Uzbekistan",
    "438": "Jordan", "440": "South Korea", "441": "South Korea", "443": "Palestine",
    "445": "North Korea", "447": "Kuwait", "450": "Lebanon", "451": "Kyrgyzstan",
    "453": "Macao", "455": "Maldives", "457": "Mongolia", "459": "Nepal",
    "461": "Oman", "463": "Pakistan", "466": "Qatar", "468": "Syria",
    "470": "United Arab Emirates", "471": "United Arab Emirates",
    "472": "Tajikistan", "473": "Yemen", "475": "Yemen", "477": "Hong Kong",
    "478": "Bosnia & Herzegovina", "501": "Adelie Land", "503": "Australia",
    "506": "Myanmar", "508": "Brunei", "510": "Micronesia", "511": "Palau",
    "512": "New Zealand", "514": "Cambodia", "515": "Cambodia", "516": "Christmas Island",
    "518": "Cook Islands", "520": "Fiji", "523": "Cocos Islands", "525": "Indonesia",
    "529": "Kiribati", "531": "Laos", "533": "Malaysia", "536": "Northern Marianas",
    "538": "Marshall Islands", "540": "New Caledonia", "542": "Niue",
    "544": "Nauru", "546": "French Polynesia", "548": "Philippines",
    "553": "Papua New Guinea", "555": "Pitcairn", "557": "Solomon Islands",
    "559": "American Samoa", "561": "Samoa", "563": "Singapore", "564": "Singapore",
    "565": "Singapore", "566": "Singapore", "567": "Thailand", "570": "Tonga",
    "572": "Tuvalu", "574": "Vietnam", "576": "Vanuatu", "577": "Vanuatu",
    "578": "Wallis & Futuna", "601": "South Africa", "603": "Angola",
    "605": "Algeria", "607": "St Paul & Amsterdam", "608": "Ascension",
    "609": "Burundi", "610": "Benin", "611": "Botswana", "612": "Central African Rep",
    "613": "Cameroon", "615": "Congo", "616": "Comoros", "617": "Cabo Verde",
    "618": "Crozet Archipelago", "619": "Cote d'Ivoire", "620": "Comoros",
    "621": "Djibouti", "622": "Egypt", "624": "Ethiopia", "625": "Eritrea",
    "626": "Gabon", "627": "Ghana", "629": "Gambia", "630": "Guinea-Bissau",
    "631": "Equatorial Guinea", "632": "Guinea", "633": "Burkina Faso",
    "634": "Kenya", "635": "Kerguelen", "636": "Liberia", "637": "Liberia",
    "638": "South Sudan", "642": "Libya", "644": "Lesotho", "645": "Mauritius",
    "647": "Madagascar", "649": "Mali", "650": "Mozambique", "654": "Mauritania",
    "655": "Malawi", "656": "Niger", "657": "Nigeria", "659": "Namibia",
    "660": "Reunion", "661": "Rwanda", "662": "Sudan", "663": "Senegal",
    "664": "Seychelles", "665": "St Helena", "666": "Somalia", "667": "Sierra Leone",
    "668": "Sao Tome & Principe", "669": "Eswatini", "670": "Chad",
    "671": "Togo", "672": "Tunisia", "674": "Tanzania", "675": "Uganda",
    "676": "DR Congo", "677": "Tanzania", "678": "Zambia", "679": "Zimbabwe",
    "701": "Argentina", "710": "Brazil", "720": "Bolivia", "725": "Chile",
    "730": "Colombia", "735": "Ecuador", "740": "Falkland Islands",
    "745": "French Guiana", "750": "Guyana", "755": "Paraguay", "760": "Peru",
    "765": "Suriname", "770": "Uruguay", "775": "Venezuela",
}

# Registries repeatedly identified in open reporting as low-transparency or
# frequently used by the sanctioned tanker fleet. Presence here is context for
# a reviewer, not an allegation against any individual hull. Very large,
# entirely legitimate fleets fly several of these.
LOW_TRANSPARENCY_FLAGS = {
    "Cameroon", "Comoros", "Cook Islands", "Djibouti", "Eswatini", "Gabon",
    "Guinea-Bissau", "Guyana", "Honduras", "Palau", "Sao Tome & Principe",
    "Sierra Leone", "St Kitts & Nevis", "St Vincent & the Grenadines",
    "Tanzania", "Togo", "Vanuatu", "Mongolia", "Moldova", "Barbados",
}


def decode_mid(mmsi) -> dict:
    """Split an MMSI into its station class and issuing administration."""
    s = str(int(mmsi)).zfill(9) if mmsi not in (None, "", "nan") else ""
    if len(s) != 9:
        return {"valid": False, "reason": "MMSI is not 9 digits"}

    if s.startswith("00"):
        kind, mid = "Coast station", s[2:5]
    elif s.startswith("0"):
        kind, mid = "Group of ships", s[1:4]
    elif s.startswith("111"):
        kind, mid = "SAR aircraft", s[3:6]
    elif s.startswith("99"):
        kind, mid = "Aid to navigation", s[2:5]
    elif s.startswith("98"):
        kind, mid = "Craft associated with parent ship", s[2:5]
    elif s.startswith("970"):
        kind, mid = "AIS-SART", s[3:6]
    elif s.startswith("972"):
        kind, mid = "MOB device", s[3:6]
    elif s.startswith("974"):
        kind, mid = "EPIRB-AIS", s[3:6]
    else:
        kind, mid = "Ship station", s[:3]

    return {
        "valid": True,
        "mmsi": s,
        "station_class": kind,
        "mid": mid,
        "flag_from_mid": MID.get(mid),
        "low_transparency_registry": MID.get(mid) in LOW_TRANSPARENCY_FLAGS,
    }


def validate_imo(imo) -> dict:
    """IMO numbers carry a modulo-10 check digit over the first six digits."""
    raw = re.sub(r"[^0-9]", "", str(imo or ""))
    if len(raw) != 7:
        return {"present": bool(raw), "valid": False, "reason": "not a 7-digit IMO number"}
    total = sum(int(d) * w for d, w in zip(raw[:6], (7, 6, 5, 4, 3, 2)))
    ok = total % 10 == int(raw[6])
    return {
        "present": True,
        "imo": raw,
        "valid": ok,
        "reason": None if ok else "check digit does not validate - this number was never issued",
    }


def pivots(mmsi, imo=None, name=None, callsign=None, lat=None, lon=None, ts=None) -> list[dict]:
    """External sources to test the self-reported identity against.

    Grouped by what they let you establish. Nothing here is scraped: these are
    links the analyst opens themselves, which keeps GRAPNEL clear of every
    tracking provider's terms of service and keeps the provenance of any
    follow-on finding attributable to the analyst rather than to this tool.
    """
    m = str(mmsi)
    i = re.sub(r"[^0-9]", "", str(imo or ""))
    n = quote(str(name).strip()) if name else ""
    out = []

    def add(group, label, url, note=""):
        out.append({"group": group, "label": label, "url": url, "note": note})

    add("Position history", "MarineTraffic", f"https://www.marinetraffic.com/en/ais/details/ships/mmsi:{m}",
        "Port calls and track history; free tier is time-limited")
    add("Position history", "VesselFinder", f"https://www.vesselfinder.com/vessels?name={n or m}")
    add("Position history", "MyShipTracking", f"https://www.myshiptracking.com/vessels/mmsi-{m}")
    add("Position history", "Global Fishing Watch", f"https://globalfishingwatch.org/map/search?query={m}",
        "Free account; exposes AIS-off events and loitering for all vessel types")

    if i:
        add("Identity & registry", "Equasis", "https://www.equasis.org/EquasisWeb/public/HomePage",
            f"Free account required; search IMO {i}. Gives owner, manager, ISM and class history")
        add("Identity & registry", "IMO GISIS", "https://gisis.imo.org/Public/SHIPS/Default.aspx",
            f"Free account; authoritative on IMO {i} and on flag history")
        add("Identity & registry", "Baltic Shipping", f"https://www.balticshipping.com/vessel/imo/{i}")
        add("Port state control", "Paris MoU", "https://parismou.org/inspection-search/inspection-search",
            f"Deficiency and detention record for IMO {i}")
        add("Port state control", "Tokyo MoU", "https://www.tokyo-mou.org/inspections_detentions/psc_database.php")
    add("Identity & registry", "ITU MARS", "https://www.itu.int/mmsapp/ShipStation/list",
        f"Station licence record behind MMSI {m}; confirms the issuing administration")

    if name:
        add("Ownership & sanctions", "OpenSanctions", f"https://www.opensanctions.org/search/?q={n}",
            "Aggregates OFAC, EU, UK OFSI and others; has vessel entities")
        add("Ownership & sanctions", "OFAC sanctions search", "https://sanctionssearch.ofac.treas.gov/")
        add("Ownership & sanctions", "EU consolidated list", "https://data.europa.eu/data/datasets/consolidated-list-of-persons-groups-and-entities-subject-to-eu-financial-sanctions")
        add("Ownership & sanctions", "UK OFSI list", "https://www.gov.uk/government/publications/financial-sanctions-consolidated-list-of-targets")
        add("Imagery", "ShipSpotting", f"https://www.shipspotting.com/photos/search?vesselName={n}",
            "Photographs; useful for confirming a hull matches its declared dimensions")

    if lat is not None and lon is not None:
        add("Overhead imagery", "Copernicus Browser",
            f"https://browser.dataspace.copernicus.eu/?zoom=11&lat={lat:.4f}&lng={lon:.4f}",
            "Free Sentinel-1 SAR. SAR sees hulls regardless of AIS, so this is how "
            "you test whether a gap was really a dark transit")
        add("Overhead imagery", "Sentinel-1 scene search",
            "https://search.asf.alaska.edu/",
            f"ASF Vertex; search {lat:.4f}, {lon:.4f} for the incident window")

    add("Connectivity corroboration", "IODA", "https://ioda.inetintel.cc.gatech.edu/",
        "Georgia Tech outage observatory; independent evidence a cable actually failed")
    add("Connectivity corroboration", "Cloudflare Radar", "https://radar.cloudflare.com/")
    add("Connectivity corroboration", "RIPE Atlas", "https://atlas.ripe.net/")

    return out


def build(mmsi: int, static_rows: pd.DataFrame, positions: pd.DataFrame | None = None,
          lat=None, lon=None, ts=None) -> dict:
    """Assemble the dossier for one MMSI from all static rows seen for it."""
    rows = static_rows[static_rows["mmsi"] == mmsi] if not static_rows.empty else static_rows

    def collect(col):
        if rows.empty or col not in rows:
            return []
        vals = [str(v).strip() for v in rows[col].dropna().unique() if str(v).strip() not in ("", "nan", "None")]
        return sorted(set(vals))

    names = collect("name")
    callsigns = collect("callsign")
    imos = collect("imo")
    ship_types = collect("ship_type")
    destinations = collect("destination")

    name = names[0] if names else None
    imo = imos[0] if imos else None

    def last(col):
        if rows.empty or col not in rows:
            return None
        s = rows[col].dropna()
        return None if s.empty else s.iloc[-1]

    mid = decode_mid(mmsi)
    imo_check = validate_imo(imo)

    churn = {}
    if len(names) > 1:
        churn["name"] = names
    if len(callsigns) > 1:
        churn["callsign"] = callsigns
    if len(imos) > 1:
        churn["imo"] = imos

    integrity = []
    if not mid.get("flag_from_mid"):
        integrity.append("MMSI MID is unassigned or unrecognised")
    if mid.get("low_transparency_registry"):
        integrity.append(
            f"MID resolves to {mid['flag_from_mid']}, a registry that appears frequently in "
            "open reporting on opaque ownership. Context only - many legitimate fleets fly it."
        )
    if imo_check.get("present") and not imo_check.get("valid"):
        integrity.append("Broadcast IMO number fails its check digit")
    if not imo_check.get("present"):
        integrity.append("No IMO number broadcast (not required for all hulls, but limits registry pivots)")
    if churn:
        integrity.append(f"Identity fields changed within the observation window: {', '.join(churn)}")

    return {
        "mmsi": int(mmsi),
        "self_reported": {
            "name": name,
            "callsign": callsigns[0] if callsigns else None,
            "imo": imo,
            "ship_type": ship_types[0] if ship_types else None,
            "cargo_type": last("cargo_type"),
            "length_m": _num(last("length")),
            "width_m": _num(last("width")),
            "draught_m": _num(last("draught")),
            "destination": destinations[-1] if destinations else None,
            "eta": last("eta"),
        },
        "all_reported_values": {
            "names": names, "callsigns": callsigns, "imos": imos,
            "destinations": destinations, "ship_types": ship_types,
        },
        "mmsi_decode": mid,
        "imo_check": imo_check,
        "identity_churn": churn,
        "integrity_notes": integrity,
        "pivots": pivots(mmsi, imo=imo, name=name,
                         callsign=callsigns[0] if callsigns else None,
                         lat=lat, lon=lon, ts=ts),
        "disclaimer": (
            "Every field above except the MID decode and IMO check digit is self-reported by "
            "the vessel over AIS and is trivially falsifiable. Treat it as a claim to be "
            "verified, not as identification."
        ),
    }


def _num(v):
    try:
        f = float(v)
        return None if f != f or f == 0 else round(f, 2)
    except (TypeError, ValueError):
        return None
