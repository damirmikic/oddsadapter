// Bookie offer coverage view: compares which bookies carry each upcoming PinnWire match (presence only).
import { useCallback, useEffect, useMemo, useState } from "react";
import { requestJson } from "./api.js";
import { clockFmt, fmtKickoffShort } from "./format.js";
import { SPORTS } from "./menu.jsx";
import { useBookie } from "./ui.jsx";

export function CoveragePage({ initialSport = "football", onClose }) {
  const activeBookie = useBookie();
  const [sport, setSport] = useState(initialSport === "value" ? "football" : initialSport);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [missingOnly, setMissingOnly] = useState(false);

  const load = useCallback(async (refresh = false) => {
    setLoading(true);
    setError("");
    try {
      const res = await requestJson(`/api/coverage?sport=${sport}${refresh ? "&refresh=1" : ""}`);
      setData(res);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, [sport]);

  useEffect(() => {
    load(false);
  }, [load]);

  const q = query.trim().toLocaleLowerCase();
  const rows = useMemo(() => {
    const matches = data?.matches || [];
    return matches.filter((m) => {
      if (missingOnly && m.available.includes(activeBookie.name)) return false;
      if (!q) return true;
      return `${m.home} ${m.away} ${m.leagueName}`.toLocaleLowerCase().includes(q);
    });
  }, [data, missingOnly, activeBookie.name, q]);

  const bookies = data?.bookies || [];

  return (
    <div className="coverage-page">
      <div className="filters">
        <button type="button" className="button ghost" onClick={onClose}>← Nazad</button>
        <div className="segmented">
          {SPORTS.map((s) => (
            <button key={s.key} type="button" className={sport === s.key ? "active" : ""} onClick={() => setSport(s.key)}>
              {s.label}
            </button>
          ))}
        </div>
        <input
          className="search"
          type="search"
          placeholder="Pretraga meča ili lige…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <label className="auto" title={`Prikaži samo mečeve koje ${activeBookie.label} nema u ponudi`}>
          <input type="checkbox" checked={missingOnly} onChange={(e) => setMissingOnly(e.target.checked)} />
          Samo fale kod {activeBookie.short}
        </label>
        <button type="button" className="button primary" onClick={() => load(true)} disabled={loading}>
          {loading ? "Učitavam…" : "↻ Osveži ponude"}
        </button>
        {data?.fetchedAt && (
          <span className="muted">{clockFmt.format(new Date(data.fetchedAt))} · {rows.length} mečeva</span>
        )}
      </div>

      {error && <p className="state error" role="alert">{error}</p>}

      {!!bookies.length && (
        <div className="coverage-chips">
          {bookies.map((b) => (
            <span key={b.name} className={`coverage-chip${b.error ? " err" : ""}${b.name === activeBookie.name ? " active" : ""}`}
              title={b.error || `${b.label}: ${b.paired} upareno / ${b.offered} u ponudi`}>
              <b>{b.label}</b> {b.paired}/{b.offered}
              {b.error && <small> · greška</small>}
            </span>
          ))}
        </div>
      )}

      {!data && loading && <p className="state">Povlačim ponude svih kladionica…</p>}

      {data && !rows.length && <p className="state">Nema mečeva za izabrane filtere.</p>}

      {!!rows.length && (
        <div className="board-scroll mc-wrap">
          <table className="mc-board coverage-table">
            <thead>
              <tr>
                <th className="mc-match">Meč</th>
                {bookies.map((b) => (
                  <th key={b.name} className="cov-col" title={b.label}>{b.short}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((m) => {
                const avail = new Set(m.available);
                return (
                  <tr key={m.id} className="mc-row">
                    <td className="mc-match">
                      <div className="mc-when">{fmtKickoffShort(new Date(m.starts))}</div>
                      <div className="mc-teams"><b>{m.home}</b> <span>vs</span> <b>{m.away}</b></div>
                      <div className="mc-league">{m.leagueName}</div>
                    </td>
                    {bookies.map((b) => (
                      <td key={b.name} className="cov-cell">
                        {avail.has(b.name)
                          ? <span className="cov-yes" title={`${b.label} ima meč`}>✓</span>
                          : <span className="muted" title={`${b.label} nema meč`}>—</span>}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
