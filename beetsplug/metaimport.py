from importlib import import_module

from beets import autotag, config, importer, plugins, ui
from beets.autotag import Distance, Proposal, Recommendation, hooks
from beets.dbcore import types
from beets.plugins import BeetsPlugin
from beets.ui import colorize, print_
from beets.ui.commands import import_cmd  # Import the import command
from beets.ui.commands import (PromptChoice, abort_action, choose_candidate,
                               manual_id, manual_search)
from beets.util import displayable_path


class MetaImportPlugin(BeetsPlugin):
    def __init__(self):
        super().__init__()

        self.config.add({
            'sources': [],  # List of metadata sources
            'timid': False,  # Always ask for confirmation
        })

        # Initialize source plugins and ID fields
        self.sources = []
        self.source_plugins = {}
        self.SOURCE_ID_FIELDS = {}
        self.album_types = {}
        self.opts = None  # Store command options

        if self.config["sources"].exists():
            configured_sources = self.config["sources"].as_str_seq()
            if configured_sources:
                self._log.debug(f"Configured sources: {configured_sources}")
                for source in configured_sources:
                    self._init_source(source)

    def _init_source(self, source):
        """Initialize a single source plugin and its fields."""
        try:
            # Try to import the plugin module
            module = import_module(f'beetsplug.{source}')

            # Get the plugin class (assuming it follows the naming convention)
            plugin_class_name = f"{source.capitalize()}Plugin"
            if hasattr(module, plugin_class_name):
                plugin_class = getattr(module, plugin_class_name)
                plugin = plugin_class()

                # Get field name from plugin
                field_name = f"{source}_album_id"  # Default format
                if hasattr(plugin, 'album_id_field'):
                    field_name = plugin.album_id_field

                # Add to our mappings
                self.source_plugins[source] = plugin
                self.SOURCE_ID_FIELDS[source] = field_name
                self.album_types[field_name] = types.STRING
                self.sources.append(source)

                self._log.debug(f"Successfully loaded source plugin: {source}")
                self._log.debug(f"Using field name: {field_name}")
            else:
                self._log.warning(
                    f"Plugin {source} found but {plugin_class_name} class not found"
                )
        except ImportError as e:
            self._log.warning(f"Could not import plugin {source}: {str(e)}")
        except Exception as e:
            self._log.warning(f"Failed to initialize source {source}: {str(e)}")

    def commands(self):
        cmd = ui.Subcommand(
            "metaimport",
            help="collect identifiers from configured sources"
        )
        cmd.parser.add_option(
            '-t',
            '--timid',
            dest='timid',
            action='store_true',
            help='always show candidates, even for exact matches',
        )
        cmd.func = self._command
        return [cmd]

    def _score_match(self, album_info, artist, album):
        """Calculate a match score between input metadata and album info."""
        dist = Distance()

        # Compare artists - use beets' string distance
        if album_info.artist and artist:
            dist.add_string("artist", artist, album_info.artist)

        # Compare album titles
        if album_info.album and album:
            dist.add_string("album", album, album_info.album)

        # Additional scoring based on other metadata
        if album_info.year:
            dist.add("year", 0.0)  # No penalty for year mismatch for now

        # Return 1.0 - distance to get a score where 1.0 is perfect
        return 1.0 - dist.distance

    def _collect_identifiers(self, artist, album, album_obj):
        """Collect identifiers from all configured sources."""
        identifiers = {}

        for source in self.sources:
            self._log.debug(f"Processing source: {source}")
            try:
                field_name = self.SOURCE_ID_FIELDS[source]
                existing_id = getattr(album_obj, field_name, None)

                if existing_id:
                    self._log.debug(f'Found existing {field_name}: {existing_id}')
                    if self.config['timid'] or self.opts.timid:
                        print_(f"\nFound existing {source} match:")
                        print_(f"  Artist: {artist}")
                        print_(f"  Album: {album}")
                        print_(f"  ID: {existing_id}")
                        if ui.input_yn('Use this match? (Y/n)', True):
                            identifiers[field_name] = existing_id
                    else:
                        identifiers[field_name] = existing_id

                else:
                    # Search for new matches
                    self._log.debug(f"Searching {source}...")
                    plugin = self.source_plugins[source]
                    results = plugin._search_api(
                        "album", keywords=album, filters={"artist": artist}
                    )

                    if results and len(results) > 0:
                        album_info = plugin.album_for_id(str(results[0]["id"]))
                        if album_info:
                            print_(f"\nFound {source} match:")
                            print_(f"  Artist: {album_info.artist}")
                            print_(f"  Album: {album_info.album}")
                            if not (self.config['timid'] or self.opts.timid) or \
                               ui.input_yn(f'Use this {source} match? (Y/n)', True):
                                identifiers[field_name] = album_info.album_id

            except Exception as e:
                self._log.warning(f"Error searching {source}: {e}")
                # Continue with next source instead of exiting
                continue

        return identifiers

    def _show_match_details(self, match, source):
        """Show detailed information about a match."""
        print_(f"\nDetails for {source} match:")
        if match.info.tracks:
            for track in match.info.tracks:
                print_(
                    f"     * (#{track.index}) {track.title}"
                    f" ({track.length/60:.2f})"
                )

        return True

    def _command(self, lib, opts, args):
        """Main command implementation."""
        # Store options for use in other methods
        self.opts = opts

        if not self.sources:
            self._log.warning("No valid metadata sources configured")
            return

        items = lib.items(ui.decargs(args))
        if not items:
            self._log.warning("No items matched your query")
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
            # Show full path to album
            print_()  # Blank line
            path = displayable_path(items[0].get_album().path)
            print_(ui.colorize('text_highlight', path))
            print_(ui.colorize('text', f' ({len(items)} items)'))

            # Collect identifiers
            identifiers = self._collect_identifiers(albumartist, album_name, items[0].get_album())

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
