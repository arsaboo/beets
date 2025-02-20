# This file is part of beets.
# Copyright 2019, Rahul Ahuja.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

"""Adds Deezer release and track search support to the autotagger"""

import collections
import time

import requests
import unidecode

from beets import ui
from beets.autotag import AlbumInfo, TrackInfo
from beets.dbcore import types
from beets.library import DateType
from beets.plugins import BeetsPlugin, MetadataSourcePlugin
from beets.util.id_extractors import deezer_id_regex


class DeezerPlugin(MetadataSourcePlugin, BeetsPlugin):
    data_source = "Deezer"

    item_types = {
        "deezer_track_rank": types.INTEGER,
        "deezer_track_id": types.INTEGER,
        "deezer_updated": DateType(),
    }

    # Base URLs for the Deezer API
    # Documentation: https://developers.deezer.com/api/
    search_url = "https://api.deezer.com/search/"
    album_url = "https://api.deezer.com/album/"
    track_url = "https://api.deezer.com/track/"

    id_regex = deezer_id_regex

    def __init__(self):
        super().__init__()

    def commands(self):
        """Add beet UI commands to interact with Deezer."""
        deezer_update_cmd = ui.Subcommand(
            "deezerupdate", help=f"Update {self.data_source} rank"
        )

        def func(lib, opts, args):
            items = lib.items(ui.decargs(args))
            self.deezerupdate(items, ui.should_write())

        deezer_update_cmd.func = func

        return [deezer_update_cmd]

    def fetch_data(self, url):
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            self._log.error("Error fetching data from {}\n Error: {}", url, e)
            return None
        if "error" in data:
            self._log.error("Deezer API error: {}", data["error"]["message"])
            return None
        return data

    def album_for_id(self, album_id):
        """Fetch an album by its Deezer ID or URL and return an AlbumInfo object."""
        try:
            deezer_id = str(album_id)  # Ensure ID is a string
            self._log.debug('Fetching album {} from Deezer', deezer_id)

            album_data = self.fetch_data(self.album_url + deezer_id)
            self._log.debug('Raw album data: {}', album_data)

            if not album_data or 'error' in album_data:
                return None

            # Extract basic info
            artist = album_data.get('artist', {}).get('name')
            artist_id = album_data.get('artist', {}).get('id')

            # Get tracks
            tracks = []
            medium_totals = collections.defaultdict(int)
            tracks_data = self.fetch_data(f"{self.album_url}{deezer_id}/tracks")

            if tracks_data and 'data' in tracks_data:
                for i, track_data in enumerate(tracks_data['data'], start=1):
                    try:
                        track = self._get_track(track_data)
                        track.index = i
                        track.medium = 1  # Default to medium 1 if not specified
                        medium_totals[track.medium] += 1
                        tracks.append(track)
                    except Exception as e:
                        self._log.debug('Error processing track {}: {}', i, str(e))

            # Create album info
            return AlbumInfo(
                album=album_data['title'],
                album_id=deezer_id,
                deezer_album_id=deezer_id,
                artist=artist,
                artist_id=artist_id,
                tracks=tracks,
                year=int(album_data['release_date'][:4]) if album_data.get('release_date') else None,
                data_source=self.data_source,
                data_url=album_data.get('link'),
                cover_art_url=album_data.get('cover_xl')
            )
        except Exception as e:
            self._log.error('Error creating album info: {} ({})', str(e), type(e).__name__)
            return None

    def _get_track(self, track_data):
        """Convert a Deezer track object dict to a TrackInfo object.

        :param track_data: Deezer Track object dict
        :type track_data: dict
        :return: TrackInfo object for track
        :rtype: beets.autotag.hooks.TrackInfo
        """
        artist, artist_id = self.get_artist(
            track_data.get("contributors", [track_data["artist"]])
        )
        return TrackInfo(
            title=track_data["title"],
            track_id=track_data["id"],
            deezer_track_id=track_data["id"],
            isrc=track_data.get("isrc"),
            artist=artist,
            artist_id=artist_id,
            length=track_data["duration"],
            index=track_data.get("track_position"),
            medium=track_data.get("disk_number"),
            deezer_track_rank=track_data.get("rank"),
            medium_index=track_data.get("track_position"),
            data_source=self.data_source,
            data_url=track_data["link"],
            deezer_updated=time.time(),
        )

    def track_for_id(self, track_id=None, track_data=None):
        """Fetch a track by its Deezer ID or URL and return a
        TrackInfo object or None if the track is not found.

        :param track_id: (Optional) Deezer ID or URL for the track. Either
            ``track_id`` or ``track_data`` must be provided.
        :type track_id: str
        :param track_data: (Optional) Simplified track object dict. May be
            provided instead of ``track_id`` to avoid unnecessary API calls.
        :type track_data: dict
        :return: TrackInfo object for track
        :rtype: beets.autotag.hooks.TrackInfo or None
        """
        if track_data is None:
            deezer_id = self._get_id("track", track_id, self.id_regex)
            if deezer_id is None:
                return None
            track_data = self.fetch_data(self.track_url + deezer_id)
            if track_data is None:
                return None
        track = self._get_track(track_data)

        # Get album's tracks to set `track.index` (position on the entire
        # release) and `track.medium_total` (total number of tracks on
        # the track's disc).
        album_tracks_obj = self.fetch_data(
            self.album_url + str(track_data["album"]["id"]) + "/tracks"
        )
        if album_tracks_obj is None:
            return None
        try:
            album_tracks_data = album_tracks_obj["data"]
        except KeyError:
            self._log.debug(
                "Error fetching album tracks for {}", track_data["album"]["id"]
            )
            return None
        medium_total = 0
        for i, track_data in enumerate(album_tracks_data, start=1):
            if track_data["disk_number"] == track.medium:
                medium_total += 1
                if track_data["id"] == track.track_id:
                    track.index = i
        track.medium_total = medium_total
        return track

    @staticmethod
    def _construct_search_query(filters=None, keywords=""):
        """Construct a query string with the specified filters and keywords to
        be provided to the Deezer Search API
        (https://developers.deezer.com/api/search).

        :param filters: (Optional) Field filters to apply.
        :type filters: dict
        :param keywords: (Optional) Query keywords to use.
        :type keywords: str
        :return: Query string to be provided to the Search API.
        :rtype: str
        """
        query_components = [
            keywords,
            " ".join(f'{k}:"{v}"' for k, v in filters.items()),
        ]
        query = " ".join([q for q in query_components if q])
        if not isinstance(query, str):
            query = query.decode("utf8")
        return unidecode.unidecode(query)

    def _search_api(self, query_type, filters=None, keywords=""):
        """Query the Deezer Search API."""
        try:
            # Ensure filters is a dict
            filters = filters or {}

            # Clean up keywords
            if isinstance(keywords, dict):
                keywords = keywords.get('album', '')
            elif isinstance(keywords, (list, tuple)):
                keywords = ' '.join(map(str, keywords))

            # Build query string - include both keywords and filters if present
            query_parts = []
            if keywords:
                query_parts.append(str(keywords).strip())
            if filters:
                filter_str = ' '.join(f'{k}:"{v}"' for k, v in filters.items())
                if filter_str:
                    query_parts.append(filter_str)

            query = ' '.join(query_parts).strip()
            if not query:
                return []

            self._log.debug("Searching Deezer with query: {}", query)

            # Make API request
            response = requests.get(
                f"{self.search_url}{query_type}",
                params={"q": query},
                timeout=10
            )
            response.raise_for_status()

            data = response.json()
            if not isinstance(data, dict):
                return []

            results = data.get("data", [])
            self._log.debug("Found {} results for query: {}", len(results), query)
            return results

        except Exception as e:
            self._log.debug("Search error: {} ({})", str(e), type(e).__name__)
            return []

    def deezerupdate(self, items, write):
        """Obtain rank information from Deezer."""
        for index, item in enumerate(items, start=1):
            self._log.info(
                "Processing {}/{} tracks - {} ", index, len(items), item
            )
            try:
                deezer_track_id = item.deezer_track_id
            except AttributeError:
                self._log.debug("No deezer_track_id present for: {}", item)
                continue
            try:
                rank = self.fetch_data(
                    f"{self.track_url}{deezer_track_id}"
                ).get("rank")
                self._log.debug(
                    "Deezer track: {} has {} rank", deezer_track_id, rank
                )
            except Exception as e:
                self._log.debug("Invalid Deezer track_id: {}", e)
                continue
            item.deezer_track_rank = int(rank)
            item.store()
            item.deezer_updated = time.time()
            if write:
                item.try_write()
