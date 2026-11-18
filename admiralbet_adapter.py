"""AdmiralBet (`srboffer.admiralbet.rs`) bookie adapter."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

import basketball as bb
import bookie_adapter
import handball
import hockey
import mma
import pinnwire_client as pw
import tennis

OFFER_URL = "https://srboffer.admiralbet.rs/api/offer/getwebevents"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Language": "sr-Latn",
    "OfficeId": "138",
}
SPORT_IDS = {
    "football": (1,),
    "basketball": (2,),
    "tennis": (3,),
    "hockey": (4,),
    "mma": (36,),
    "handball": (6, 20),  # 6 = handball, 20 = volleyball
}
SUB_SPORTS = {6: handball.HANDBALL, 20: handball.VOLLEYBALL}
DAYS_AHEAD = 10


def _parse_dt_ms(value):
    if not value:
        return 0
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _index_bets(bets):
    by_type = {}
    for b in bets or ():
        if b.get("isPlayable") is False or b.get("isInOffer") is False:
            continue
        by_type.setdefault(b.get("betTypeId"), []).append(b)
    return by_type


def _outcomes(bet):
    out = {}
    for o in bet.get("betOutcomes") or ():
        if o.get("isPlayable") is False or o.get("isInOffer") is False:
            continue
        try:
            odd = float(o.get("odd"))
        except (TypeError, ValueError):
            continue
        if odd > 1.0:
            name = (o.get("name") or "").strip().lower()
            out[name] = odd
    return out


def _pick(by_type, type_id, key_map):
    for bet in by_type.get(type_id) or ():
        oc = _outcomes(bet)
        res = {side: oc.get(name) for side, name in key_map.items()}
        if all(res.values()):
            return res
    return None


def _lines(by_type, type_ids, key_map, allowed):
    out = {}
    main_line = None
    for tid in type_ids:
        for bet in by_type.get(tid) or ():
            try:
                line = float(bet.get("sbv"))
            except (TypeError, ValueError):
                continue
            if not allowed(line):
                continue
            oc = _outcomes(bet)
            res = {side: oc.get(name) for side, name in key_map.items()}
            if all(res.values()) and line not in out:
                out[line] = res
                if bet.get("isMostBalanced") and main_line is None:
                    main_line = line
    return out, main_line


def _allowed_tot(line):
    return pw.allowed_line(line, "totals")


def _allowed_sp(line):
    return pw.allowed_line(line, "spreads")


@bookie_adapter.register_adapter
class AdmiralBetAdapter(bookie_adapter.BookieAdapter):
    """Adapter for AdmiralBet Serbia (`srboffer.admiralbet.rs`)."""

    name = "admiralbet"
    aliases = ("admiral", "ab")
    label = "AdmiralBet"
    short = "AB"
    supports_match = False

    def __init__(self, timeout=25):
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update(HEADERS)

    def _fetch_sport(self, sport_id):
        now = datetime.now(timezone.utc)
        params = [
            ("sportId", str(sport_id)),
            ("isLive", "false"),
            ("dateFrom", (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000")),
            ("dateTo", (now + timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%dT%H:%M:%S.000")),
            *[("eventMappingTypes", str(t)) for t in (1, 2, 3, 4, 5)],
        ]
        try:
            r = self.s.get(OFFER_URL, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise RuntimeError(f"AdmiralBet nije dostupan: {exc}") from exc
        if r.status_code != 200:
            raise RuntimeError(f"AdmiralBet HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError as exc:
            raise RuntimeError("AdmiralBet je vratio neispravan JSON.") from exc
        return data.get("events") or [] if isinstance(data, dict) else []

    def offer(self, sport):
        sport_ids = SPORT_IDS.get(sport)
        if not sport_ids:
            raise ValueError(f"AdmiralBet ne podržava sport: {sport!r}")
        rows = []
        for sid in sport_ids:
            sub = SUB_SPORTS.get(sid)
            for ev in self._fetch_sport(sid):
                name = ev.get("name") or ""
                if " - " not in name or not ev.get("bets"):
                    continue
                home, _, away = name.partition(" - ")
                rows.append({
                    "id": ev["id"],
                    "matchCode": ev.get("code"),
                    "home": home.strip(),
                    "away": away.strip(),
                    "kickOffTime": _parse_dt_ms(ev.get("dateTime")),
                    "live": bool(ev.get("isLive")),
                    "leagueId": ev.get("competitionId"),
                    "leagueName": ev.get("competitionName"),
                    "sport": sub,
                    "_bets": ev["bets"],
                })
        return rows

    def decode_markets(self, sport, row, period=None):
        by_type = _index_bets(row.get("_bets"))
        ou_map = {"over": "vise", "under": "manje"}
        sp_map = {"home": "1", "away": "2"}
        ml3_map = {"home": "1", "draw": "x", "away": "2"}

        if sport == "football":
            ml = _pick(by_type, 135, ml3_map)
            dc = _pick(by_type, 152, {"1x": "1x", "x2": "x2"})
            spreads, derived = {}, set()
            dnb = _pick(by_type, 154, sp_map)
            if dnb:
                spreads[0.0] = dnb
            if ml and dc:
                spreads[-0.5] = {"home": ml["home"], "away": dc["x2"]}
                spreads[0.5] = {"home": dc["1x"], "away": ml["away"]}
                derived.update((-0.5, 0.5))
            decoded = {
                0: {
                    "ml": ml,
                    "totals": _lines(by_type, (137,), ou_map, _allowed_tot)[0],
                    "spreads": spreads,
                    "derived": derived,
                    "teamTotals": {
                        "home": _lines(by_type, (161,), ou_map, _allowed_tot)[0],
                        "away": _lines(by_type, (162,), ou_map, _allowed_tot)[0],
                    },
                },
                1: {
                    "ml": _pick(by_type, 148, ml3_map),
                    "totals": _lines(by_type, (143,), ou_map, _allowed_tot)[0],
                    "spreads": {},
                    "derived": set(),
                    "teamTotals": {"home": {}, "away": {}},
                },
            }
            return decoded if period is None else decoded[period]

        p = period or "ft"
        if sport == "basketball":
            cfg = {
                "ft": {"ml": 186, "spreads": (191,), "totals": (213,), "home": (728,), "away": (729,)},
                "ht": {"ml": None, "spreads": (196, 1310), "totals": (197,), "home": (), "away": ()},
                "q1": {"ml": None, "spreads": (193,), "totals": (195,), "home": (), "away": ()},
            }[p]

            def _bb(_r, _per):
                sp, sp_m = _lines(by_type, cfg["spreads"], sp_map, _allowed_sp)
                tot, tot_m = _lines(by_type, cfg["totals"], ou_map, _allowed_tot)
                hm, hm_m = _lines(by_type, cfg["home"], ou_map, _allowed_tot)
                aw, aw_m = _lines(by_type, cfg["away"], ou_map, _allowed_tot)
                return {
                    "ml": _pick(by_type, cfg["ml"], sp_map) if cfg["ml"] else None,
                    "spreads": sp,
                    "totals": tot,
                    "home": hm,
                    "away": aw,
                    "main": {"spreads": sp_m, "totals": tot_m, "home": hm_m, "away": aw_m},
                }

            return bb.reversible(_bb)(row, p)

        if sport == "tennis":
            def _tn(_r, per):
                out = bookie_adapter.empty_markets("tennis")
                if per == "ft":
                    out["ml"] = _pick(by_type, 1723, sp_map)
                    out["setspreads"] = _lines(by_type, (221,), sp_map, _allowed_tot)[0]
                    out["spreads"] = _lines(by_type, (220,), sp_map, _allowed_tot)[0]
                    out["totals"] = _lines(by_type, (187,), ou_map, _allowed_tot)[0]
                    out["home"] = _lines(by_type, (218,), ou_map, _allowed_tot)[0]
                    out["away"] = _lines(by_type, (219,), ou_map, _allowed_tot)[0]
                elif per == "s1":
                    out["ml"] = _pick(by_type, 214, sp_map)
                    out["totals"] = _lines(by_type, (215,), ou_map, _allowed_tot)[0]
                return out

            return bb.reversible(_tn)(row, p)

        if sport == "hockey":
            cfg = {
                "ft": {"ml": None, "x12": 135, "spreads": (788,), "totals": (137,), "home": (161,), "away": (162,)},
                "p1": {"ml": None, "x12": None, "spreads": (), "totals": (240,), "home": (), "away": ()},
                "p2": {"ml": None, "x12": None, "spreads": (), "totals": (241,), "home": (), "away": ()},
                "p3": {"ml": None, "x12": None, "spreads": (), "totals": (242,), "home": (), "away": ()},
            }[p]

            def _hk(_r, _per):
                return {
                    "ml": None,
                    "x12": _pick(by_type, cfg["x12"], ml3_map) if cfg["x12"] else None,
                    "spreads": _lines(by_type, cfg["spreads"], sp_map, _allowed_tot)[0],
                    "totals": _lines(by_type, cfg["totals"], ou_map, _allowed_tot)[0],
                    "home": _lines(by_type, cfg["home"], ou_map, _allowed_tot)[0],
                    "away": _lines(by_type, cfg["away"], ou_map, _allowed_tot)[0],
                }

            return bb.reversible(_hk)(row, p)

        if sport == "mma":
            home_key, away_key = ("2", "1") if row.get(mma.SWAPPED) else ("1", "2")
            return {
                "ml": _pick(by_type, 454, {"home": home_key, "away": away_key}),
                "totals": _lines(by_type, (1632,), ou_map, _allowed_tot)[0],
            }

        if sport == "handball":
            out = dict(handball.EMPTY)
            if row.get("sport") == handball.VOLLEYBALL:
                out["ml"] = _pick(by_type, 454, sp_map)
                return out
            if p == "ft":
                out["x12"] = _pick(by_type, 135, ml3_map)
                out["spreads"] = _lines(by_type, (788,), sp_map, _allowed_sp)[0]
                out["totals"] = _lines(by_type, (190,), ou_map, _allowed_tot)[0]
            elif p == "h1":
                out["spreads"] = _lines(by_type, (196,), sp_map, _allowed_sp)[0]
                out["totals"] = _lines(by_type, (197,), ou_map, _allowed_tot)[0]
            return out

        raise ValueError(f"AdmiralBet ne podržava sport: {sport!r}")

