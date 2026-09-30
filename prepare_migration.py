#!/usr/bin/env python3
"""Prepare a separate config candidate; never edit a running installation."""

import argparse
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from dotenv import dotenv_values

NAME = "prowlarr-qbittorrent"


def prepare(config: Path, output: Path, staging: str):
    if output.resolve() == config.resolve():
        raise ValueError("Output must be a separate candidate file")
    data = json.loads(config.read_text())
    if NAME in data.get("plugins", {}):
        raise ValueError("Plugin configuration already exists; refusing to overwrite it")
    key = dotenv_values(config.parent / ".env").get("DATA_ENC_KEY")
    if not key:
        raise ValueError("The existing config/.env DATA_ENC_KEY is required")
    crypto = Fernet(key.encode())

    def encrypted(value):
        if not value:
            raise ValueError("Both existing API keys must be configured")
        try:
            crypto.decrypt(value.encode())
            return value
        except InvalidToken:
            if value.startswith("gAAAA"):
                raise ValueError("An existing credential cannot be decrypted") from None
            return crypto.encrypt(value.strip().encode()).decode()

    qbt = data.get("download_clients", {}).get("qbittorrent", {})
    prowlarr = data.get("prowlarr", {})
    if not qbt.get("url") or not prowlarr.get("url"):
        raise ValueError("Existing qBittorrent and Prowlarr URLs are required")
    if not qbt.get("downloads_mount") or not Path(staging).is_absolute():
        raise ValueError("Existing download mount and absolute staging path are required")
    settings = {
        "qbittorrent_url": qbt["url"],
        "qbittorrent_api_key": encrypted(qbt.get("api_key", "")),
        "prowlarr_url": prowlarr["url"],
        "prowlarr_api_key": encrypted(prowlarr.get("api_key", "")),
        "downloads_path": qbt["downloads_mount"],
        "staging_path": staging,
        "category": qbt.get("category") or "droppedneedle",
        "search_backend": "prowlarr",
        "categories": ",".join(str(c) for c in prowlarr.get("categories", [3000])) or "3000",
    }
    data.setdefault("plugins", {})[NAME] = {"enabled": False, "settings": settings}
    # Do not change active sources until the operator enables/selects the plugin.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--staging", default="/plugin-staging")
    args = parser.parse_args()
    prepare(args.config, args.output, args.staging)
    print("Candidate written with plugin disabled; existing credentials remain encrypted.")
