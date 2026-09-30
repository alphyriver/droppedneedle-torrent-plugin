import json
import shutil
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import msgspec
import pytest
from infrastructure.plugins.adapters import PluginClientAdapter, PluginIndexerAdapter
from infrastructure.plugins.manifest import load_manifest
from infrastructure.plugins.protocols import DownloadClientProtocol, IndexerProtocol, PluginContext

import plugin as p


class Wire:
    def __init__(self):
        self.rows = []
        self.adds = 0
        self.deletes = []
        self.search_rows = [
            dict(
                title="Artist - Album FLAC",
                protocol="torrent",
                size=1000,
                seeders=5,
                infoHash="abc",
                downloadUrl="/download/abc?apikey=secret",
                categories=[{"id": 3040}],
            )
        ]
        self.xml = b"<rss><channel/></rss>"
        self.version = "v5.2.0"

    def __call__(self, req):
        if req.url.host == "qbt":
            assert req.headers["authorization"] == "Bearer qbt-key"
            if req.url.path.endswith("/app/version"):
                return httpx.Response(200, text=self.version)
            if req.url.path.endswith("/torrents/info"):
                rows = self.rows
                if req.url.params.get("tag"):
                    rows = [r for r in rows if req.url.params["tag"] in r["tags"].split(",")]
                return httpx.Response(200, json=rows)
            if req.url.path.endswith("/torrents/add"):
                self.adds += 1
                data = parse_qs(req.content.decode())
                self.rows.append(
                    dict(
                        hash="abc",
                        tags=data["tags"][0],
                        category=data["category"][0],
                        state="downloading",
                        progress=0.4,
                        size=1000,
                        downloaded=400,
                        save_path="/remote/music",
                        content_path="/remote/music/Album",
                    )
                )
                return httpx.Response(200, text="Ok.")
            if req.url.path.endswith("/torrents/delete"):
                self.deletes.append(parse_qs(req.content.decode()))
                self.rows.clear()
                return httpx.Response(200)
        if req.url.host == "prowlarr":
            assert req.headers["x-api-key"] == "prowlarr-key"
            if req.url.path.endswith("/system/status"):
                return httpx.Response(200, json={"version": "2.0"})
            assert req.url.params["categories"] == "3000"
            return httpx.Response(200, json=self.search_rows)
        if req.url.host == "torznab":
            return httpx.Response(200, content=self.xml)
        raise AssertionError(f"Unexpected endpoint {req.url.host}{req.url.path}")


@pytest.fixture
async def setup(tmp_path):
    settings = dict(
        qbittorrent_url="http://qbt",
        qbittorrent_api_key="qbt-key",
        prowlarr_url="http://prowlarr",
        prowlarr_api_key="prowlarr-key",
        downloads_path=str(tmp_path / "downloads"),
        staging_path=str(tmp_path / "staging"),
    )
    Path(settings["downloads_path"]).mkdir()
    wire = Wire()
    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as http:

        def factory():
            return p.TorrentPlugin(
                PluginContext(
                    plugin_name="prowlarr-qbittorrent", settings=lambda: settings, http=http
                )
            )

        yield factory, wire, settings


def test_manifest():
    manifest = load_manifest(Path(p.__file__).parent)
    assert manifest.api_version == 1
    assert manifest.capabilities == ["download_client", "indexer"]
    assert {f.key for f in manifest.settings if f.secret} == {
        "qbittorrent_api_key",
        "prowlarr_api_key",
        "torznab_api_key",
    }


