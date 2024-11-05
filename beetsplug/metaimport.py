from beets import config, ui, plugins, autotag
from beets.plugins import BeetsPlugin
from beets.ui import print_, colorize
from beets.util import displayable_path
from beets.autotag import hooks

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

    def candidates(self, items, artist, album, va_likely, extra_tags=None):
        """Hook for providing metadata matches during import."""
        matches = []

        # Track which IDs we've already processed to avoid duplicates
        seen_ids = set()

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

                        # Skip if we've already processed this ID
                        if album_id in seen_ids:
                            continue
                        seen_ids.add(album_id)

                        album_info = plugin.album_for_id(album_id)
                        if album_info:
                            # Create a new hooks.AlbumInfo object with proper source attribution
                            info = hooks.AlbumInfo(
                                album=album_info.album,
                                album_id=album_info.album_id,
                                artist=album_info.artist,
                                artist_id=album_info.artist_id,
                                tracks=album_info.tracks,
                                asin=album_info.asin,
                                albumtype=album_info.albumtype,
                                va=album_info.va,
                                year=album_info.year,
                                month=album_info.month,
                                day=album_info.day,
                                label=album_info.label,
                                mediums=album_info.mediums,
                                artist_sort=album_info.artist_sort,
                                releasegroup_id=album_info.releasegroup_id,
                                catalognum=album_info.catalognum,
                                script=album_info.script,
                                language=album_info.language,
                                country=album_info.country,
                                style=album_info.style,
                                genre=album_info.genre,
                                albumstatus=album_info.albumstatus,
                                media=album_info.media,
                                albumdisambig=album_info.albumdisambig,
                                artist_credit=album_info.artist_credit,
                                data_source=source,
                                data_url=getattr(album_info, 'data_url', None)
                            )
                            matches.append(info)
                            self._log.debug(f'Found metadata from {source}')
            except Exception as e:
                self._log.warning('Error getting metadata from {}: {}',
                                source, str(e))

        return matches
