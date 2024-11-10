from beets import autotag, config, ui, plugins
from beets.plugins import BeetsPlugin
from beets.ui import print_, colorize
from beets.dbcore import types
from beets.autotag import hooks, Proposal, Recommendation, Distance

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

    def _score_match(self, album_info, artist, album):
        """Calculate a match score between input metadata and album info."""
        dist = Distance()

        # Compare artists - use beets' string distance
        if album_info.artist and artist:
            dist.add_string('artist', artist, album_info.artist)

        # Compare album titles
        if album_info.album and album:
            dist.add_string('album', album, album_info.album)

        # Additional scoring based on other metadata
        if album_info.year:
            dist.add('year', 0.0)  # No penalty for year mismatch for now

        # Return 1.0 - distance to get a score where 1.0 is perfect
        return 1.0 - dist.distance

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
                        album_info = plugin.album_for_id(str(result['id']))
                        if album_info:
                            # Score the match
                            score = self._score_match(album_info, artist, album)

                            # Create distance object for display
                            dist = Distance()
                            dist.add('album', 1.0 - score)

                            candidates.append(autotag.AlbumMatch(
                                distance=dist,
                                info=album_info,
                                mapping={},
                                extra_items=[],
                                extra_tracks=[],
                            ))

                    if candidates:
                        # Sort candidates by score
                        candidates.sort(key=lambda c: c.distance)

                        # Determine recommendation based on score
                        rec = Recommendation.none
                        best_score = 1.0 - candidates[0].distance
                        if best_score > 0.8:
                            rec = Recommendation.strong
                        elif best_score > 0.5:
                            rec = Recommendation.medium

                        # Create proposal
                        prop = Proposal(candidates, rec)

                        # Present candidates using beets' UI
                        match = choose_candidate(
                            prop.candidates,
                            False,
                            prop.recommendation,
                            artist,
                            album,
                            itemcount=len(candidates),
                            choices=[],
                        )

                        if match:
                            field_name = self.SOURCE_ID_FIELDS[source]
                            identifiers[field_name] = match.info.album_id
                            self._log.debug(f'Found {field_name}: {match.info.album_id}')

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
