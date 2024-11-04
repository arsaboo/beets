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
            # Use beets' autotagger to get album info
            artist, album, prop = autotag.tag_album(items)

            if prop.candidates:
                # Show the candidates using beets' standard format
                self._show_candidates(items, artist, album, prop.candidates)

                # Get user choice
                choice = ui.input_options(
                    ('Apply', 'Skip', 'aBort'),
                    default='a',
                    require=True
                )

                if choice == 'a':  # Apply
                    match = prop.candidates[0]
                    self._apply_candidate(items, match)
                elif choice == 'b':  # Abort
                    return
                else:  # Skip
                    self._log.info('Skipped album: {} - {}', albumartist, album_name)
            else:
                self._log.info('No metadata found for album: {} - {}', albumartist, album_name)

    def _show_candidates(self, items, artist, album, candidates):
        """Show metadata matches using beets' standard format."""
        print_()
        print_(colorize('text_highlight', 'Finding tags for album:'))
        print_(colorize('text', '  {0} - {1}'.format(artist, album)))
        print_(colorize('text', '  {0} tracks'.format(len(items))))
        print_()

        # Show best match
        match = candidates[0]
        print_(colorize('text_highlight', 'Album Info:'))
        print_(colorize('text', '=' * 80))
        print_(colorize('text', 'Album: {0}'.format(match.info.album)))
        print_(colorize('text', 'Artist: {0}'.format(match.info.artist)))
        if match.info.year:
            print_(colorize('text', 'Year: {0}'.format(match.info.year)))
        if match.info.label:
            print_(colorize('text', 'Label: {0}'.format(match.info.label)))
        print_()
        print_(colorize('text_highlight', 'Tracks:'))
        for track_info in match.info.tracks:
            print_(colorize('text', '  {0}. {1} - {2}'.format(
                track_info.index, track_info.title, track_info.artist
            )))

    def _apply_candidate(self, items, match):
        """Apply metadata from the selected match."""
        print_()
        print_(colorize('text_highlight', 'Applying metadata changes:'))
        print_(colorize('text', '=' * 80))

        # Apply metadata using beets' autotagger
        autotag.apply_metadata(match.info, match.mapping)

        # Show changes and save
        for item in items:
            print_()
            print_(colorize('text', f'Track: {item.title}'))
            print_(colorize('text', f'Path: {displayable_path(item.path)}'))

            try:
                item.store()
                item.write()
                print_(colorize('text_success', '  Success!'))
            except Exception as e:
                print_(colorize('text_error', f'  Error: {str(e)}'))
