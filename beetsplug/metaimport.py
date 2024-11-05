from beets import config, ui, plugins, autotag
from beets.plugins import BeetsPlugin
from beets.ui import print_, colorize
from beets.util import displayable_path
from beets.autotag import hooks, match
from collections import defaultdict

# Import supported plugins directly
from beetsplug.spotify import SpotifyPlugin
from beetsplug.deezer import DeezerPlugin

class MetaImportPlugin(BeetsPlugin):
    # Map of supported source names to their plugin classes
    SUPPORTED_SOURCES = {
        'spotify': SpotifyPlugin,
        'deezer': DeezerPlugin
    }

    def __init__(self):
        super().__init__()

        # Default config
        self.config.add({
            'sources': [],  # List of metadata sources in order of preference
            'exclude_fields': [],  # Fields to exclude from metadata import
            'match_threshold': 0.25,  # Distance threshold for considering albums the same
        })

        # Initialize source plugins
        self.sources = []
        self.source_plugins = {}

        # Only try to load sources if they are explicitly configured
        if self.config['sources'].exists():
            configured_sources = self.config['sources'].as_str_seq()
            if configured_sources:
                self._log.debug(f'Configured sources: {configured_sources}')
                for source in configured_sources:
                    if source not in self.SUPPORTED_SOURCES:
                        self._log.warning(f'Unsupported source: {source}')
                        continue
                    self._init_source(source)
            else:
                self._log.debug('No sources configured in metaimport.sources')

        # Register as a metadata provider
        self.register_listener('album_candidates', self.candidates)

    def _init_source(self, source):
        """Initialize a single source plugin."""
        try:
            # Get the plugin class from our supported sources
            plugin_class = self.SUPPORTED_SOURCES[source]
            # Instantiate the plugin
            plugin = plugin_class()
            self.source_plugins[source] = plugin
            self.sources.append(source)
            self._log.debug(f'Successfully loaded source plugin: {source}')
        except Exception as e:
            self._log.warning(f'Failed to initialize source {source}: {str(e)}')

    def commands(self):
        cmd = ui.Subcommand(
            'metaimport',
            help='import metadata from configured sources'
        )
        cmd.func = self._command
        return [cmd]

    def _command(self, lib, opts, args):
        """Main command implementation."""
        if not self.sources:
            self._log.warning('No valid metadata sources configured. Supported sources: {}'.format(
                ', '.join(self.SUPPORTED_SOURCES.keys())
            ))
            return

        items = lib.items(ui.decargs(args))
        if not items:
            self._log.warning('No items matched your query')
            return

        # Group items by album
        albums = {}
        for item in items:
            key = (item.albumartist or item.artist, item.album)
            if key not in albums:
                albums[key] = []
            albums[key].append(item)

        # Use beets' built-in import process
        for (albumartist, album_name), items in albums.items():
            print_(colorize('text_highlight', '\nProcessing album:'))
            print_(colorize('text', f'  {albumartist} - {album_name}'))
            print_(colorize('text', f'  {len(items)} tracks'))

            # Let beets handle the import process
            # The candidates hook will provide matches from our sources
            autotag.tag_album(items)

    def _albums_match(self, album1, album2):
        """Check if two albums are likely the same based on metadata."""
        # Create a mapping between tracks
        mapping, extra_items, extra_tracks = match.assign_items(
            album1.tracks, album2.tracks
        )

        # Calculate distance between albums
        dist = match.distance(album1.tracks, album2, mapping)

        # Return True if distance is below threshold
        return dist.distance <= self.config['match_threshold'].as_number()

    def _merge_album_info(self, albums):
        """Merge metadata from multiple album matches."""
        if not albums:
            return None

        # Use the first album as base
        base = albums[0]

        # Create a new AlbumInfo object with merged data
        merged = hooks.AlbumInfo(
            album=base.album,
            album_id=base.album_id,
            artist=base.artist,
            artist_id=base.artist_id,
            tracks=base.tracks,
            asin=base.asin,
            albumtype=base.albumtype,
            va=base.va,
            year=base.year,
            month=base.month,
            day=base.day,
            label=base.label,
            mediums=base.mediums,
            artist_sort=base.artist_sort,
            releasegroup_id=base.releasegroup_id,
            catalognum=base.catalognum,
            script=base.script,
            language=base.language,
            country=base.country,
            style=base.style,
            genre=base.genre,
            albumstatus=base.albumstatus,
            media=base.media,
            albumdisambig=base.albumdisambig,
            artist_credit=base.artist_credit,
            data_source=f"merged({','.join(a.data_source for a in albums)})",
            data_url=base.data_url
        )

        # Merge additional metadata from other matches
        for other in albums[1:]:
            # Fill in missing fields from other sources
            for field in ['year', 'month', 'day', 'label', 'catalognum',
                         'country', 'media', 'albumdisambig']:
                if not getattr(merged, field) and getattr(other, field):
                    setattr(merged, field, getattr(other, field))

            # Merge genre/style lists if present
            if other.genre and merged.genre:
                merged.genre = list(set(merged.genre + other.genre))
            elif other.genre:
                merged.genre = other.genre

            if other.style and merged.style:
                merged.style = list(set(merged.style + other.style))
            elif other.style:
                merged.style = other.style

        return merged

    def candidates(self, items, artist, album, va_likely, extra_tags=None):
        """Hook for providing metadata matches during import."""
        # Group matches by album to detect same album across sources
        album_groups = defaultdict(list)

        for source in self.sources:
            try:
                plugin = self.source_plugins[source]
                # Search for the album using plugin's search capabilities
                results = plugin._search_api('album', keywords=album,
                                          filters={'artist': artist})

                if results:
                    for result in results:
                        # Get album info from the source plugin
                        album_id = str(result['id'])
                        album_info = plugin.album_for_id(album_id)

                        if album_info:
                            # Set the data source
                            album_info.data_source = source

                            # Try to find matching album group
                            matched = False
                            for group_id, group in album_groups.items():
                                if any(self._albums_match(album_info, a) for a in group):
                                    group.append(album_info)
                                    matched = True
                                    break

                            # Create new group if no match found
                            if not matched:
                                group_id = f"group_{len(album_groups)}"
                                album_groups[group_id].append(album_info)

                            self._log.debug(f'Found metadata from {source}')
            except Exception as e:
                self._log.warning('Error getting metadata from {}: {}',
                                source, str(e))

        # Merge matches and return candidates
        matches = []
        for group in album_groups.values():
            merged = self._merge_album_info(group)
            if merged:
                # Create mapping between items and tracks
                mapping, extra_items, extra_tracks = match.assign_items(items, merged.tracks)

                # Calculate distance
                dist = match.distance(items, merged, mapping)

                matches.append(hooks.AlbumMatch(dist, merged, mapping, extra_items, extra_tracks))

        return matches
