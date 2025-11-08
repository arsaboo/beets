"""AudioMuse plugin.

Provides basic track search against an AudioMuse-AI server
to retrieve the media server `item_id` given a beets track's
title and artist. AudioMuse uses the field name `author` for
the artist internally.

Configuration example (in beets config.yaml):

    audiomuse:
      url: "http://192.168.2.162:8001"  # AudioMuse base URL

Usage:

    beet audiomusesearch QUERY

Where QUERY is any beets item query (e.g. album:XYZ). For each
matching item we call /api/search_tracks?title=...&artist=..., pick
the first match (exact title+author case-insensitive) and display it.

Optional flags:
    --set : store the returned item_id into flexible field `audiomuse_item_id`
    --write : after setting the field, write tags to file if possible.

Limitations:
 - Only basic search is implemented; similarity endpoints and others
   can be added later.
 - No retries/backoff; simple HTTP GET with timeout.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

import requests

from beets import ui
from beets.dbcore import types
from beets.plugins import BeetsPlugin


class AudioMusePlugin(BeetsPlugin):
    item_types = {
        # store remote item id from AudioMuse media server
        "audiomuse_item_id": types.STRING,
        # embedding vector as JSON string
        "audiomuse_embedding": types.STRING,
        # selected score fields (others are dynamic and stored as
        # strings/floats implicitly)
        "audiomuse_energy": types.FLOAT,
        "audiomuse_tempo": types.FLOAT,
        "audiomuse_key": types.STRING,
        "audiomuse_scale": types.STRING,
    }

    def __init__(self):
        super().__init__()
        self.config.add({"url": "http://127.0.0.1:8001"})
        # redact if future auth keys added
        self.base_url = self.config["url"].as_str().rstrip("/")

    def _search_track(self, title: str, artist: str | None = None):
        """Query AudioMuse /api/search_tracks endpoint.

        Returns list[dict] each containing at least: title, author, item_id.
        """
        endpoint = f"{self.base_url}/api/search_tracks"
        try:
            params = {"title": title}
            if artist:
                params["artist"] = artist
            resp = requests.get(endpoint, params=params, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as exc:
            self._log.debug("AudioMuse request failed: {}", exc)
            return []

        try:
            data = resp.json()
        except ValueError:
            self._log.debug("AudioMuse invalid JSON response: {}", resp.text[:200])
            return []

        if not isinstance(data, list):
            self._log.debug("AudioMuse unexpected payload type: {}", type(data))
            return []
        return data

    def _get_item_id_for_item(self, item) -> Optional[str]:
        """Return AudioMuse item_id for a beets item, searching if needed.

        Stores the item_id on success and returns it.
        """
        try:
            current = item.get("audiomuse_item_id")
        except Exception:
            current = None
        if current:
            return current

        title = item.title
        artist = item.artist or item.albumartist
        if not title or not artist:
            return None

        artist_tokens = self._split_artists(artist)
        primary = artist_tokens[0] if artist_tokens else artist
        results = self._search_track(title, primary)
        if not results:
            results = self._search_track(title, None)
        match = self._match_first(results, title, artist)
        if not match:
            return None
        item_id = match.get("item_id")
        if item_id:
            item["audiomuse_item_id"] = item_id
            item.store()
        return item_id

    def _slug(self, s: str) -> str:
        s = s.lower().strip()
        s = re.sub(r"[^a-z0-9]+", "_", s)
        s = re.sub(r"_+", "_", s).strip("_")
        return s

    def _parse_kv_string(self, s: str) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for part in (s or "").split(","):
            if not part:
                continue
            if ":" not in part:
                continue
            k, v = part.split(":", 1)
            k = self._slug(k)
            try:
                out[k] = float(v)
            except ValueError:
                continue
        return out

    def _get_similar_tracks(self, item_id: str, count: int = 20) -> List[Dict]:
        """Get similar tracks from AudioMuse /api/similar_tracks endpoint.

        Returns list of dicts with item_id, title, author fields.
        """
        endpoint = f"{self.base_url}/api/similar_tracks"
        try:
            params = {"item_id": item_id, "n": str(count)}
            resp = requests.get(endpoint, params=params, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as exc:
            self._log.debug("AudioMuse similar_tracks request failed: {}", exc)
            return []

        try:
            data = resp.json()
        except ValueError:
            self._log.debug(
                "AudioMuse similar_tracks invalid JSON: {}", resp.text[:200]
            )
            return []

        if not isinstance(data, list):
            self._log.debug(
                "AudioMuse similar_tracks unexpected type: {}", type(data)
            )
            return []
        return data

    def _split_artists(self, s: str) -> List[str]:
        # Normalize common separators: comma, bullet, ampersand, and/&, feat/ft/
        # featuring, x/×, slash, plus.
        if not s:
            return []
        normalized = re.sub(
            r"\s*(feat\.|featuring|ft\.|&|and|×|x|/|\+|•|,)\s*",
            ",",
            s,
            flags=re.IGNORECASE,
        )
        parts = [p.strip() for p in normalized.split(",") if p.strip()]
        return parts

    def _authors_match(self, a: str, b: str) -> bool:
        aset = {p.lower() for p in self._split_artists(a)}
        bset = {p.lower() for p in self._split_artists(b)}
        if not aset or not bset:
            return a.lower() == b.lower()
        return aset == bset or aset.issubset(bset) or bset.issubset(aset)

    def _match_first(self, data, title: str, artist: str):
        """Return best match candidate.

        Priority:
        - exact title and normalized author match
        - exact title match
        - first element
        """
        title_lower = (title or "").lower()
        # exact title + normalized author
        for row in data:
            try:
                if row.get("title", "").lower() == title_lower and self._authors_match(
                    row.get("author", ""), artist
                ):
                    return row
            except Exception:
                continue
        # exact title
        for row in data:
            try:
                if row.get("title", "").lower() == title_lower:
                    return row
            except Exception:
                continue
        return data[0] if data else None

    def commands(self):
        # 1) Match: ensure audiomuse_item_id for items
        def match_func(lib, opts, args):
            items = lib.items(args)
            if not items:
                self._log.info("No matching items for query.")
                return
            self._log.info("Matching AudioMuse item_id for {} items", len(items))
            for item in items:
                item_id = self._get_item_id_for_item(item)
                if item_id:
                    self._log.info("item_id set: {} -> {}", item, item_id)
                else:
                    self._log.info(
                        "No AudioMuse match: '{}' - '{}'",
                        item.title,
                        item.artist or item.albumartist,
                    )

        match_cmd = ui.Subcommand(
            "audiomuse_match",
            help="resolve and store AudioMuse item_id for items",
        )
        match_cmd.func = match_func

        # 2) Get embedding vector
        def embedding_func(lib, opts, args):
            items = lib.items(args)
            if not items:
                self._log.info("No matching items for query.")
                return
            base = f"{self.base_url}/external/get_embedding"
            action = "Previewing" if opts.pretend else "Fetching"
            self._log.info("{} embeddings for {} items", action, len(items))
            for item in items:
                item_id = self._get_item_id_for_item(item) if not opts.pretend else item.get("audiomuse_item_id")
                if not item_id:
                    self._log.info("Skipping (no item_id): {}", item)
                    continue
                try:
                    resp = requests.get(base, params={"id": item_id}, timeout=10)
                    resp.raise_for_status()
                    data = resp.json()
                except requests.RequestException as exc:
                    self._log.debug("Embedding request failed: {}", exc)
                    continue
                except ValueError:
                    self._log.debug("Embedding invalid JSON for id {}", item_id)
                    continue

                vector: Optional[List[float]] = None
                if isinstance(data, list):
                    vector = data
                elif isinstance(data, dict):
                    if isinstance(data.get("vector"), list):
                        vector = data["vector"]
                    elif isinstance(data.get("embedding"), list):
                        vector = data["embedding"]

                if not vector:
                    self._log.debug("No embedding vector found for {}", item_id)
                    continue

                if opts.pretend:
                    self._log.info(
                        "Would store embedding for {} ({} dims)", item, len(vector)
                    )
                else:
                    item["audiomuse_embedding"] = json.dumps(vector)
                    item.store()
                    self._log.info(
                        "Stored embedding for {} ({} dims)", item, len(vector)
                    )

        embedding_cmd = ui.Subcommand(
            "audiomuse_get_embedding",
            help="fetch and store AudioMuse embedding vector",
        )
        embedding_cmd.parser.add_option(
            "-p",
            "--pretend",
            action="store_true",
            dest="pretend",
            help="preview embeddings without storing to database",
        )
        embedding_cmd.func = embedding_func

        # 3) Get score details
        def score_func(lib, opts, args):
            items = lib.items(args)
            if not items:
                self._log.info("No matching items for query.")
                return
            base = f"{self.base_url}/external/get_score"
            action = "Previewing" if opts.pretend else "Fetching"
            self._log.info("{} scores for {} items", action, len(items))
            for item in items:
                if opts.pretend:
                    item_id = item.get("audiomuse_item_id")
                else:
                    item_id = self._get_item_id_for_item(item)
                if not item_id:
                    self._log.info("Skipping (no item_id): {}", item)
                    continue
                try:
                    resp = requests.get(base, params={"id": item_id}, timeout=10)
                    resp.raise_for_status()
                    data = resp.json()
                except requests.RequestException as exc:
                    self._log.debug("Score request failed: {}", exc)
                    continue
                except ValueError:
                    self._log.debug("Score invalid JSON for id {}", item_id)
                    continue

                if not isinstance(data, dict):
                    self._log.debug("Unexpected score payload type: {}", type(data))
                    continue

                if opts.pretend:
                    # Just show what would be stored
                    fields = []
                    if "energy" in data:
                        fields.append(f"energy={data['energy']}")
                    if "tempo" in data:
                        fields.append(f"tempo={data['tempo']}")
                    if "key" in data:
                        fields.append(f"key={data['key']}")
                    if "scale" in data:
                        fields.append(f"scale={data['scale']}")
                    if "mood_vector" in data:
                        fields.append(f"mood_vector={data['mood_vector'][:50]}...")
                    if "other_features" in data:
                        fields.append(f"other_features={data['other_features'][:50]}...")
                    self._log.info("Would store for {}: {}", item, ", ".join(fields))
                else:
                    if "energy" in data:
                        try:
                            item["audiomuse_energy"] = float(data["energy"])  # type: ignore[assignment]
                        except Exception:
                            pass
                    if "tempo" in data:
                        try:
                            item["audiomuse_tempo"] = float(data["tempo"])  # type: ignore[assignment]
                        except Exception:
                            pass
                    if "key" in data:
                        item["audiomuse_key"] = str(data["key"])  # type: ignore[assignment]
                    if "scale" in data:
                        item["audiomuse_scale"] = str(data["scale"])  # type: ignore[assignment]

                    if "mood_vector" in data:
                        mv_str = str(data["mood_vector"])  # e.g., "hip-hop:0.55,..."
                        item["audiomuse_mood_vector"] = mv_str
                        for label, val in self._parse_kv_string(mv_str).items():
                            item[f"audiomuse_mood_{label}"] = val
                    if "other_features" in data:
                        # e.g., "danceable:0.99,..."
                        of_str = str(data["other_features"])
                        item["audiomuse_other_features"] = of_str
                        for label, val in self._parse_kv_string(of_str).items():
                            item[f"audiomuse_{label}"] = val

                    if data.get("item_id"):
                        item["audiomuse_item_id"] = data["item_id"]

                    item.store()
                    self._log.info("Stored scores for {}", item)

        score_cmd = ui.Subcommand(
            "audiomuse_get_score",
            help="fetch and store AudioMuse score details",
        )
        score_cmd.parser.add_option(
            "-p",
            "--pretend",
            action="store_true",
            dest="pretend",
            help="preview scores without storing to database",
        )
        score_cmd.func = score_func

        # 4) Get similar tracks
        def similar_func(lib, opts, args):
            items = lib.items(args)
            if not items:
                self._log.info("No matching items for query.")
                return
            count = opts.count or 20
            self._log.info("Finding similar tracks for {} items", len(items))
            for item in items:
                item_id = self._get_item_id_for_item(item)
                if not item_id:
                    self._log.info("Skipping (no item_id): {}", item)
                    continue

                similar = self._get_similar_tracks(item_id, count)
                if similar:
                    self._log.info(
                        "Found {} similar tracks for '{}' - '{}'",
                        len(similar), item.title, item.artist
                    )
                    for track in similar[:10]:  # Show first 10
                        self._log.info(
                            "  → '{}' - '{}'",
                            track.get("title", "?"),
                            track.get("author", "?")
                        )
                else:
                    self._log.info("No similar tracks found for {}", item)

        similar_cmd = ui.Subcommand(
            "audiomuse_similar",
            help="find similar tracks using AudioMuse-AI embeddings",
        )
        similar_cmd.parser.add_option(
            "-n",
            "--count",
            type="int",
            dest="count",
            default=20,
            help="number of similar tracks to retrieve (default: 20)",
        )
        similar_cmd.func = similar_func

        return [match_cmd, embedding_cmd, score_cmd, similar_cmd]

