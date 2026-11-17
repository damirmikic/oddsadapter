"""Bookie adapter interface, registry, and the default MerkurXtip adapter.

Pinnacle (via PinnWire) is always the reference book. Any target bookmaker can be plugged in by subclassing
`BookieAdapter`, registering it with `@register_adapter`, and selecting it via `--bookie <name>`, `XTIP_BOOKIE`,
or `"bookie"` in `data/pinnwire_config.json`.

See `bookie_example.py` for a minimal starter template.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import os
import time

import bookie_client as mx

SPORTS = ("football", "basketball", "tennis", "hockey", "mma", "handball")

_EMPTY_TEMPLATES = {
    "football": lambda: {
        0: {"ml": None, "totals": {}, "spreads": {}, "derived": set(), "teamTotals": {"home": {}, "away": {}}},
        1: {"ml": None, "totals": {}, "spreads": {}, "derived": set(), "teamTotals": {"home": {}, "away": {}}},
    },
    "basketball": lambda: {"ml": None, "spreads": {}, "totals": {}, "home": {}, "away": {}},
    "tennis": lambda: {
        "ml": None, "setspreads": {}, "settotals": {}, "spreads": {}, "totals": {},
        "home": {}, "away": {}, "setwin": {},
    },
    "hockey": lambda: {"ml": None, "x12": None, "spreads": {}, "totals": {}, "home": {}, "away": {}},
    "mma": lambda: {"ml": None, "totals": {}},
    "handball": lambda: {"ml": None, "x12": None, "spreads": {}, "totals": {}},
}


def empty_markets(sport):
    """Return an empty decoded-markets dict in the shape expected by `sport`."""
    factory = _EMPTY_TEMPLATES.get(sport)
    if factory is None:
        raise ValueError(f"Nepoznat sport za bookie adapter: {sport!r}")
    return factory()


class AdapterSportAPI:
    """Presents one sport of a `BookieAdapter` as `.offer()` / `.match(match_id)` for `OfferCache`."""

    def __init__(self, adapter, sport):
        self.adapter = adapter
        self.sport = sport

    def offer(self):
        return self.adapter.offer(self.sport)

    def match(self, match_id):
        return self.adapter.match(self.sport, match_id)


class BookieAdapter:
    """Base interface for a bookmaker compared against Pinnacle.

    Subclasses must implement `offer(sport)` and `decode_markets(sport, row, period=None)`.
    Each row returned by `offer(sport)` should be a dict with at least:
      - `id`: unique match identifier at this bookie
      - `home`, `away`: participant names (str)
      - `kickOffTime`: UTC kickoff timestamp in milliseconds (int)
      - `live`: bool (or override `is_prematch(row)`)
    Optional fields used by specific views:
      - `leagueId` / `leagueShort`: bookie competition id/code (used by `comp_id`)
      - `matchCode`: display code in football
      - `sport`: `"HB"` or `"V"` for the combined handball/volleyball board
    """

    name = "base"
    aliases = ()
    label = "Bookie"
    short = "BK"
    pair_by_comp = False
    selectable = True

    def meta(self):
        return {"name": self.name, "label": self.label, "short": self.short or self.label[:3].upper()}

    def offer(self, sport):
        """Return a list of normalized offer rows for `sport`."""
        raise NotImplementedError

    def match(self, sport, match_id):
        """Fetch fresh single-match data for `match_id`, or raise LookupError if unsupported."""
        raise LookupError(f"{self.label} nema pojedinačni meč.")

    def is_prematch(self, row):
        """True if `row` should still be paired as prematch."""
        return not row.get("live") or (row.get("kickOffTime") or 0) > time.time() * 1000

    def comp_id(self, sport, row):
        """Map a bookie offer row to a PinnWire `league_id` for `sport`, or None if untracked."""
        return None

    def decode_markets(self, sport, row, period=None):
        """Decode `row` into the market dict shape expected by `sport` (see `empty_markets(sport)`)."""
        raise NotImplementedError

    def sport_api(self, sport):
        return AdapterSportAPI(self, sport)


ADAPTERS = {}


def register_adapter(cls):
    """Register a `BookieAdapter` subclass by its `name` and `aliases`."""
    ADAPTERS[cls.name.lower()] = cls
    for alias in getattr(cls, "aliases", ()):
        ADAPTERS[alias.lower()] = cls
    return cls


def list_adapters():
    """Canonical names of all registered bookie adapters."""
    seen = {}
    for cls in ADAPTERS.values():
        seen[cls.name] = cls
    return sorted(seen)


def adapter_catalog():
    """Metadata list `[{name, label, short}]` of selectable adapters for the UI dropdown."""
    seen = {}
    for cls in ADAPTERS.values():
        if getattr(cls, "selectable", True) and cls.name not in seen:
            seen[cls.name] = {"name": cls.name, "label": cls.label, "short": cls.short or cls.label[:3].upper()}
    return list(seen.values())


def load_external_adapters(modules=None, config_loader=None):
    """Load external bookie adapters from modules, `ODDS_BOOKIE_MODULES`, config, and `odds.bookies` entry points."""
    names = list(modules or ())
    env_mods = os.environ.get("ODDS_BOOKIE_MODULES", "")
    if env_mods:
        names.extend(m.strip() for m in env_mods.split(",") if m.strip())
    if config_loader is not None:
        cfg_mods = (config_loader() or {}).get("bookie_modules") or ()
        names.extend(cfg_mods)
    for mod_name in names:
        importlib.import_module(mod_name)
    for ep in importlib.metadata.entry_points(group="odds.bookies"):
        loaded = ep.load()
        if isinstance(loaded, type) and issubclass(loaded, BookieAdapter):
            register_adapter(loaded)


def get_adapter(name=None, config_loader=None, modules=None):
    """Instantiate the active `BookieAdapter` (explicit name -> `ODDS_BOOKIE` -> config -> maxbet)."""
    load_external_adapters(modules=modules, config_loader=config_loader)
    chosen = name or os.environ.get("ODDS_BOOKIE")
    if not chosen and config_loader is not None:
        chosen = (config_loader() or {}).get("bookie")
    key = (chosen or "maxbet").strip().lower()
    cls = ADAPTERS.get(key)
    if cls is None:
        available = ", ".join(list_adapters())
        raise ValueError(f"Nepoznat bookie adapter: {chosen!r}. Dostupni: {available}")
    return cls()


class FeedBookieAdapter(BookieAdapter):
    """Base adapter for NSoft-style JSON feeds (`bookie_client.py` and per-sport code tables)."""

    name = "feed"
    label = "Bookie"
    short = "BK"
    base = mx.BASE

    _OFFER_PATHS = {
        "football": mx.OFFER_PATH,
        "basketball": "/restapi/custom/offer/sr/sport/B/mob",
        "tennis": "/restapi/custom/offer/sr/sport/T/mob",
        "hockey": "/restapi/custom/offer/sr/sport/H/mob",
        "mma": "/restapi/custom/offer/sr/sport/MM/mob",
    }

    def __init__(self):
        self._apis = {}

    def _api(self, sport):
        if sport not in self._apis:
            if sport == "handball":
                import handball

                self._apis[sport] = handball.JoinedAPI(base=self.base, label=self.label)
            else:
                path = self._OFFER_PATHS.get(sport)
                if path is None:
                    raise ValueError(f"{self.label} ne podržava sport: {sport!r}")
                self._apis[sport] = mx.BookieAPI(offer_path=path, base=self.base, label=self.label)
        return self._apis[sport]

    def offer(self, sport):
        return self._api(sport).offer()

    def match(self, sport, match_id):
        return self._api(sport).match(match_id)

    def is_prematch(self, row):
        return mx.is_prematch(row)

    def comp_id(self, sport, row):
        if sport == "football":
            import pinnwire_dashboard as dash

            return dash.merkur_comp(row)
        if sport == "basketball":
            import basketball as bb

            return bb.MX_COMP.get(row.get("leagueId"))
        if sport == "hockey":
            import hockey

            return hockey.MX_COMP.get(row.get("leagueId"))
        return None

    def decode_markets(self, sport, row, period=None):
        if sport == "football":
            decoded = mx.markets(row)
            return decoded if period is None else decoded[period]
        if sport == "basketball":
            import basketball as bb

            return bb.merkur_markets(row, period or "ft")
        if sport == "tennis":
            import tennis

            return tennis.merkur_markets(row, period or "ft")
        if sport == "hockey":
            import hockey

            return hockey.merkur_markets(row, period or "ft")
        if sport == "mma":
            import mma

            return mma.merkur_markets(row)
        if sport == "handball":
            import handball

            return handball.merkur_markets(row, period or "ft")
        raise ValueError(f"{self.label} ne podržava sport: {sport!r}")


@register_adapter
class MaxBetAdapter(FeedBookieAdapter):
    """Default MaxBet adapter (`maxbet.rs`)."""

    name = "maxbet"
    aliases = ("mb",)
    label = "MaxBet"
    short = "MB"
    base = "https://www.maxbet.rs"


def register_feed_bookie(name, label, short, base, aliases=()):
    """Register a bookie using the shared JSON feed format."""
    cls = type(
        f"{name.title()}FeedAdapter",
        (FeedBookieAdapter,),
        {"name": name, "label": label, "short": short, "base": base, "aliases": tuple(aliases)},
    )
    return register_adapter(cls)


for _name, _label, _short, _base, _aliases in (
    ("merkurxtip", "MerkurXtip", "MX", "https://www.merkurxtip.rs", ("mx",)),
    ("oktagon", "OktagonBet", "OK", "https://www.oktagonbet.com", ("oktagonbet",)),
    ("betole", "BetOle", "BO", "https://www.betole.com", ()),
    ("brazilbet", "BrazilBet", "BB", "https://www.brazilbet.rs", ()),
    ("soccerbet", "SoccerBet", "SB", "https://www.soccerbet.rs", ("soccer",)),
):
    register_feed_bookie(_name, _label, _short, _base, _aliases)

