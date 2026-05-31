#!/usr/bin/env python3
"""Find Home Assistant entities that are not referenced anywhere:
neither in any YAML config file nor in any storage-mode dashboard.

Run it on the HA host inside the config directory:
    python3 find_unused_entities.py

Output is a list of entity_ids that appear in the entity registry but
are not mentioned as text anywhere in your configuration or dashboards.
Treat them as CANDIDATES to review, not as a delete list (see caveats below).
"""

import json
import re
from pathlib import Path

CONFIG_DIR = Path("/.")
STORAGE_DIR = CONFIG_DIR / ".storage"

# Domains you usually do NOT want flagged even if unreferenced
# (they exist on their own and are "used" implicitly). Adjust to taste.
IGNORE_DOMAINS = {
    "automation",
    "script",
    "scene",
    "zone",
    "person",
    "device_tracker",
    "update",
    "tts",
    "stt",
}


def load_entity_ids():
    """Read every registered entity_id from the entity registry."""
    registry = STORAGE_DIR / "core.entity_registry"
    data = json.loads(registry.read_text(encoding="utf-8"))
    ids = []
    for entry in data["data"]["entities"]:
        entity_id = entry["entity_id"]
        domain = entity_id.split(".")[0]
        if domain in IGNORE_DOMAINS:
            continue
        ids.append(entity_id)
    return ids


def build_corpus():
    """Concatenate everything an entity_id could be referenced in as text."""
    chunks = []

    # 1. All YAML config: automations.yaml, scripts.yaml, scenes.yaml,
    #    configuration.yaml, packages/*.yaml, template sensors, etc.
    for path in CONFIG_DIR.rglob("*.yaml"):
        if ".storage" in path.parts:
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            pass

    # 2. Storage-mode dashboards + the Energy dashboard preferences
    seen = set()
    for pattern in ("lovelace*", "*dashboard*", "energy"):
        for path in STORAGE_DIR.glob(pattern):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            try:
                chunks.append(path.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass

    return "\n".join(chunks)


def main():
    entity_ids = load_entity_ids()
    corpus = build_corpus()

    unused = []
    for entity_id in entity_ids:
        # Guard both sides so sensor.foo is not matched inside
        # binary_sensor.foo (left) or sensor.foo_2 (right).
        pattern = re.compile(r"(?<![\w.])" + re.escape(entity_id) + r"(?![\w])")
        if not pattern.search(corpus):
            unused.append(entity_id)

    unused.sort()
    print(
        f"# {len(unused)} of {len(entity_ids)} entities are not referenced "
        f"in YAML or dashboards\n"
    )
    for entity_id in unused:
        print(entity_id)


if __name__ == "__main__":
    main()
