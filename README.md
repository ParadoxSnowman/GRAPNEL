# GRAPNEL

Open-source monitoring for anomalous vessel behaviour over submarine cable corridors. Ingests cable geometry and AIS, flags behaviour that matches documented cable-damage patterns, and hands you a complete vessel dossier so you can do your own OSINT on the hull.

A grapnel is the tool used to hook a cable off the seabed — for repair, or otherwise.

---

## Read this before anything else

**This tool cannot detect cable tapping.** Tapping is conducted from submarines and ROVs that do not transmit AIS. A mothership loitering overhead is indistinguishable from a research vessel doing legitimate survey work, and the actual intercept happens hundreds of metres below the surface where no civilian sensor reaches. Anyone who tells you an AIS map detects taps is selling something.

What GRAPNEL observes is the threat that has actually been materialising in the open record: **anchor-drag damage, pre-incident loitering and survey behaviour, and transponder gaps over cable corridors.** That is a narrower claim and a defensible one.

**GRAPNEL produces observations, not accusations.** Every detection carries the track that produced it, the thresholds it crossed, and why it scored the way it did. It never asserts intent or attribution. This is a legal position as much as an epistemic one: "vessel X sabotaged cable Y" is a defamation exposure, while "vessel X held 2.1 kn on a constant course across corridor Y during the four-hour window containing an observed fault, track attached" is a finding.

**The false-positive rate is the hard problem, not the detection rate.** In the Baltic in winter, dozens of hulls a week satisfy "slow over a cable". Legitimate anchoring, fishing, ice holds and machinery breakdowns all produce the same surface signature. The [incident library](config/incidents.json) deliberately over-represents cases that were investigated and closed as accidents — the *Vezhen*, the vessel boarded at Liepaja — because a tool that only shows you confirmed sabotage teaches you to see sabotage everywhere.

---

## What it does

| Detector | Fires on | Reference case |
|---|---|---|
| `anchor_drag` | Sustained sub-transit speed on a near-constant course crossing a corridor | *Fitburg*, *Eagle S*, *Yi Peng 3* |
| `loiter` | Near-stationary presence over a cable, sustained for hours | *Hong Tai 58* |
| `survey_pattern` | Reciprocal-leg lawnmower track near a corridor | *Yantar* |
| `corridor_gap` | AIS silence beginning one side of a corridor, ending the other | *Shunxing-39* |
| `position_jump` | Consecutive positions no hull could travel between | GNSS spoofing / dual transponders |

Detections are then **corroborated** against independently observed faults — operator notices, ENTSO-E interconnector capacity drops, IODA and RIPE Atlas connectivity signals. This join is the point of the project. A slow corridor transit alone is background noise; the same transit inside the window of a fault observed by an instrument that has no idea any vessel was present is a finding.

## Why a fresh deployment shows no detections (and what to do)

A live AIS feed returns **one fix per vessel per poll**. Every detector needs a track, so a single poll can never produce a detection no matter how much traffic is out there. At the default 30-minute cadence a vessel has a usable track after roughly three hours in the area. This is correct behaviour and it looks exactly like a broken tool, so the map does not rely on detections to have something to show:

- **Live vessel layer.** Every hull currently observed, dimmed grey, magenta if it is inside a corridor, orange if it is inside a corridor and under 2 knots. Populated on the first run.
- **In-corridor watchlist.** Which hulls are over a cable *right now*, slowest first, each with a full dossier. Needs no history at all, and it is the same question a duty watch officer would ask.
- **Precedent library.** Eleven sourced cases, available before any data exists.

For real detections immediately, backfill from the Danish archive instead of waiting:

```bash
python scripts/bootstrap.py --days 2
```