async def test_wire_lifecycle_restart_and_seeding(setup, tmp_path):
    factory, wire, settings = setup
    instance = factory()
    assert isinstance(instance, DownloadClientProtocol)
    assert isinstance(instance, IndexerProtocol)
    assert (await instance.health_check()).status == "ok"
    indexer = PluginIndexerAdapter(
        plugin_name="prowlarr-qbittorrent", target=p.SOURCE, instance=instance
    )
    results = await indexer.search_album("Artist", "Album")
    assert len(results) == 1
    assert results[0].plugin.quality_tier == "lossless"
    client = PluginClientAdapter("prowlarr-qbittorrent", instance)
    request = p.EnqueueRequest(
        task_id="task1",
        source=p.SOURCE,
        job_name="droppedneedle-task1-0",
        payload=results[0].plugin.payload,
    )
    handle = await client.enqueue(request)
    assert wire.adds == 1
    assert (await client.get_status(handle)).status == "downloading"
    # Fresh plugin + adapter and a serialized handle, without any in-memory token map.
    client = PluginClientAdapter("prowlarr-qbittorrent", factory())
    handle = msgspec.json.decode(msgspec.json.encode(handle), type=p.TaskHandle)
    assert (await client.get_status(handle)).status == "downloading"
    await client.enqueue(request)
    assert wire.adds == 1
    album = Path(settings["downloads_path"]) / "Album"
    (album / "Disc 1").mkdir(parents=True)
    (album / "Disc 2").mkdir()
    originals = [album / "Disc 1/01.flac", album / "Disc 2/01.flac"]
    for i, source in enumerate(originals):
        source.write_bytes(f"torrent bytes {i}".encode())
    wire.rows[0].update(progress=1.0, state="uploading")
    assert (await client.get_status(handle)).status == "completed"
    staged = await client.list_completed_files(handle)
    assert len(staged) == 2
    assert all(str(path).startswith(settings["staging_path"]) for path in staged)
    evidence = await client.inspect_materialization(handle)
    assert all(str(path).startswith(settings["staging_path"]) for path in evidence.file_paths)
    assert not set(map(str, originals)) & set(evidence.file_paths)
    # Model the upstream importer's move + retag operations.
    library = tmp_path / "library"
    library.mkdir()
    for i, staged_file in enumerate(staged):
        dest = library / f"{i}.flac"
        shutil.move(staged_file, dest)
        dest.write_bytes(b"retagged imported music")
    client = PluginClientAdapter("prowlarr-qbittorrent", factory())
    assert await client.list_completed_files(handle) == []  # consumed files never recopied
    assert await client.abort(handle)
    assert await client.discard_client_artifacts(handle)
    assert wire.deletes == []
    assert len(wire.rows) == 1
    assert [path.read_bytes() for path in originals] == [b"torrent bytes 0", b"torrent bytes 1"]


async def test_abort_removes_only_incomplete_owned_torrent(setup):
    factory, wire, _ = setup
    instance = factory()
    request = p.EnqueueRequest(
        task_id="abc",
        source=p.SOURCE,
        payload=json.dumps({"magnet_url": "magnet:?xt=urn:btih:abc"}),
    )
    handle = await instance.enqueue(request)
    assert await instance.abort(handle)
    assert wire.deletes == [{"hashes": ["abc"], "deleteFiles": ["true"]}]
    assert await instance.abort(handle)
    assert len(wire.deletes) == 1


async def test_unowned_torrent_never_removed_even_if_api_ignores_tag_filter(setup):
    factory, wire, _ = setup
    instance = factory()

    async def unfiltered(**kwargs):
        return [
            p.QbtTorrentInfo(
                hash="other", tags="someone-elses-task", category="droppedneedle", progress=0.5
            )
        ]

    instance._validate = lambda: None
    instance._client.torrents_info = unfiltered
    assert await instance.abort(p.TaskHandle(source=p.SOURCE, job_name="droppedneedle-abc"))
    assert not wire.deletes


async def test_search_filters_and_deduplicates(setup):
    factory, wire, _ = setup
    row = wire.search_rows[0]
    wire.search_rows.extend(
        [
            dict(row),
            dict(row, protocol="usenet"),
            dict(row, seeders=0),
            dict(row, infoHash="video", categories=[{"id": 3020}]),
            dict(row, infoHash="unknown", seeders=None),
        ]
    )
    results = await factory().search_album("Artist", "Album")
    assert len(results) == 2
    assert results[1].plugin.score <= 0.69


