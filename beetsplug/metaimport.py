from beets import autotag, config, ui, plugins
from beets.plugins import BeetsPlugin
from beets.ui import print_, colorize
from beets.dbcore import types
from beets.autotag import hooks

# Import supported plugins directly
from beetsplug.spotify import SpotifyPlugin
from beetsplug.deezer import DeezerPlugin

class MetaImportPlugin(BeetsPlugin):
    # Map sources to their corresponding field names
    SOURCE_ID_FIELDS = {
        'spotify': 'spotify_album_id',
        'deezer': 'deezer_album_id'
    }

    SUPPORTED_SOURCES = {
        'spotify': SpotifyPlugin,
        'deezer': DeezerPlugin
    }

    # Declare fields that will be added to the database
    album_types = {
        'spotify_album_id': types.STRING,
        'deezer_album_id': types.STRING,
    }

    def __init__(self):
        super().__init__()

        self.config.add({
            'sources': [],  # List of metadata sources
        })

        # Initialize source plugins
        self.sources = []
        self.source_plugins = {}

        if self.config['sources'].exists():
            configured_sources = self.config['sources'].as_str_seq()
            if configured_sources:
                self._log.debug(f'Configured sources: {configured_sources}')
                for source in configured_sources:
                    if source not in self.SUPPORTED_SOURCES:
                        self._log.warning(f'Unsupported source: {source}')
                        continue
                    self._init_source(source)

    def _init_source(self, source):
        """Initialize a single source plugin."""
        try:
            plugin_class = self.SUPPORTED_SOURCES[source]
            plugin = plugin_class()
            self.source_plugins[source] = plugin
            self.sources.append(source)
            self._log.debug(f'Successfully loaded source plugin: {source}')
        except Exception as e:
            self._log.warning(f'Failed to initialize source {source}: {str(e)}')

    def commands(self):
        cmd = ui.Subcommand(
            'metaimport',
            help='collect identifiers from configured sources'
        )
        cmd.func = self._command
        return [cmd]

    def _collect_identifiers(self, artist, album):
        """Collect identifiers from all configured sources."""
        identifiers = {}

        for source in self.sources:
            try:
                plugin = self.source_plugins[source]
                results = plugin._search_api('album', keywords=album, filters={'artist': artist})

                if results and len(results) > 0:
                    candidates = []
                    for result in results:
                        # Get full album info from the source
                        album_info = plugin.album_for_id(str(result['id']))
                        if album_info:
                            candidates.append(album_info)

                    if candidates:
                        # Use beets' built-in album distance calculation
                        dist_album = candidates[0]
                        min_dist = float('inf')
                        for candidate in candidates:
                            # Calculate similarity using title and artist
                            dist = hooks.string_dist(album, candidate.album)
                            dist += hooks.string_dist(artist, candidate.artist)
                            if dist < min_dist:
                                min_dist = dist
                                dist_album = candidate

                        # Ask for user confirmation if match isn't exact
                        if min_dist > 0.0:
                            # Show all candidates
                            print_(colorize('text', f'\nCandidates from {source}:'))
                            for i, candidate in enumerate(candidates, 1):
                                print_(colorize('text',
                                    f'  {i}. {candidate.artist} - {candidate.album}'))

                            # Ask user to choose
                            sel = ui.input_options(
                                ('Choose candidate (n match, s skip): '),
                                ('n', 's') + tuple(str(i) for i in range(1, len(candidates) + 1))
                            )

                            if sel == 'n':
                                continue
                            elif sel == 's':
                                break
                            else:
                                dist_album = candidates[int(sel) - 1]

                        # Store the identifier
                        field_name = self.SOURCE_ID_FIELDS[source]
                        identifiers[field_name] = dist_album.album_id
                        self._log.debug(f'Found {field_name}: {dist_album.album_id}')

            except Exception as e:
                self._log.warning(f'Error getting {source} identifier: {str(e)}')

        return identifiers

    def _command(self, lib, opts, args):
        """Main command implementation."""
        if not self.sources:
            self._log.warning('No valid metadata sources configured')
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

        # Process each album
        for (albumartist, album_name), items in albums.items():
            print_(colorize('text_highlight', '\nProcessing album:'))
            print_(colorize('text', f'  {albumartist} - {album_name}'))

            # Collect identifiers
            identifiers = self._collect_identifiers(albumartist, album_name)

            if identifiers:
                # Update the first item's album with the identifiers
                album = items[0].get_album()
                if album:
                    for field, value in identifiers.items():
                        setattr(album, field, value)
                    album.store()
                    print_(colorize('text_success', '  ✓ Identifiers stored'))
                    for source, id_value in identifiers.items():
                        print_(colorize('text', f'    {source}: {id_value}'))
            else:
                print_(colorize('text_warning', '  ✗ No identifiers found'))
