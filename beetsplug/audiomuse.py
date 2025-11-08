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

import requests

from beets import ui
from beets.dbcore import types
from beets.plugins import BeetsPlugin


class AudioMusePlugin(BeetsPlugin):
    item_types = {
        # store remote item id from AudioMuse media server
        "audiomuse_item_id": types.STRING,
    }

    def __init__(self):
        super().__init__()
        self.config.add({"url": "http://127.0.0.1:8001"})
        # redact if future auth keys added
        self.base_url = self.config["url"].as_str().rstrip("/")

    def _search_track(self, title: str, artist: str):
        """Query AudioMuse /api/search_tracks endpoint.

        Returns list[dict] each containing at least: title, author, item_id.
        """
        endpoint = f"{self.base_url}/api/search_tracks"
        try:
            resp = requests.get(
                endpoint,
                params={"title": title, "artist": artist},
                timeout=10,
            )
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

    def _match_first(self, data, title: str, artist: str):
        """Return best match candidate.

        Prefer case-insensitive exact match on both title and author.
        Fall back to first element.
        """
        title_lower = title.lower()
        artist_lower = artist.lower()
        for row in data:
            try:
                if (
                    row.get("title", "").lower() == title_lower
                    and row.get("author", "").lower() == artist_lower
                ):
                    return row
            except AttributeError:
                continue
        return data[0] if data else None

    def commands(self):
        cmd = ui.Subcommand(
            "audiomusesearch", help="search AudioMuse for track item_id"
        )
        cmd.parser.add_option(
            "-s",
            "--set",
            action="store_true",
            dest="set_field",
            help="store audiomuse_item_id flexible field",
        )
        cmd.parser.add_option(
            "-w",
            "--write",
            action="store_true",
            dest="write_tags",
            help="write tags to file after setting field",
        )

        def func(lib, opts, args):
            items = lib.items(args)
            if not items:
                self._log.info("No matching items for query.")
                return
            self._log.info("Querying AudioMuse for {} items", len(items))
            for item in items:
                title = item.title
                artist = item.artist or item.albumartist
                if not title or not artist:
                    self._log.debug("Skipping item missing title/artist: {}", item)
                    continue
                results = self._search_track(title, artist)
                match = self._match_first(results, title, artist)
                if match:
                    item_id = match.get("item_id")
                    author = match.get("author")
                    self._log.info(
                        "Match: '{}' - '{}' => item_id {}", title, author, item_id
                    )
                    if opts.set_field and item_id:
                        item["audiomuse_item_id"] = item_id
                        item.store()
                        if opts.write_tags and ui.should_write():
                            item.try_write()
                else:
                    self._log.info("No AudioMuse match: '{}' - '{}'", title, artist)

        cmd.func = func
        return [cmd]
