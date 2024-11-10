from beets import autotag, config, ui, plugins, importer
from beets.plugins import BeetsPlugin
from beets.ui import print_, colorize
from beets.dbcore import types
from beets.autotag import hooks, Proposal, Recommendation, Distance
from beets.ui.commands import (
    choose_candidate,
    PromptChoice,
    manual_search,
    manual_id,
    abort_action,
)
from beets.util import displayable_path

# Import supported plugins directly
from beetsplug.spotify import SpotifyPlugin
from beetsplug.deezer import DeezerPlugin


class MetaImportPlugin(BeetsPlugin):
    # Map sources to their corresponding field names
    SOURCE_ID_FIELDS = {"spotify": "spotify_album_id", "deezer": "deezer_album_id"}

    SUPPORTED_SOURCES = {"spotify": SpotifyPlugin, "deezer": DeezerPlugin}

    # Declare fields that will be added to the database
    album_types = {
        "spotify_album_id": types.STRING,
        "deezer_album_id": types.STRING,
    }

    def __init__(self):
        super().__init__()

        self.config.add({
            'sources': [],  # List of metadata sources
            'timid': config['import']['timid'].get(),  # Inherit from import config
        })

        # Initialize source plugins
        self.sources = []
        self.source_plugins = {}

        if self.config["sources"].exists():
            configured_sources = self.config["sources"].as_str_seq()
            if configured_sources:
                self._log.debug(f"Configured sources: {configured_sources}")
                for source in configured_sources:
                    if source not in self.SUPPORTED_SOURCES:
                        self._log.warning(f"Unsupported source: {source}")
                        continue
                    self._init_source(source)

    def _init_source(self, source):
        """Initialize a single source plugin."""
        try:
            plugin_class = self.SUPPORTED_SOURCES[source]
            plugin = plugin_class()
            self.source_plugins[source] = plugin
            self.sources.append(source)
            self._log.debug(f"Successfully loaded source plugin: {source}")
        except Exception as e:
            self._log.warning(f"Failed to initialize source {source}: {str(e)}")

    def commands(self):
        cmd = ui.Subcommand(
            "metaimport",
            help="collect identifiers from configured sources"
        )
        # Add timid flag
        cmd.parser.add_option(
            '-t',
            '--timid',
            dest='timid',
            action='store_true',
            default=False,
            help='always confirm even perfect matches'
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

        # Get global timid setting and command-line override
        is_timid = (config["import"]["timid"].get() or
                   self.config["timid"].get())

        for source in self.sources:
            try:
                # Check if identifier already exists
                field_name = self.SOURCE_ID_FIELDS[source]
                existing_id = getattr(album_obj, field_name, None)
                if existing_id:
                    self._log.debug(f'Using existing {field_name}: {existing_id}')
                    identifiers[field_name] = existing_id
                    continue

                plugin = self.source_plugins[source]

                # Main matching loop - allows retrying with manual search/ID
                while True:
                    results = plugin._search_api(
                        "album", keywords=album, filters={"artist": artist}
                    )

                    if results and len(results) > 0:
                        candidates = []
                        for result in results:
                            album_info = plugin.album_for_id(str(result["id"]))
                            if album_info:
                                match = autotag.AlbumMatch(
                                    distance=hooks.Distance(),
                                    info=album_info,
                                    mapping={},
                                    extra_items=[],
                                    extra_tracks=[],
                                )
                                score = self._score_match(album_info, artist, album)
                                match.distance.add("album", 1.0 - score)
                                candidates.append(match)

                        if candidates:
                            candidates.sort(key=lambda c: c.distance)
                            best_score = 1.0 - candidates[0].distance
                            rec = Recommendation.none
                            if best_score > 0.8:
                                rec = Recommendation.strong
                            elif best_score > 0.5:
                                rec = Recommendation.medium

                            # Present candidates
                            match = choose_candidate(
                                candidates=candidates,
                                singleton=False,
                                rec=rec,
                                cur_artist=artist,
                                cur_album=album,
                                itemcount=len(album_info.tracks) if album_info else 0,
                                choices=[
                                    PromptChoice("s", "Skip", importer.action.SKIP),
                                    PromptChoice(
                                        "u", "Use as-is", importer.action.ASIS
                                    ),
                                    PromptChoice(
                                        "t", "as Tracks", importer.action.TRACKS
                                    ),
                                    PromptChoice(
                                        "g", "Group albums", importer.action.ALBUMS
                                    ),
                                    PromptChoice("e", "Enter search", manual_search),
                                    PromptChoice("i", "enter Id", manual_id),
                                    PromptChoice("b", "aBort", abort_action),
                                ],
                            )

                            # Handle choice callbacks
                            if isinstance(match, PromptChoice):
                                if match.callback == manual_id:
                                    # Get ID from user
                                    search_id = ui.input_("Enter ID:").strip()
                                    album_info = plugin.album_for_id(search_id)
                                    if album_info:
                                        field_name = self.SOURCE_ID_FIELDS[source]
                                        identifiers[field_name] = album_info.album_id
                                        print_(f"\nDetails for {source} match:")
                                        if album_info.tracks:
                                            for track in album_info.tracks:
                                                print_(
                                                    f"     * (#{track.index}) {track.title}"
                                                    f" ({track.length/60:.2f})"
                                                )
                                    break
                                elif match.callback == manual_search:
                                    # Get search terms from user
                                    artist = ui.input_("Artist:").strip()
                                    album = ui.input_("Album:").strip()
                                    continue  # Retry search with new terms
                                elif match.callback == abort_action:
                                    raise importer.ImportAbortError()
                                else:
                                    break  # Skip or other action
                            elif match and not isinstance(match, str):
                                field_name = self.SOURCE_ID_FIELDS[source]

                                # Show match details before asking for confirmation
                                self._show_match_details(match, source)

                                # Always require confirmation in timid mode
                                if not is_timid and best_score == 1.0:
                                    identifiers[field_name] = match.info.album_id
                                    self._log.debug(
                                        f'Perfect match found for {source}, '
                                        f'automatically applying'
                                    )
                                else:
                                    # Ask for confirmation
                                    if ui.input_yn('Apply match (y/n)?', True):
                                        identifiers[field_name] = match.info.album_id

                            break  # Done with this source
                    break  # No results found

            except Exception as e:
                self._log.warning(f"Error getting {source} identifier: {str(e)}")

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

    def _command(self, lib, opts, args):
        """Main command implementation."""
        # Override config from command line
        self.config['timid'].set(opts.timid or self.config['timid'].get())

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
