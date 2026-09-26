# TM & S

A responsive Flask streaming catalogue for phone and PC. The public site handles discovery and the private admin area handles your catalogue and manually configured playback integrations.

## Render

The project is ready to upload at the repository root. Render can run the included `Procfile` and install from `requirements.txt`.

Set this Render environment variable for the private admin doorway:

- `ADMIN_NAME` — the single admin name you type at `/promise21232425`.
- `SECRET_KEY` — optional but recommended for Flask sessions; this is an internal server setting, not a user login password.

## Admin

Open `/promise21232425`. Enter the exact value stored in `ADMIN_NAME`. There is no password or secret-key field in the UI. The public navigation does not expose the admin or API Library.

The private dashboard contains:

- member join records (phone plus browser-provided GPS coordinates when the member consents)
- CSV export
- API & Integrations
- API Library
- system errors with request IDs
- authorised local/remote media catalogue management

## Discovery and playback

TM & S uses a public movie/series metadata layer to put real title cards on the home page and to search titles. This metadata layer is separate from playback. When you save and activate a playback API in Admin, TM & S uses that connection for playback and attempts to match a discovered title to the provider's own ID by title.

The API form has a small set of fields for the information only you can know (base URL, API name, authentication/headers). Common endpoint paths and JSON mappings are filled automatically and can be overridden in Advanced.

## Important

Only connect playback sources and media you are authorised to access and distribute.
