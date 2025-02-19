from beets import plugins, ui
from beets.plugins import BeetsPlugin
from collections import defaultdict
import sys

class MetaImportPlugin(BeetsPlugin):
    def __init__(self):
        super().__init__()
        self._log.debug('Initializing MetaImport plugin...')

        # Default configuration
        self.config.add({
            'sources': ['spotify', 'deezer'],  # List of sources to query
            'write': True,  # Whether to write tags to files
        })

        # Initialize metadata source plugins
        self.meta_sources = {}
        configured_sources = self.config['sources'].as_str_seq()
        self._log.debug('Configured sources: {}', configured_sources)

        # Direct plugin mapping instead of using find_plugins()
        plugin_mapping = {
            'spotify': 'beetsplug.spotify',
            'deezer': 'beetsplug.deezer'
        }

        for source in configured_sources:
            self._log.debug('Loading source plugin: {}', source)

            try:
                if source in plugin_mapping:
                    # Import the module directly
                    module_name = plugin_mapping[source]
                    if module_name not in sys.modules:
                        __import__(module_name)

                    module = sys.modules[module_name]

                    # Get the plugin class (SpotifyPlugin or DeezerPlugin)
                    plugin_class = None
                    for name in dir(module):
                        if name.endswith('Plugin'):
                            plugin_class = getattr(module, name)
                            break

                    if plugin_class:
                        self._log.debug('Found plugin class: {}', plugin_class.__name__)
                        try:
                            plugin_instance = plugin_class()
                            if hasattr(plugin_instance, 'album_for_id'):
                                self.meta_sources[source] = plugin_instance
                                self._log.debug('Successfully registered {} plugin', source)
                            else:
                                self._log.warning('{} plugin does not support album lookup', source)
                        except Exception as e:
                            self._log.error('Error initializing {} plugin: {} ({})',
                                          source, str(e), type(e).__name__)
                    else:
                        self._log.warning('Could not find plugin class for {}', source)
                else:
                    self._log.warning('{} plugin not supported', source)
            except Exception as e:
                self._log.error('Error loading {} plugin: {} ({})',
                              source, str(e), type(e).__name__)

        self._log.debug('Loaded {} source plugins: {}',
                       len(self.meta_sources),
                       list(self.meta_sources.keys()))

        if not self.meta_sources:
            self._log.warning(
                'No metadata source plugins loaded. Plugin will be inactive.'
            )

    def commands(self):
        cmd = ui.Subcommand('metaimport',
            help='fetch track metadata from all configured sources')

        def func(lib, opts, args):
            self.fetch_metadata(lib, ui.decargs(args))

        cmd.func = func
        return [cmd]

    def fetch_metadata(self, lib, query):
        """Process library albums and fetch missing metadata from configured sources."""
        if not self.meta_sources:
            self._log.warning('No metadata source plugins available. Aborting.')
            return

        try:
            albums = lib.albums(query)
        except Exception as e:
            self._log.error('Error querying library: {}', e)
            return

        self._log.info(f'Processing {len(albums)} albums...')
        self._log.debug('Query: {}', query)

        write = self.config['write'].get(bool)
        self._log.debug('Write enabled: {}', write)

        for album in albums:
            self._log.info(f'Processing album: {album}')
            self._log.debug('Album details - Artist: {}, Album: {}, Items: {}',
                          album.albumartist, album.album, len(album.items()))

            for source_name, source_plugin in self.meta_sources.items():
                self._log.debug('Processing source: {}', source_name)

                # Check if source-specific ID exists
                id_field = f'{source_name}_album_id'
                current_id = getattr(album, id_field, None)
                self._log.debug('Checking for existing {} - Current value: {}',
                              id_field, current_id)

                if current_id:
                    self._log.debug(f'Already has {source_name} ID: {current_id}')
                    continue

                # Try to find album on the source
                self._log.debug('Searching {} for album - Artist: {}, Album: {}',
                              source_name, album.albumartist, album.album)
                try:
                    query_filters = {
                        'artist': album.albumartist,
                        'album': album.album,
                    }
                    self._log.debug('Search filters: {}', query_filters)

                    results = source_plugin._search_api(
                        query_type='album',
                        filters=query_filters,
                        keywords=album.album
                    )
                    self._log.debug('Search returned {} results', len(results) if results else 0)

                    if results and len(results) > 0:
                        album_data = results[0]
                        self._log.debug('Selected album data: {}', album_data.get('id'))

                        album_info = source_plugin.album_for_id(album_data.get('id'))
                        if album_info:
                            self._log.debug('Got album info from {}: {}',
                                          source_name, vars(album_info))

                            # Update album metadata
                            source_fields = [f for f in vars(album_info)
                                           if f.startswith(source_name)]
                            self._log.debug('Updating {} fields: {}',
                                          source_name, source_fields)

                            for field in source_fields:
                                old_value = getattr(album, field, None)
                                new_value = getattr(album_info, field)
                                if old_value != new_value:
                                    self._log.debug('Updating {} - Old: {}, New: {}',
                                                  field, old_value, new_value)
                                    setattr(album, field, new_value)

                            # Store album changes
                            album.store()
                            self._log.debug('Stored album changes')

                            # Update individual tracks
                            self._log.debug('Processing {} tracks', len(album_info.tracks))
                            tracks_by_index = defaultdict(list)
                            for track_info in album_info.tracks:
                                tracks_by_index[track_info.index].append(track_info)

                            for item in album.items():
                                self._log.debug('Processing track {} of {}',
                                              item.track, item.title)
                                if item.track in tracks_by_index:
                                    track_info = tracks_by_index[item.track][0]
                                    track_fields = [f for f in vars(track_info)
                                                  if f.startswith(source_name)]
                                    self._log.debug('Updating track fields: {}',
                                                  track_fields)

                                    for field in track_fields:
                                        old_value = getattr(item, field, None)
                                        new_value = getattr(track_info, field)
                                        if old_value != new_value:
                                            self._log.debug('Updating {} - Old: {}, New: {}',
                                                          field, old_value, new_value)
                                            setattr(item, field, new_value)

                                    item.store()
                                    if write:
                                        self._log.debug('Writing changes to file: {}',
                                                      item.path)
                                        item.try_write()
                    else:
                        self._log.debug('No matches found on {} for album: {}',
                                      source_name, album.album)

                except Exception as e:
                    self._log.warning('Error querying {}: {} ({})',
                                    source_name, str(e), type(e).__name__)
                    continue

        self._log.info('Metadata import completed')
