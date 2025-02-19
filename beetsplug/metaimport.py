from beets import plugins, ui
from beets.plugins import BeetsPlugin, MetadataSourcePlugin
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

        # Plugin class mapping
        plugin_classes = {
            'spotify': ('beetsplug.spotify', 'SpotifyPlugin'),
            'deezer': ('beetsplug.deezer', 'DeezerPlugin')
        }

        for source in configured_sources:
            self._log.debug('Loading source plugin: {}', source)

            try:
                if source in plugin_classes:
                    module_name, class_name = plugin_classes[source]
                    if module_name not in sys.modules:
                        __import__(module_name)

                    module = sys.modules[module_name]
                    plugin_class = getattr(module, class_name)

                    if plugin_class and issubclass(plugin_class, MetadataSourcePlugin):
                        self._log.debug('Found plugin class: {}', plugin_class.__name__)
                        try:
                            plugin_instance = plugin_class()
                            self.meta_sources[source] = plugin_instance
                            self._log.debug('Successfully registered {} plugin', source)
                        except Exception as e:
                            self._log.error('Error initializing {} plugin: {} ({})',
                                          source, str(e), type(e).__name__)
                    else:
                        self._log.warning('{} plugin is not a MetadataSourcePlugin', source)
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

    def _get_search_function(self, plugin):
        """Get the appropriate search function for the given plugin."""
        if isinstance(plugin, MetadataSourcePlugin):
            if hasattr(plugin, '_search_api'):
                self._log.debug('Using _search_api method')
                return plugin._search_api
            elif hasattr(plugin, 'search'):
                self._log.debug('Using search method')
                return plugin.search
            elif hasattr(plugin, 'search_album'):
                self._log.debug('Using search_album method')
                return plugin.search_album

        self._log.debug('No valid search method found for plugin')
        return None

    def _execute_search(self, source_name, search_function, album):
        """Execute the search using the appropriate method for each source."""
        try:
            if source_name == 'deezer':
                # Just search by album name for better results
                search_query = album.album
                self._log.debug('Deezer search query: {}', search_query)

                results = search_function('album', None, search_query)
                if not results:
                    return []

                # Filter results by artist match
                filtered_results = []
                album_artists = {a.strip().lower() for a in album.albumartist.split(',')}

                for result in results:
                    if not isinstance(result, dict):
                        continue

                    artist_name = result.get('artist', {}).get('name', '').lower()
                    if any(artist.lower() in artist_name or artist_name in artist.lower()
                          for artist in album_artists):
                        filtered_results.append(result)

                self._log.debug('Found {} matching results after filtering', len(filtered_results))
                return filtered_results

            else:
                # Default search method (e.g. for Spotify)
                return search_function(
                    query_type='album',
                    filters={'artist': album.albumartist, 'album': album.album},
                    keywords=album.album
                )

        except Exception as e:
            self._log.error('Search failed: {} ({})', str(e), type(e).__name__)
            return None

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
                    search_function = self._get_search_function(source_plugin)
                    if not search_function:
                        self._log.warning(f'No search function found for {source_name}')
                        continue

                    results = self._execute_search(source_name, search_function, album)
                    if not results:
                        self._log.debug('No results found for {} on {}', album.album, source_name)
                        continue

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
