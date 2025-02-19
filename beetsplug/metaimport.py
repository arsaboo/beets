from beets import plugins
from beets import ui
from beets.plugins import BeetsPlugin

class MetaImportPlugin(BeetsPlugin):
    def __init__(self):
        super().__init__()

        # Default configuration
        self.config.add({
            'sources': ['spotify', 'deezer'],  # List of sources to query
            'write': True,  # Whether to write tags to files
        })

        # Get configured metadata source plugins
        self.meta_sources = {}
        for source in self.config['sources'].as_str_seq():
            if source in plugins.find_plugins():
                plugin = plugins.find_plugins()[source]
                if hasattr(plugin, 'track_for_id'):
                    self.meta_sources[source] = plugin
                else:
                    self._log.warning(f'{source} plugin does not support track lookup')
            else:
                self._log.warning(f'{source} plugin not found')

    def commands(self):
        cmd = ui.Subcommand('metaimport',
            help='fetch track metadata from all configured sources')

        def func(lib, opts, args):
            self.fetch_metadata(lib, ui.decargs(args))

        cmd.func = func
        return [cmd]

    def fetch_metadata(self, lib, query):
        """Process library items and fetch missing metadata from configured sources."""
        items = lib.items(query)
        self._log.info(f'Processing {len(items)} tracks...')

        write = self.config['write'].get(bool)

        for item in items:
            self._log.info(f'Processing track: {item}')

            for source_name, source_plugin in self.meta_sources.items():
                # Check if source-specific ID exists
                id_field = f'{source_name}_track_id'

                if hasattr(item, id_field) and getattr(item, id_field):
                    self._log.debug(f'Already has {source_name} ID: {getattr(item, id_field)}')
                    continue

                # Try to find track on the source
                self._log.debug(f'Searching {source_name} for track...')
                try:
                    # Use source plugin's search capabilities
                    query_filters = {
                        'artist': item.albumartist,
                        'album': item.album,
                    }
                    results = source_plugin._search_api(
                        query_type='track',
                        filters=query_filters,
                        keywords=item.title
                    )

                    if results and len(results) > 0:
                        # Take the first match
                        track_data = results[0]
                        # Use the source's track_for_id to get full metadata
                        track_info = source_plugin.track_for_id(
                            track_id=None,
                            track_data=track_data
                        )

                        if track_info:
                            # Update item with source-specific fields
                            self._log.info(f'Found match on {source_name}')
                            for field in track_info.keys():
                                if field.startswith(source_name):
                                    setattr(item, field, track_info[field])

                            # Store changes
                            item.store()
                            if write:
                                item.try_write()
                    else:
                        self._log.debug(f'No matches found on {source_name}')

                except Exception as e:
                    self._log.warning(f'Error querying {source_name}: {str(e)}')
                    continue

        self._log.info('Metadata import completed')
