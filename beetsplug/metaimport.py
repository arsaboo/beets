from beets import config, ui, plugins, autotag
from beets.plugins import BeetsPlugin
from beets.ui import print_
from beets.util import displayable_path

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

        self._import_albums_metadata(albums)

    def _import_albums_metadata(self, albums):
        """Import metadata for albums from all configured sources."""
        for (albumartist, album_name), items in albums.items():
            self._log.info('Processing album: {} - {}', albumartist, album_name)

            # Collect metadata from all sources
            album_infos = []
            for source in self.sources:
                try:
                    plugin = self.source_plugins[source]
                    # Search for the album using plugin's search capabilities
                    results = plugin._search_api('album', keywords=album_name,
                                              filters={'artist': albumartist})
                    if results:
                        # Get the first result's ID and fetch full album info
                        album_id = results[0]['id']
                        album_info = plugin.album_for_id(album_id)
                        if album_info:
                            album_infos.append(album_info)
                            self._log.debug('Found metadata from {}', source)
                except Exception as e:
                    self._log.warning('Error getting metadata from {}: {}',
                                    source, str(e))

            if album_infos:
                # Show the first album info (highest priority source)
                album_info = album_infos[0]
                self._show_album_info(album_info)

                # Get user choice
                if ui.input_yn('Apply this metadata?'):
                    self._apply_metadata(items, album_info)
                else:
                    self._log.info('Skipped album: {} - {}', albumartist, album_name)
            else:
                self._log.info('No metadata found for album: {} - {}', albumartist, album_name)

    def _show_album_info(self, album_info):
        """Display album metadata."""
        print_('\nAlbum Info:')
        print_('=' * 80)
        print_(f'Album: {album_info.album}')
        print_(f'Artist: {album_info.artist}')
        if album_info.year:
            print_(f'Year: {album_info.year}')
        if album_info.label:
            print_(f'Label: {album_info.label}')
        print_('\nTracks:')
        for track in album_info.tracks:
            print_(f'  {track.index}. {track.title} - {track.artist}')

    def _apply_metadata(self, items, album_info):
        """Apply metadata from album info to items."""
        exclude_fields = self.config['exclude_fields'].as_str_seq()

        print_('\nApplying metadata changes:')
        print_('=' * 80)

        for item in items:
            # Find matching track info
            track_info = None
            for track in album_info.tracks:
                if track.index == item.track:
                    track_info = track
                    break

            if track_info:
                changes = {}
                # Apply album-level metadata
                for field in ['album', 'albumartist', 'year', 'month', 'day', 'label']:
                    if hasattr(album_info, field) and field not in exclude_fields:
                        value = getattr(album_info, field)
                        if value and value != getattr(item, field):
                            changes[field] = value

                # Apply track-level metadata
                for field in ['title', 'artist', 'length', 'medium', 'medium_index']:
                    if hasattr(track_info, field) and field not in exclude_fields:
                        value = getattr(track_info, field)
                        if value and value != getattr(item, field):
                            changes[field] = value

                if changes:
                    # Show changes
                    print_(f'\nTrack: {item.title}')
                    print_(f'Path: {displayable_path(item.path)}')
                    for field, value in changes.items():
                        print_(f'  {field}: {getattr(item, field)} -> {value}')

                    # Apply changes
                    for field, value in changes.items():
                        setattr(item, field, value)
                    item.store()

                    try:
                        item.write()
                        print_('  Success!')
                    except Exception as e:
                        print_(f'  Error: {str(e)}')
            else:
                self._log.warning('No matching track info found for: {}', item.title)