async def test_direct_torznab(setup):
    factory, wire, settings = setup
    settings.update(
        search_backend="torznab", torznab_url="http://torznab/feed/api", torznab_api_key="tor-key"
    )
    wire.xml = b"""<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel><item>
    <title>Artist - Album FLAC</title><enclosure url="/download/abc" length="1234"/>
    <torznab:attr name="seeders" value="5"/><torznab:attr name="category" value="3040"/>
    </item></channel></rss>"""
    results = await factory().search_album("Artist", "Album")
    assert len(results) == 1
    assert results[0].plugin.size_bytes == 1234
    assert json.loads(results[0].plugin.payload)["download_url"] == "http://torznab/download/abc"


@pytest.mark.parametrize(
    "xml", [b'<!DOCTYPE rss [<!ENTITY a "bad">]><rss/>', "<!DOCTYPE rss><rss/>".encode("utf-16")]
)
async def test_rejects_xml_entities(setup, xml):
    factory, wire, settings = setup
    settings.update(
        search_backend="torznab", torznab_url="http://torznab", torznab_api_key="tor-key"
    )
    wire.xml = xml
    with pytest.raises((ValueError, UnicodeError)):
        await factory().search_album("Artist", "Album")


async def test_live_settings_validation(setup):
    factory, _, settings = setup
    instance = factory()
    assert instance.is_configured()
    settings["staging_path"] = str(Path(settings["downloads_path"]) / "nested")
    assert not instance.is_configured()
    assert (await instance.health_check()).status == "error"


async def test_staging_rejects_symlink_escape(setup, tmp_path):
    factory, _, settings = setup
    instance = factory()
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"private")
    source = Path(settings["downloads_path"]) / "escape.flac"
    source.symlink_to(outside)
    with pytest.raises(ValueError):
        instance._stage_files(
            instance._job_dir(p.TaskHandle(source=p.SOURCE, job_name="droppedneedle-test")),
            [source],
        )
    assert outside.read_bytes() == b"private"


async def test_staging_rejects_job_symlink(setup, tmp_path):
    factory, _, _ = setup
    instance = factory()
    handle = p.TaskHandle(source=p.SOURCE, job_name="droppedneedle-test")
    job = instance._job_dir(handle)
    job.parent.mkdir()
    job.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        await instance.discard_client_artifacts(handle)


async def test_enqueue_rejects_multiple_urls_in_one_value(setup):
    factory, wire, _ = setup
    with pytest.raises(ValueError):
        await factory().enqueue(
            p.EnqueueRequest(
                task_id="test",
                source=p.SOURCE,
                payload=json.dumps({"download_url": "http://x/a\nhttp://x/b"}),
            )
        )
    assert wire.adds == 0


async def test_health_rejects_old_qbittorrent(setup):
    factory, wire, _ = setup
    wire.version = "v5.1.4"
    assert (await factory().health_check()).status == "error"


