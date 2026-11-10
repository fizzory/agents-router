"""Release-only updater metadata and configuration checks (standard library)."""
import base64
import binascii
from datetime import datetime, timezone
import json
from urllib.parse import quote

PLACEHOLDER = "AGT_UPDATER_PUBLIC_KEY_REQUIRED_BEFORE_RELEASE"
ENDPOINT = "https://github.com/Watcharagorn/agents-router/releases/latest/download/latest.json"


def validate_public_key(key):
    try:
        # Tauri stores a base64-encoded minisign public key file, not the raw key.
        text = base64.b64decode(key, validate=True).decode("ascii").strip().splitlines()
        raw = base64.b64decode(text[1], validate=True)
        if len(text) != 2 or not text[0].startswith("untrusted comment: ") or len(raw) != 42 or raw[:2] != b"Ed":
            raise ValueError()
        if any(word in text[0].lower() for word in ("throwaway", "test", "placeholder")):
            raise ValueError()
    except (ValueError, TypeError, IndexError, UnicodeError, binascii.Error):
        raise ValueError("Replace AGT_UPDATER_PUBLIC_KEY_REQUIRED_BEFORE_RELEASE with the maintainer's real public key before publishing") from None


def validate_release(config):
    updater = config.get("plugins", {}).get("updater", {})
    validate_public_key(updater.get("pubkey"))
    if updater.get("endpoints") != [ENDPOINT] or "dangerousInsecureTransportProtocol" in json.dumps(config):
        raise ValueError("Release updater must use the GitHub Releases HTTPS endpoint")


def latest(version, archive, signature, notes=""):
    if not signature.strip():
        raise ValueError("Missing updater archive signature")
    return {
        "version": version, "notes": notes,
        "pub_date": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "platforms": {"darwin-aarch64": {
            "url": f"https://github.com/Watcharagorn/agents-router/releases/download/v{version}/{quote(archive)}",
            "signature": signature.strip(),
        }},
    }
