# Retire the application fork

## Verified starting points (2026-09-30)

- Fork main: `51d8565d`; upstream main: `cf7278a1`; stable: `v2.15.0` / `1cf117b4`.
- Fork sync PR [#20](https://github.com/alphyriver/DroppedNeedle/pull/20) conflicts in 28 files. This plugin replaces the need to carry the fork's torrent changes; it does not merge that PR.
- Upstream ships Plugin API v1 and Usenet-only Prowlarr. Repository search found no existing compatible qBittorrent torrent-acquisition plugin. The similarly named qBittorrent search plugin is not a DroppedNeedle plugin.
- The checked-in Hercules stack uses `ghcr.io/alphyriver/droppedneedle:v2.6.0.post1`, existing config/cache/plugin volumes, and `/mnt/media/torrents/droppedneedle:/qbittorrent-downloads:ro`. These are repository values, not a live deployment inventory.

## Before cutover

1. Record the **actual running** image digest, Compose invocation, resolved volume names, PUID/PGID, and mounts. Stop accepting new requests and drain existing fork torrent tasks, including queued, retrying, processing, and partial tasks. Cancel unresolved work through the fork's UI only after deciding how to handle its partial data.
2. Keep completed seeding torrents in qBittorrent. Preserve their category/save path. Do not delete or re-add them as a migration step.
3. Stop DroppedNeedle. Back up the complete config, cache/database (including any WAL/SHM files), and plugins volumes together while stopped. Preserve the old image and Compose configuration. An application startup backup alone is not the rollback plan.
4. Stage and verify the upstream image and plugin files before modifying the live service. Install the plugin into the persistent plugins volume **disabled**. Test with a separate copy of config/cache and isolate it from real download clients so two instances cannot orchestrate the same queue.
5. Create `/mnt/media/droppedneedle/plugin-staging`, owned/writable by the service PUID/PGID. It is dedicated scratch storage, not the library or torrent save directory.

## Cutover configuration

The [Compose override](deploy/compose.upstream.yml) changes only the `musicseerr` image and adds the staging mount. Apply it as an additional `-f` file to the deployment's **existing complete Compose invocation**; it is not a standalone stack and does not replace the existing hardened-base extends, networks, or volumes.

Retain:

- `musicseerr-config:/app/config`
- `musicseerr-cache:/app/cache`
- `musicseerr-plugins:/app/plugins`
- `/mnt/media/torrents/droppedneedle:/qbittorrent-downloads:ro`
- Existing music, slskd, and Usenet mounts.

Prepare a separate configuration candidate using the existing `config.json` and adjacent `.env` encryption key:

```sh
python prepare_migration.py --config /path/to/copied-config/config.json \
  --output /path/to/copied-config/config.candidate.json --staging /plugin-staging
```

The helper never overwrites its input, writes the candidate mode 0600, and keeps the plugin disabled and source priority unchanged. It preserves existing encrypted API keys, encrypts legacy plaintext keys for the plugin, and does not print credentials. Run it against the stopped-volume backup immediately before cutover, so unrelated settings are not overwritten by an older candidate. Preserve the same `.env` key alongside the candidate. Review and promote the candidate to `config.json` only while the service is stopped.

Upstream v2.15.0 masks plugin secrets but saves their supplied values without encryption. The plugin accepts the fork's ciphertext directly and decrypts it with the host key. Do not replace migrated keys with plaintext via the plugin settings UI unless upstream has fixed that save path.

In **Settings > Plugins > Torrents**, check the migrated connection settings. Use:

| Plugin setting | Value |
| --- | --- |
| Downloads path | `/qbittorrent-downloads` |
| Staging path | `/plugin-staging` |
| Category | `droppedneedle` |
| Search backend | `prowlarr` (or the deliberately chosen direct Torznab feed) |

The qBittorrent category save path should remain `/data/torrents/droppedneedle`, matching the existing mounted host directory. Retain the actual configured URLs/keys rather than inferring them from Compose service names.

Enable the plugin and add **Torrents** to source priority. If Usenet is also used, configure upstream's built-in Usenet search backend separately. Do not blindly copy the fork's whole download-client settings object onto upstream.

### Existing tasks are not converted

The fork uses `source="torrent"` and additional torrent-specific handle fields. Upstream uses `source="plugin:prowlarr-qbittorrent"` and durable job-name correlation. This plugin does **not** rewrite historical database rows, active attempts, quarantine records, or source priorities. The optional helper copies connection settings into a separate disabled plugin configuration while preserving encrypted credentials. Completed library entries remain in the normal upstream library; old torrent task history is not guaranteed to remain actionable. Keep the stopped-volume backup and drain active work before switching. Recreate any wanted requests that still reference the retired source through the normal UI.

## Acceptance before retiring the fork

- Check startup upgrades, login, plugin health, library browsing, playback, and existing slskd/Usenet settings.
- Search for a known authorized release through Prowlarr, select the plugin result, and confirm qBittorrent receives exactly one correctly tagged/category-assigned task.
- Restart DroppedNeedle during transfer and verify it reattaches to that same task.
- Confirm import completes from staging, the library is playable, and the torrent's original bytes and seeding session remain intact. A qBittorrent force recheck should still pass.
- Verify a failed or cancelled incomplete plugin task cleans up only its own partial download/staging. Never use a valuable seeding torrent for this test.
- Verify the configured policy and source priority behave as intended, including manual selection for results with uncertain seeder counts.

Only after real-service acceptance: make the upstream image/staging mount permanent in the deployment repository, disable the fork's sync/image workflows, close PR #20 as superseded, and archive the fork. **Archive rather than delete** so old image provenance and rollback history remain available. The plugin repository remains maintained independently.

## Rollback

Stop upstream before restoring anything. Restore the stopped-volume config/cache/plugins snapshot as a unit and restore the exact previous image/configuration. Do not start the older fork against a database already upgraded by upstream. Keep original torrent files and qBittorrent sessions untouched. Reconcile any new post-cutover downloads/library imports explicitly, since rolling back metadata does not roll back external client activity or music files.