async def test_real_host_loads_disabled_then_activates_both_capabilities(
    setup, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from infrastructure.plugins.host import PluginHost

    factory, _, settings = setup
    instance = factory()
    root = tmp_path / "plugins"
    dest = root / "prowlarr-qbittorrent"
    dest.mkdir(parents=True)
    for name in ("plugin.py", "plugin.toml"):
        shutil.copyfile(Path(p.__file__).parent / name, dest / name)
    config = SimpleNamespace(enabled=False, settings=settings)
    prefs = SimpleNamespace(get_plugin_config=lambda name: config)
    monkeypatch.setattr(
        PluginHost, "_plugin_http_client", staticmethod(lambda name: instance.ctx.http)
    )
    host = PluginHost(plugins_dir=root, preferences_service=prefs)
    host.load_all()
    assert host.get("prowlarr-qbittorrent").instance is None
    config.enabled = True
    host.load_all()
    loaded = host.get("prowlarr-qbittorrent")
    assert loaded.error is None
    assert loaded.active_capabilities == ["download_client", "indexer"]
    assert (await loaded.instance.health_check()).status == "ok"


@pytest.mark.parametrize("state", ["moving", "checkingUP", "checkingResumeData"])
async def test_completed_torrent_not_imported_during_move_or_check(setup, state):
    factory, wire, settings = setup
    instance = factory()
    handle = await instance.enqueue(
        p.EnqueueRequest(
            task_id="test",
            source=p.SOURCE,
            payload=json.dumps({"magnet_url": "magnet:?xt=urn:btih:abc"}),
        )
    )
    wire.rows[0].update(progress=1.0, state=state)
    assert (await instance.get_status(handle)).status == "processing"
    assert await instance.list_completed_files(handle) == []
    assert await instance.abort(handle)
    assert wire.deletes == []


async def test_path_mapping_rejects_escape(setup):
    factory, _, _ = setup
    instance = factory()
    for path in ("/elsewhere/Album", "/remote/music/../Album", "/remote/music"):
        with pytest.raises(ValueError):
            instance._local_path(p.QbtTorrentInfo(save_path="/remote/music", content_path=path))


async def test_failed_copy_is_not_published_and_can_retry(setup, monkeypatch):
    factory, _, settings = setup
    instance = factory()
    source = Path(settings["downloads_path"]) / "track.flac"
    source.write_bytes(b"unchanged")
    handle = p.TaskHandle(source=p.SOURCE, job_name="droppedneedle-test")
    job = instance._job_dir(handle)
    original = p.shutil.copyfileobj

    def broken(*args):
        raise OSError("disk full")

    monkeypatch.setattr(p.shutil, "copyfileobj", broken)
    with pytest.raises(OSError):
        instance._stage_files(job, [source])
    assert not (job / ".ready.json").exists()
    assert source.read_bytes() == b"unchanged"
    monkeypatch.setattr(p.shutil, "copyfileobj", original)
    assert len(instance._stage_files(job, [source])) == 1


def test_quality_tiers_are_host_keys():
    from services.native.quality_tiers import TIER_KEYS

    assert p._quality("Artist Album MP3 320", []) in TIER_KEYS
    assert p._quality("Artist Album", [3040]) == "lossless"


async def test_concurrent_enqueue_adds_once(setup):
    import asyncio

    factory, wire, _ = setup
    instance = factory()
    request = p.EnqueueRequest(
        task_id="test",
        source=p.SOURCE,
        payload=json.dumps({"magnet_url": "magnet:?xt=urn:btih:abc"}),
    )
    handles = await asyncio.gather(instance.enqueue(request), instance.enqueue(request))
    assert wire.adds == 1
    assert handles[0].job_name == handles[1].job_name


async def test_ambiguous_add_error_does_not_submit_fallback(setup, monkeypatch):
    factory, wire, _ = setup
    calls = []

    async def failed_add(self, **kwargs):
        calls.append(kwargs)
        raise p.QbittorrentApiError("ambiguous timeout")

    monkeypatch.setattr(p.QbittorrentClient, "add_torrent", failed_add)
    with pytest.raises(p.QbittorrentApiError):
        await factory().enqueue(
            p.EnqueueRequest(
                task_id="test",
                source=p.SOURCE,
                payload=json.dumps(
                    {
                        "magnet_url": "magnet:?xt=urn:btih:abc",
                        "download_url": "http://prowlarr/grab",
                    }
                ),
            )
        )
    assert len(calls) == 1


async def test_add_timeout_recovers_tagged_torrent(setup, monkeypatch):
    factory, wire, _ = setup
    original = p.QbittorrentClient.add_torrent

    async def timed_out_after_accept(self, **kwargs):
        await original(self, **kwargs)
        raise p.QbittorrentApiError("ambiguous timeout")

    monkeypatch.setattr(p.QbittorrentClient, "add_torrent", timed_out_after_accept)
    instance = factory()
    handle = await instance.enqueue(
        p.EnqueueRequest(
            task_id="test",
            source=p.SOURCE,
            payload=json.dumps({"magnet_url": "magnet:?xt=urn:btih:abc"}),
        )
    )
    assert (await instance.get_status(handle)).status == "downloading"
    assert wire.adds == 1
