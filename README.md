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
| Historical AIS (Danish waters) | DMA `web.ais.dk/aisdata` | Danish PSI act, free, back to 2006 |
| Cable display layer | TeleGeography v3 API | CC BY-NC-SA 3.0 |
| Cable charted layer | National hydrographic offices | varies |
| Derived events | Global Fishing Watch Events API | free key; loitering, encounters, AIS-off |
| Dark-vessel corroboration | Sentinel-1 SAR via Copernicus | free |
| Fault corroboration | IODA, Cloudflare Radar, RIPE Atlas, ENTSO-E | free |

Terrestrial AIS reaches roughly 40–70 nm from a receiver. **A gap in a coastal feed is not evidence a transponder was switched off** — the vessel may simply have sailed out of range. This is why the Baltic, North Sea, Irish Sea and Taiwan Strait are the useful theatres and the mid-ocean is not.

---

## Running it

```bash
git clone https://github.com/YOURNAME/grapnel && cd grapnel
pip install -r requirements.txt

python scripts/make_demo.py        # synthetic data, exercises every detector
python -m grapnel.pipeline -v      # real run against the live feed
python -m pytest tests/            # regression suite

cd docs && python -m http.server 8000
```

Then open `http://localhost:8000`. Same files that Pages serves.

The demo generates fabricated vessels on fabricated cables and stamps the payload so the UI shouts about it. Delete `docs/data/` before publishing anything real.

## Deploying to GitHub Pages

The site is plain static files — no build step, no framework, no server. `docs/` is the site root.

1. Push the repo to GitHub. It must be **public** for free Actions minutes on a schedule.
2. **Settings → Pages → Source: `GitHub Actions`.** Not "Deploy from a branch" — branch mode serves whatever is committed and ignores the artifact the workflow uploads, so your data would freeze at whatever you last pushed by hand.
3. **Actions → monitor → Run workflow** to seed it. The schedule alone can take up to an hour to fire the first time.
4. Live at `https://YOURNAME.github.io/grapnel/`.

Everything is relative-path, so the `/grapnel/` subpath works with no config.

`monitor.yml` runs every 30 minutes: restores the AIS archive from the Actions cache, polls the feed, detects, writes `docs/data/`, then uploads and deploys `docs/` as the Pages artifact. Each run writes a summary table of detections to the Actions run page, so you can triage from the Actions tab without opening the site.

**Why one workflow and not two.** The obvious design is a monitor job that commits data plus a Pages job triggered on push. It does not work: a push made with `GITHUB_TOKEN` does not trigger other workflows, because GitHub blocks that to prevent recursion. The site would deploy once, then go stale forever while every run still reported green. Deploying inline is the only arrangement that stays live.

**Commits.** The 30-minute runs commit nothing — publishing happens through the Pages artifact, so the repo does not accumulate 48 commits a day. A single daily run additionally writes `history/YYYY-MM-DD.json` and commits it. Every published payload is a public claim about real, named vessels, so there needs to be a record of what was asserted and when; one snapshot a day gives you that without drowning the history.

**The archive is the fragile part.** Digitraffic serves current positions only, so **the archive is exactly as dense as your polling interval** and anything not captured is gone permanently. It lives in the Actions cache, which GitHub evicts LRU past 10 GB and drops entirely after 7 days without access — fine while the schedule runs, fatal if you disable the workflow for a week. If you need durable history, back the archive with a `data` orphan branch or an S3/R2 bucket instead. Everything before you started polling has to come from DMA.

## Tuning

Thresholds live in `config/settings.yml` and are seeded from Global Fishing Watch's published parameters where they overlap, so the numbers are defensible against an existing baseline rather than invented.

Two design decisions worth understanding before you change them:

**Drag is scored over the whole contiguous slow run, not the corridor slice.** In-corridor distance is bounded by corridor width for a perpendicular crossing — 6 km for a 3 km corridor — no matter how far the anchor was actually dragged.

**The transit baseline is the vessel's own upper-quartile moving speed.** If a drag dominates the observation window then the median *is* the drag, and comparing the anomaly against itself gives a ratio of 1.0 and silently suppresses the detection. When no usable baseline exists — a hull slow throughout the window — the detector falls back to a course-hold test and is capped at LOW, because a small fishing vessel whose normal working speed is two knots must not be flagged.

## Contributing

Adding an incident: append to `config/incidents.json` with at least one source URL and open a PR. Claims sourced only to social media are rejected. Coordinates are approximate and must be marked as such.

Adding a detector: it must emit its thresholds and a `benign_explanations` list in `evidence`. If you cannot write down what innocent behaviour looks identical to yours, the detector is not ready.

## Licence

Code is MIT. **Data is not** — see the note in [LICENSE](LICENSE). The TeleGeography layer is CC BY-NC-SA 3.0, and redistributing anything derived from it inherits non-commercial and share-alike.
