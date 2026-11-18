"""BalkanBet (`sports-sm-distribution-api.de-2.nsoftcdn.com`) bookie adapter."""
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

BASE_URL = "https://sports-sm-distribution-api.de-2.nsoftcdn.com/api/v1"
COMPANY_UUID = "4f54c6aa-82a9-475d-bf0e-dc02ded89225"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
SPORT_IDS = {
    "football": (18,),
    "basketball": (36,),
    "tennis": (78,),
    "hockey": (21,),
    "mma": (245,),
    "handball": (24, 30),  # 24 = handball, 30 = volleyball
}
SUB_SPORTS = {24: handball.HANDBALL, 30: handball.VOLLEYBALL}
DAYS_AHEAD = 10

_FB_TOTALS = {1.5: ("2", "01"), 2.5: ("3", "02"), 3.5: ("4", "03"), 4.5: ("5", "04"), 5.5: ("6", "05")}
_FB_HT_TOTALS = {0.5: ("1", "0"), 1.5: ("2", "01"), 2.5: ("3", "02")}
_FB_TEAM_TOTALS = {0.5: ("1", "0"), 1.5: ("2", "01"), 2.5: ("3", "02")}
_FB_HT_TEAM_TOTALS = {0.5: ("1", "0"), 1.5: ("2", "01")}


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


def _is_active(flag):
    return flag not in (0, False)


def _index_markets(markets):
    by_id = {}
    for m in markets or ():
        if not _is_active(m.get("active")):
            continue
        by_id.setdefault(m.get("marketId"), []).append(m)
    return by_id


def _outcomes(market):
    out = {}
    for o in market.get("outcomes") or ():
        if not _is_active(o.get("active")):
            continue
        try:
            odd = float(o.get("odd"))
        except (TypeError, ValueError):
            continue
        if odd > 1.0:
            sc = str(o.get("shortcut") or "").strip()
            if sc:
                out[sc] = odd
    return out


def _pick(by_id, market_id, key_map):
    for m in by_id.get(market_id) or ():
        oc = _outcomes(m)
        res = {side: oc.get(sc) for side, sc in key_map.items()}
        if all(res.values()):
            return res
    return None


def _pick_ranges(by_id, market_id, ranges, allowed):
    lines = {}
    for m in by_id.get(market_id) or ():
        oc = _outcomes(m)
        for line, (over_sc, under_sc) in ranges.items():
            if not allowed(line) or line in lines:
                continue
            over, under = oc.get(over_sc), oc.get(under_sc)
            if over and under:
                lines[line] = {"over": over, "under": under}
    return lines


def _lines(by_id, market_ids, key_map, allowed):
    out = {}
    main_line = None
    for mid in market_ids:
        for m in by_id.get(mid) or ():
            sv = m.get("specialValues") or ()
            if not sv:
                continue
            try:
                line = float(sv[0])
            except (TypeError, ValueError):
                continue
            if not allowed(line):
                continue
            oc = _outcomes(m)
            res = {side: oc.get(sc) for side, sc in key_map.items()}
            if all(res.values()) and line not in out:
                out[line] = res
                if m.get("favourite") and main_line is None:
                    main_line = line
    return out, main_line


def _allowed_tot(line):
    return pw.allowed_line(line, "totals")


def _allowed_sp(line):
    return pw.allowed_line(line, "spreads")


def _euro_spreads(by_id, market_id, spreads, derived):
    sides = {}
    for m in by_id.get(market_id) or ():
        sv = m.get("specialValues") or ()
        if not sv or ":" not in str(sv[0]):
            continue
        hg_s, _, ag_s = str(sv[0]).partition(":")
        try:
            h = int(hg_s) - int(ag_s)
        except ValueError:
            continue
        if h == 0:
            continue
        oc = _outcomes(m)
        if oc.get("1") and _allowed_sp(h - 0.5):
            sides.setdefault(h - 0.5, {}).setdefault("home", oc["1"])
        if oc.get("2") and _allowed_sp(h + 0.5):
            sides.setdefault(h + 0.5, {}).setdefault("away", oc["2"])
    for line, pair in sides.items():
        if line in spreads:
            continue
        spreads[line] = pair
        derived.add(line)


