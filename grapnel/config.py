"""Runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .detect import Thresholds

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    name: str = "gulf-of-finland"
    #: (min_lon, min_lat, max_lon, max_lat)
    bbox: tuple = (18.0, 58.5, 30.5, 61.0)
    use_telegeography: bool = True
    charted_cable_files: list = field(default_factory=list)
    sources: list = field(default_factory=lambda: ["digitraffic"])
    thresholds: Thresholds = field(default_factory=Thresholds)
    cache_dir: Path = ROOT / ".cache"
    data_dir: Path = ROOT / "data"
    site_data_dir: Path = ROOT / "docs" / "data"
    incidents_file: Path = ROOT / "config" / "incidents.json"
    retain_days: int = 45
    max_detections_published: int = 500

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        path = Path(path or ROOT / "config" / "settings.yml")
        cfg = cls()
        if not path.exists():
            return cfg
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

        for k in ("name", "use_telegeography", "sources", "retain_days", "max_detections_published"):
            if k in raw:
                setattr(cfg, k, raw[k])
        if "bbox" in raw:
            cfg.bbox = tuple(float(x) for x in raw["bbox"])
        if "charted_cable_files" in raw:
            cfg.charted_cable_files = raw["charted_cable_files"] or []
        for k in ("cache_dir", "data_dir", "site_data_dir", "incidents_file"):
            if k in raw:
                setattr(cfg, k, (ROOT / raw[k]).resolve())
        if "thresholds" in raw and raw["thresholds"]:
            th = Thresholds()
            for k, v in raw["thresholds"].items():
                if hasattr(th, k):
                    setattr(th, k, float(v) if not isinstance(v, bool) else v)
            cfg.thresholds = th

        for d in (cfg.cache_dir, cfg.data_dir, cfg.site_data_dir):
            Path(d).mkdir(parents=True, exist_ok=True)
        return cfg
