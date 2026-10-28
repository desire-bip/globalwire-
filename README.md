# GlobalWire — auto-updating world news aggregator

A professional news site aggregating **36 public RSS/Atom feeds** (BBC, NYT, Guardian, NPR,
Sky News, Google News, TechCrunch, The Verge, Wired, Ars, NASA, ScienceDaily, CBS Sports,
Rolling Stone and more) across **9 categories**: top · world · business · technology ·
science · health · sports · entertainment · politics.

## Stack
- **Backend:** `server.py` — Python 3.10+ **standard library only** (no pip installs to rot or break).
  RSS 2.0 + Atom parsing, per-feed image extraction (media:thumbnail / enclosure / inline), HTML-stripped
  summaries, UTC-normalised timestamps, dedupe, trend-sorted merging, per-source health tracking.
- **Sources:** `sources.json` — edit freely; any public RSS/Atom feed works.
- **Frontend:** `static/index.html` — zero-CDN vanilla JS/CSS (works behind any proxy), hero lead story,
  card grid, category nav, live search, clock/countdown, manual refresh button, in-page auto-reload the
  moment a new hourly cycle lands.

## Auto-update (every 1 hour)
`REFRESH_SECONDS = 3600` in `server.py`: a background thread re-pulls all feeds hourly; the page polls
`/api/status` every 5 s and reloads stories as soon as a new cycle completes. `POST /api/refresh`
forces an immediate cycle. The header shows *updated … ago* and *next refresh in …*.

## Run
```bash
python3 server.py        # serves on 0.0.0.0:8000
# open http://localhost:8000
```

## API
| Endpoint | Returns |
|---|---|
| `GET /api/news?category=technology` | items `{title, url, summary, image, source, category, published_at, published_ts}` (newest first; `all` = interleaved) |
| `GET /api/status` | per-source ok/count/error, `updated_at`, `next_refresh_in` |
| `POST /api/refresh` | start a refresh cycle now |

## Notes
- RSS feeds are published for syndication; the site links back and attributes every story to its source.
  Personal/educational use; check publishers' terms before commercial deployment.
- Design choice per the adopted skills: REA (reverse-engineering plugin) explicitly does not apply to
  ordinary web builds — a dependency-free feed architecture is the correct tool here.