@bookie_adapter.register_adapter
class BalkanBetAdapter(bookie_adapter.BookieAdapter):
    """Adapter for BalkanBet Serbia (`balkanbet.rs`)."""

    name = "balkanbet"
    aliases = ("balkan", "bbet")
    label = "BalkanBet"
    short = "BKB"
    supports_match = True

    def __init__(self, timeout=25):
        self.timeout = timeout
        self.s = requests.Session()
        self.s.headers.update(HEADERS)

    def _get(self, path, allow_404=False, **params):
        query = {"companyUuid": COMPANY_UUID, "language": "sr", "timezone": "Europe/Belgrade", **params}
        try:
            r = self.s.get(f"{BASE_URL}{path}", params=query, timeout=self.timeout)
        except requests.RequestException as exc:
            raise RuntimeError(f"BalkanBet nije dostupan: {exc}") from exc
        if allow_404 and r.status_code == 404:
            raise LookupError("BalkanBet nema ovaj meč.")
        if r.status_code != 200:
            raise RuntimeError(f"BalkanBet HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise RuntimeError("BalkanBet je vratio neispravan JSON.") from exc

    def _row(self, ev, sub=None):
        name = ev.get("name") or ""
        if " - " not in name or not ev.get("markets"):
            return None
        home, _, away = name.partition(" - ")
        return {
            "id": ev["id"],
            "matchCode": ev.get("displayId"),
            "home": home.strip(),
            "away": away.strip(),
            "kickOffTime": _parse_dt_ms(ev.get("startsAt")),
            "live": False,
            "leagueId": ev.get("tournamentId"),
            "sport": sub,
            "_markets": ev["markets"],
        }

    def offer(self, sport):
        sport_ids = SPORT_IDS.get(sport)
        if not sport_ids:
            raise ValueError(f"BalkanBet ne podržava sport: {sport!r}")
        now = datetime.now(timezone.utc)
        from_ts = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        to_ts = (now + timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%dT%H:%M:%S")
        rows = []
        for sid in sport_ids:
            sub = SUB_SPORTS.get(sid)
            data = self._get("/events", **{"filter[from]": from_ts, "filter[to]": to_ts, "filter[sportId]": sid})
            events = (data.get("data") or {}).get("events") or [] if isinstance(data, dict) else []
            for ev in events:
                row = self._row(ev, sub=sub)
                if row is not None:
                    rows.append(row)
        return rows

    def match(self, sport, match_id):
        if sport not in SPORT_IDS:
            raise ValueError(f"BalkanBet ne podržava sport: {sport!r}")
        data = self._get(f"/events/{match_id}", allow_404=True)
        ev = data.get("data") if isinstance(data, dict) else None
        sub = SUB_SPORTS.get(ev.get("sportId")) if isinstance(ev, dict) and sport == "handball" else None
        row = self._row(ev, sub=sub) if isinstance(ev, dict) else None
        if row is None:
            raise LookupError("BalkanBet nema kvote za ovaj meč.")
        return row

    def decode_markets(self, sport, row, period=None):
        by_id = _index_markets(row.get("_markets"))
        ou_map = {"over": "2", "under": "1"}
        sp_map = {"home": "1", "away": "2"}
        ml3_map = {"home": "1", "draw": "0", "away": "2"}

        if sport == "football":
            ml = _pick(by_id, 6, ml3_map)
            dc = _pick(by_id, 368, {"1x": "10", "x2": "02"})
            spreads, derived = {}, set()
            dnb = _pick(by_id, 389, sp_map)
            if dnb:
                spreads[0.0] = dnb
            if ml and dc:
                spreads[-0.5] = {"home": ml["home"], "away": dc["x2"]}
                spreads[0.5] = {"home": dc["1x"], "away": ml["away"]}
                derived.update((-0.5, 0.5))
            _euro_spreads(by_id, 728, spreads, derived)

            ht_ml = _pick(by_id, 371, ml3_map)
            ht_dc = _pick(by_id, 380, {"1x": "10", "x2": "02"})
            ht_spreads, ht_derived = {}, set()
            if ht_ml and ht_dc:
                ht_spreads[-0.5] = {"home": ht_ml["home"], "away": ht_dc["x2"]}
                ht_spreads[0.5] = {"home": ht_dc["1x"], "away": ht_ml["away"]}
                ht_derived.update((-0.5, 0.5))

            decoded = {
                0: {
                    "ml": ml,
                    "totals": _pick_ranges(by_id, 443, _FB_TOTALS, _allowed_tot),
                    "spreads": spreads,
                    "derived": derived,
                    "teamTotals": {
                        "home": _pick_ranges(by_id, 419, _FB_TEAM_TOTALS, _allowed_tot),
                        "away": _pick_ranges(by_id, 422, _FB_TEAM_TOTALS, _allowed_tot),
                    },
                },
                1: {
                    "ml": ht_ml,
                    "totals": _pick_ranges(by_id, 428, _FB_HT_TOTALS, _allowed_tot),
                    "spreads": ht_spreads,
                    "derived": ht_derived,
                    "teamTotals": {
                        "home": _pick_ranges(by_id, 407, _FB_HT_TEAM_TOTALS, _allowed_tot),
                        "away": _pick_ranges(by_id, 413, _FB_HT_TEAM_TOTALS, _allowed_tot),
                    },
                },
            }
            return decoded if period is None else decoded[period]

        p = period or "ft"
        if sport == "basketball":
            cfg = {
                "ft": {"ml": 60, "spreads": (524,), "totals": (530,), "home": (539,), "away": (542,)},
                "ht": {"ml": None, "spreads": (512,), "totals": (515,), "home": (3480,), "away": (3483,)},
                "q1": {"ml": None, "spreads": (1718,), "totals": (1694,), "home": (4896,), "away": (4899,)},
            }[p]

            def _bb(_r, _per):
                sp, sp_m = _lines(by_id, cfg["spreads"], sp_map, _allowed_sp)
                tot, tot_m = _lines(by_id, cfg["totals"], ou_map, _allowed_tot)
                hm, hm_m = _lines(by_id, cfg["home"], ou_map, _allowed_tot)
                aw, aw_m = _lines(by_id, cfg["away"], ou_map, _allowed_tot)
                return {
                    "ml": _pick(by_id, cfg["ml"], sp_map) if cfg["ml"] else None,
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
                    out["ml"] = _pick(by_id, 1955, sp_map)
                    out["setspreads"] = _lines(by_id, (1958,), sp_map, _allowed_tot)[0]
                    out["spreads"] = _lines(by_id, (342,), sp_map, _allowed_tot)[0]
                    out["totals"] = _lines(by_id, (348,), ou_map, _allowed_tot)[0]
                    out["home"] = _lines(by_id, (351,), ou_map, _allowed_tot)[0]
                    out["away"] = _lines(by_id, (354,), ou_map, _allowed_tot)[0]
                    set_tot = _pick(by_id, 2030, {"over": "3", "under": "2"})
                    if set_tot:
                        out["settotals"] = {2.5: set_tot}
                elif per == "s1":
                    out["ml"] = _pick(by_id, 2012, sp_map)
                return out

            return bb.reversible(_tn)(row, p)

        if sport == "hockey":
            cfg = {
                "ft": {"ml": 989, "x12": 141, "spreads": (617,), "totals": (968,), "home": (620,), "away": (623,)},
                "p1": {"ml": None, "x12": 1745, "spreads": (2755,), "totals": (2746,), "home": (), "away": ()},
                "p2": {"ml": None, "x12": 1748, "spreads": (2758,), "totals": (2749,), "home": (), "away": ()},
                "p3": {"ml": None, "x12": 1751, "spreads": (2761,), "totals": (2752,), "home": (), "away": ()},
            }[p]

            def _hk(_r, _per):
                return {
                    "ml": _pick(by_id, cfg["ml"], sp_map) if cfg["ml"] else None,
                    "x12": _pick(by_id, cfg["x12"], ml3_map) if cfg["x12"] else None,
                    "spreads": _lines(by_id, cfg["spreads"], sp_map, _allowed_tot)[0],
                    "totals": _lines(by_id, cfg["totals"], ou_map, _allowed_tot)[0],
                    "home": _lines(by_id, cfg["home"], ou_map, _allowed_tot)[0],
                    "away": _lines(by_id, cfg["away"], ou_map, _allowed_tot)[0],
                }

            return bb.reversible(_hk)(row, p)

        if sport == "mma":
            home_key, away_key = ("2", "1") if row.get(mma.SWAPPED) else ("1", "2")
            ml = None
            for mid in (1955, 84, 60):
                ml = _pick(by_id, mid, {"home": home_key, "away": away_key})
                if ml:
                    break
            return {"ml": ml, "totals": {}}

        if sport == "handball":
            out = dict(handball.EMPTY)
            if row.get("sport") == handball.VOLLEYBALL:
                out["ml"] = _pick(by_id, 84, sp_map)
                return out
            if p == "ft":
                out["x12"] = _pick(by_id, 93, ml3_map)
                out["spreads"] = _lines(by_id, (956,), sp_map, _allowed_sp)[0]
                out["totals"] = _lines(by_id, (959,), ou_map, _allowed_tot)[0]
            elif p == "h1":
                out["spreads"] = _lines(by_id, (2382,), sp_map, _allowed_sp)[0]
                out["totals"] = _lines(by_id, (1004,), ou_map, _allowed_tot)[0]
            return out

        raise ValueError(f"BalkanBet ne podržava sport: {sport!r}")
