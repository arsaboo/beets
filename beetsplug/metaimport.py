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
            print_(colorize('text_highlight', '\nProcessing album:'))
            print_(colorize('text', f'  {albumartist} - {album_name}'))
            print_(colorize('text', f'  {len(items)} tracks'))

            # Search using each configured source
            matches = []
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
                            matches.append((source, album_info))
                            self._log.debug('Found metadata from {}', source)
                except Exception as e:
                    self._log.warning('Error getting metadata from {}: {}',
                                    source, str(e))

            if matches:
                # Show matches from each source
                print_()
                for i, (source, album_info) in enumerate(matches, 1):
                    print_(colorize('text_highlight', f'\nMatch [{i}] from {source}:'))
                    print_(colorize('text', '=' * 80))
                    print_(colorize('text', f'Album: {album_info.album}'))
                    print_(colorize('text', f'Artist: {album_info.artist}'))
                    if album_info.year:
                        print_(colorize('text', f'Year: {album_info.year}'))
                    if album_info.label:
                        print_(colorize('text', f'Label: {album_info.label}'))
                    print_()
                    print_(colorize('text', 'Tracks:'))
                    for track in album_info.tracks:
                        print_(colorize('text', f'  {track.index}. {track.title} - {track.artist}'))

                # Let user choose which match to use
                options = []
                for i, (source, _) in enumerate(matches, 1):
                    options.append(f'Use match {i} ({source})')
                options.extend(['Skip', 'aBort'])

                choice = ui.input_options(
                    options,
                    default='s',
                    require=True
                )

                if choice == 'b':  # Abort
                    return
                elif choice != 's':  # Not skip
                    match_index = int(choice) - 1
                    source, album_info = matches[match_index]
                    self._apply_metadata(items, album_info)
                else:  # Skip
                    self._log.info('Skipped album: {} - {}', albumartist, album_name)
            else:
                self._log.info('No metadata found for album: {} - {}', albumartist, album_name)

    def _apply_metadata(self, items, album_info):
        """Apply metadata from the selected match."""
        exclude_fields = self.config['exclude_fields'].as_str_seq()

        print_()
        print_(colorize('text_highlight', 'Applying metadata changes:'))
        print_(colorize('text', '=' * 80))

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
                    print_()
                    print_(colorize('text', f'Track: {item.title}'))
                    print_(colorize('text', f'Path: {displayable_path(item.path)}'))
                    for field, value in changes.items():
                        old_value = getattr(item, field)
                        old_str = colorize('text_error', str(old_value))
                        new_str = colorize('text_highlight', str(value))
                        print_(colorize('text', f'  {field}: {old_str} -> {new_str}'))

                    # Apply changes
                    for field, value in changes.items():
                        setattr(item, field, value)
                    item.store()

                    try:
                        item.write()
                        print_(colorize('text_success', '  Success!'))
                    except Exception as e:
                        print_(colorize('text_error', f'  Error: {str(e)}'))
            else:
                self._log.warning('No matching track info found for: {}', item.title)