DMA publish complete daily archives back to 2006, free and keyless. One day gives dense multi-hour tracks for thousands of hulls — enough to exercise every detector on real vessels. Mind the coverage mismatch: **DMA covers Danish waters and the western Baltic, not the Gulf of Finland**, so bootstrap with the southern Baltic box (the default in that script — Bornholm through Öland and Gotland, where C-Lion1 and the Sweden–Lithuania interconnects run) and switch back to the Gulf of Finland for live watching.

Presence in a corridor is **not** behaviour and implies nothing. Cable routes run through shipping lanes, anchorages and fishing grounds, so on a busy day the watchlist is mostly ordinary traffic. The payload carries that warning in its own JSON so it travels with the data.

## The vessel dossier

Clicking a detection opens everything the hull broadcast about itself, unedited and copyable, plus every pivot needed to test those claims against sources that are not AIS: MarineTraffic, Equasis, IMO GISIS, ITU MARS, Paris MoU, OpenSanctions, OFAC, Copernicus Browser for Sentinel-1 SAR, IODA.

Three things are computed locally because they are cheap, deterministic and immediately decisive:

- **MID decode.** The first three MMSI digits encode the issuing administration. A hull broadcasting a Cameroon MID while claiming a Panama flag has a discrepancy worth explaining.
- **IMO check digit.** IMO numbers carry a modulo-10 checksum. A number that fails it was never issued.
- **Identity churn.** If one MMSI reports more than one name, callsign or IMO inside the observation window, that is recorded verbatim. It is one of very few AIS-internal signals that receiver coverage cannot explain away.

GRAPNEL deliberately **does not scrape** any of the external sources. You open the door yourself. That keeps the project clear of every tracking provider's terms of service and keeps the provenance of any follow-on finding attributable to you rather than to a bot.

---

## The trap that kills most versions of this

There are two classes of cable geometry and conflating them produces nothing but noise.

**Display geometry** (TeleGeography) is hand-drawn in Adobe Illustrator for cartographic clarity. It is not survey data; error against the laid route is routinely 10–50 km. Licensed CC BY-NC-SA 3.0 — non-commercial and share-alike, which will infect anything you derive from it.

**Charted geometry** (S-57/S-101 ENC, object classes `CBLSUB` and `CBLARE`) is issued by national hydrographic offices with a stated positional accuracy. It is what a mariner is legally navigating against.

GRAPNEL runs on display geometry alone, but **caps every such detection at LOW confidence in code**, regardless of how clean the behavioural signal looks. To lift that ceiling, export charted geometry and point `config/settings.yml` at it:

```bash
ogr2ogr -f GeoJSON data/enc/fi_cblsub.json /path/to/ENC_ROOT/FI5xxxxx.000 CBLSUB
```

Free ENC sources: NOAA (US), Traficom (FI), DMA (DK), Sjöfartsverket (SE), Transpordiamet (EE). UKHO ADMIRALTY is paid; Kingfisher publishes UK cable awareness charts for the fishing industry.

## Data sources

| Layer | Source | Terms |
|---|---|---|
| Live AIS (Finnish waters) | Digitraffic `meri.digitraffic.fi` | CC BY 4.0, no key |
| Bootstrap history | DMA daily archives | free, keyless, back to 2006 |
| Historical AIS (Danish waters) | DMA `web.ais.dk/aisdata` | Danish PSI act, free, back to 2006 |
| Cable display layer | TeleGeography v3 API | CC BY-NC-SA 3.0 |
| Cable charted layer | National hydrographic offices | varies |
| Derived events | Global Fishing Watch Events API | free key; loitering, encounters, AIS-off |
| Dark-vessel corroboration | Sentinel-1 SAR via Copernicus | free |
| Fault corroboration | IODA, Cloudflare Radar, RIPE Atlas, ENTSO-E | free |

Terrestrial AIS reaches roughly 40–70 nm from a receiver. **A gap in a coastal feed is not evidence a transponder was switched off** — the vessel may simply have sailed out of range. This is why the Baltic, North Sea, Irish Sea and Taiwan Strait are the useful theatres and the mid-ocean is not.

---
