# TM & S — private streaming catalogue

A polished Flask streaming/catalogue site for phone and desktop.

## What is included

- Responsive home, browse, movie/show details and video player UI
- Local and authorized remote playback
- Local downloads
- Private member join tracking with phone number
- Optional browser GPS capture with explicit location permission
- Admin member table with phone, coordinates, accuracy, joined time and last seen
- Google Maps link for saved coordinates
- CSV export of member records
- Admin-managed API connector with multiple saved configurations
- **Smart API setup:** you manually provide the API name, base URL and authentication when needed; TM & S supplies common REST endpoint and JSON mapping defaults when advanced fields are left blank
- Advanced API override fields for unusual providers
- Activate/deactivate API configurations from Admin
- Main TM & S search uses the currently active API automatically when a query is entered
- API result cards preserve the provider's returned title, media type, poster, year and ID
- API playback retrieves a fresh stream response at playback time from the configured stream endpoint
- System error center covering application exceptions, HTTP errors, API search failures and API playback failures
- Request IDs on recorded faults to help diagnose deployment problems

## API setup

The API section is **not pre-connected**. Nothing is contacted until you enter and save your own configuration.

### You normally provide

- API name
- Base URL
- Headers/Auth JSON only when the provider requires it

### TM & S can supply when you leave Advanced blank

- Search: `/search`
- Info: `/info/{id}`
- Seasons: `/seasons/{id}`
- Episodes: `/episodes/{id}`
- Stream: `/stream/{id}`
- Download: `/dl/{id}`
- Subtitles: `/subtitles/{id}`
- Search parameter: `query`
- Common response mappings for `results`, `id`, `title`, `poster`, `backdrop`, `year`, `type`, `description`, and `url`

Open **Advanced API mapping** when your provider uses different routes or JSON field names. Use `{id}` where the provider expects the selected external title ID.

After saving, tick **Save and connect this API now** (or activate a saved configuration later). The top search on TM & S then searches both your local catalogue and the active API. Clicking an API result opens the playback flow using the returned title and ID.

The connector is generic. It does not discover, scrape, or automatically add a third-party API for you.

## Admin dashboard

Open `/admin/login` and enter the value of `ADMIN_KEY`.

The dashboard contains:

- Members
- API & Integrations
- System errors
- Authorised media catalogue

## Render deployment

- Runtime: Python 3.12
- Build command: `pip install -r requirements.txt`
- Start command is already in `Procfile`
- Set `SECRET_KEY` to a long random value
- Set `ADMIN_KEY` to your private admin dashboard key
- For persistent SQLite data on Render, use a persistent disk and point `DATABASE_PATH` at that disk

## Member location

The Join page asks the member to grant browser location permission. When permission is granted, TM & S records latitude, longitude and the browser-reported GPS accuracy. If permission is denied, the member can still join and the dashboard shows location as not shared.

## Media/API use

Only use media, streams and APIs you are authorized to access, cache, download or distribute. The starter does not automatically add a third-party movie API.

For a larger production library, consider PostgreSQL plus persistent object storage/CDN for media.
