import json

import pytest
from cryptography.fernet import Fernet

from prepare_migration import prepare


def test_candidate_preserves_secrets_and_source_config(tmp_path):
    key = Fernet.generate_key()
    cipher = Fernet(key)
    (tmp_path / ".env").write_text("DATA_ENC_KEY=" + key.decode() + "\n")
    qbt_key = cipher.encrypt(b"qbt-secret").decode()
    data = {
        "download_clients": {
            "qbittorrent": {
                "url": "http://qbt",
                "api_key": qbt_key,
                "downloads_mount": "/downloads",
            }
        },
        "prowlarr": {"url": "http://prowlarr", "api_key": "legacy-secret"},
        "source_priority": ["soulseek", "torrent"],
    }
    config = tmp_path / "config.json"
    config.write_text(json.dumps(data))
    before = config.read_bytes()
    output = tmp_path / "candidate.json"
    prepare(config, output, "/plugin-staging")
    candidate = json.loads(output.read_text())
    plugin = candidate["plugins"]["prowlarr-qbittorrent"]
    assert not plugin["enabled"]
    assert plugin["settings"]["qbittorrent_api_key"] == qbt_key
    assert cipher.decrypt(plugin["settings"]["prowlarr_api_key"].encode()) == b"legacy-secret"
    # Original legacy setting is preserved as-is, but the new plugin field is encrypted.
    assert candidate["source_priority"] == data["source_priority"]
    assert config.read_bytes() == before
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        prepare(config, output, "/plugin-staging")
    with pytest.raises(ValueError):
        prepare(config, config, "/plugin-staging")


def test_plugin_decrypts_legacy_ciphertext(tmp_path):
    from infrastructure import crypto

    import plugin

    old = crypto._fernet
    try:
        crypto._fernet = Fernet(Fernet.generate_key())
        value = crypto.encrypt("secret")
        assert plugin._secret(value) == "secret"
        assert plugin._secret("plaintext") == "plaintext"
        crypto._fernet = Fernet(Fernet.generate_key())
        with pytest.raises(ValueError):
            plugin._secret(value)
    finally:
        crypto._fernet = old
