# DroppedNeedle torrent plugin

Prowlarr or Torznab search and qBittorrent downloads for **unmodified DroppedNeedle** using Plugin API v1. This extracts the torrent integration from [alphyriver/DroppedNeedle](https://github.com/alphyriver/DroppedNeedle), so maintaining an application fork is no longer required for torrent acquisition.

Status: tested locally against upstream **v2.15.0** (`1cf117b4e0f3899d1daa786d316fd9ee8bcea1ab`) and main `cf7278a1`. Real-service deployment acceptance is still required. No production migration has been performed.

## Requirements

- DroppedNeedle v2.15.0 (tested); Plugin API v1 was introduced in v2.13.0.
- qBittorrent 5.2+ with a Web API bearer key, matching the original fork's authentication support. Username/password login is not implemented.
- Your Prowlarr instance, or one direct Torznab feed. Multiple direct feeds can be aggregated through Prowlarr.
- A persistent `/app/plugins` volume, a readable torrent downloads mount, and a **separate dedicated writable staging directory**. Allow space for an extra copy of each importing release.

## Install

Install from `https://github.com/alphyriver/droppedneedle-torrent-plugin` in **Settings > Plugins**, or copy this repository into `/app/plugins/prowlarr-qbittorrent`, keeping `plugin.toml` and `plugin.py` at that directory's root.

Configure the fields below, then enable the plugin. Installation alone does not enable it. API-key fields use the host's encrypted secret settings.

| Setting | Example / meaning |
| --- | --- |
| qBittorrent URL | `http://qbittorrent:8080` |
| qBittorrent API key | Your existing bearer API key |
| Torrent downloads mount | `/qbittorrent-downloads`; maps the qBittorrent category's save directory |
| Import staging directory | `/plugin-staging`; separate from torrent downloads and the library |
| qBittorrent category | `droppedneedle` (default); create it with the intended save directory first |
| Search backend | `prowlarr` (default) or `torznab` |
| Prowlarr URL / API key | Required for Prowlarr mode |
| Torznab URL / API key | Required for Torznab mode; accepts the complete feed `/api` URL |
| Audio category IDs | `3000` (default), comma-separated |

Select **Torrents** in source priority. Upstream's built-in Prowlarr panel remains a separate **Usenet** configuration. Torrent settings belong to this plugin.

## Behavior

- Filters Prowlarr results to torrents; excludes zero-seeder releases and audio-video category 3020. Deduplicates by reported info hash, otherwise magnet or download URL.
- Uses the host's artist/album matching and quality policy. Lossless, MP3 320/256/192 declarations become host quality tiers. Missing seeder counts cap confidence at 0.69, below the host's default 0.70 automatic threshold; lowering the host threshold can permit them automatically.
- Transfers whole releases, including for single-track requests; the host selects/imports the requested tracks.
- Correlates work by persistent qBittorrent task tags and category, including after a process restart or an ambiguous add response. Never automatically adopts or deletes an unrelated torrent already present in qBittorrent.
- Completed torrents keep seeding. Import receives **copies**, not hardlinks: moving or retagging those copies cannot alter the torrent payload. Interrupted staging is retryable; completed staging manifests survive restart and consumed files are not recopied.
- Cancel/discard deletes **incomplete, task-tagged torrents** and their partial data. Completed torrents are retained. Host cleanup sees staging paths only.
- Uses the host's shared HTTP client. It has no additional runtime package requirements beyond DroppedNeedle.

Keep the category save path and plugin paths stable while tasks are active. A pre-existing untagged torrent with the same hash is not adopted automatically; complete its import manually or resolve it in qBittorrent before retrying. A missing/removed torrent remains subject to the host's no-show timeout.

See [MIGRATION.md](MIGRATION.md) for the fork cutover, backup, validation, and rollback procedure.

## Tests

Use the Python environment that runs the selected upstream version:

```sh
DROPPEDNEEDLE_SOURCE=/path/to/DroppedNeedle python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```

Tests load the real upstream manifest, host, and adapters, with mock HTTP services and real temporary filesystem copies. They cover restart recovery, concurrent enqueue, ambiguous add failures, search filtering, Torznab XML bounds, path confinement, staging failure, and seeding preservation. They do not substitute for a real download and library import.

The prepared GitHub Actions workflow is in `ci/github-actions-test.yml`. The publishing credential lacks the `workflow` scope, so it is a template rather than an active workflow. A repository administrator can review and place it at `.github/workflows/test.yml`.

AGPL-3.0-only; see [LICENSE](LICENSE) and [NOTICE](NOTICE).
