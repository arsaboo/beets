from beets import config, ui, plugins, autotag
from beets.plugins import BeetsPlugin
from beets.ui import print_, colorize
from beets.util import displayable_path
from beets.autotag import hooks, match
from collections import defaultdict
import re

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
            'match_threshold': 0.70,  # Distance threshold for considering albums the same
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

        # Register as a metadata provider
        self.register_listener('album_candidates', self.candidates)

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

        # Use beets' built-in import process
        for (albumartist, album_name), items in albums.items():
            print_(colorize('text_highlight', '\nProcessing album:'))
            print_(colorize('text', f'  {albumartist} - {album_name}'))
            print_(colorize('text', f'  {len(items)} tracks'))

            # Let beets handle the import process
            # The candidates hook will provide matches from our sources
            autotag.tag_album(items)

    def _normalize_string(self, s):
        """Normalize a string for comparison."""
        if not s:
            return ''
        # Convert to lowercase
        s = s.lower()
        # Replace special characters with spaces
        s = re.sub(r'[^\w\s]', ' ', s)
        # Replace multiple spaces with single space
        s = re.sub(r'\s+', ' ', s)
        # Remove common words and punctuation
        s = re.sub(r'\b(the|a|an|and|or|of|feat|featuring|original|motion|picture|soundtrack|ost)\b', '', s)
        # Remove parentheses and their contents
        s = re.sub(r'\([^)]*\)', '', s)
        # Remove common suffixes
        s = re.sub(r'(original motion picture soundtrack|ost|soundtrack)$', '', s)
        # Strip and normalize whitespace
        return ' '.join(s.split())

    def _strings_match(self, s1, s2, threshold=0.8):
        """Compare two strings with fuzzy matching."""
        if not s1 or not s2:
            return False

        # Normalize strings
        n1 = self._normalize_string(s1)
        n2 = self._normalize_string(s2)

        # Check if one is contained in the other
        if n1 in n2 or n2 in n1:
            return True

        # Check if they're similar enough
        words1 = set(n1.split())
        words2 = set(n2.split())
        if not words1 or not words2:
            return False

        common_words = words1.intersection(words2)
        similarity = len(common_words) / max(len(words1), len(words2))

        return similarity >= threshold

    def _albums_match(self, album1, album2):
        """Check if two albums are likely the same based on metadata."""
        try:
            # Basic metadata comparison
            if not (album1 and album2 and album1.tracks and album2.tracks):
                return False

            # Compare album names with fuzzy matching
            if not self._strings_match(album1.album, album2.album):
                self._log.debug(f'Album names do not match: {album1.album} vs {album2.album}')
                return False

            # Compare artists with fuzzy matching
            if not album1.va and not album2.va:
                if not self._strings_match(album1.artist, album2.artist):
                    self._log.debug(f'Artist names do not match: {album1.artist} vs {album2.artist}')
                    return False

            # Compare track counts
            if abs(len(album1.tracks) - len(album2.tracks)) > 2:
                self._log.debug(f'Track count difference too large: {len(album1.tracks)} vs {len(album2.tracks)}')
                return False

            # Create a mapping between tracks
            mapping, extra_items, extra_tracks = match.assign_items(
                album1.tracks, album2.tracks
            )

            # Calculate distance between albums
            dist = match.distance(album1.tracks, album2, mapping)

            # Log the distance
            self._log.debug(f'Album distance: {dist.distance} (threshold: {self.config["match_threshold"].as_number()})')

            # Return True if distance is below threshold
            return dist.distance <= self.config['match_threshold'].as_number()
        except Exception as e:
            self._log.debug(f'Error comparing albums: {str(e)}')
            return False

    def _merge_album_info(self, albums):
        """Merge metadata from multiple album matches."""
        if not albums:
            return None

        try:
            # Sort albums by completeness of metadata
            def metadata_completeness(album):
                fields = ['year', 'month', 'day', 'label', 'catalognum',
                         'country', 'media', 'albumdisambig', 'genre', 'style']
                completeness = sum(1 for f in fields if getattr(album, f))
                self._log.debug(f'Metadata completeness for {album.album} ({album.data_source}): {completeness}')
                return completeness

            albums.sort(key=metadata_completeness, reverse=True)
            base = albums[0]
            self._log.debug(f'Using base album from {base.data_source}: {base.album} by {base.artist}')

            # Create a new AlbumInfo object with merged data
            merged = hooks.AlbumInfo(
                album=base.album,
                album_id=base.album_id,
                artist=base.artist,
                artist_id=base.artist_id,
                tracks=base.tracks,
                asin=base.asin,
                albumtype=base.albumtype,
                va=base.va,
                year=base.year,
                month=base.month,
                day=base.day,
                label=base.label,
                mediums=base.mediums,
                artist_sort=base.artist_sort,
                releasegroup_id=base.releasegroup_id,
                catalognum=base.catalognum,
                script=base.script,
                language=base.language,
                country=base.country,
                style=base.style,
                genre=base.genre,
                albumstatus=base.albumstatus,
                media=base.media,
                albumdisambig=base.albumdisambig,
                artist_credit=base.artist_credit,
                data_source=f"merged({','.join(a.data_source for a in albums)})",
                data_url=base.data_url
            )

            # Merge additional metadata from other matches
            for other in albums[1:]:
                self._log.debug(f'Merging metadata from {other.data_source}')
                # Fill in missing fields from other sources
                for field in ['year', 'month', 'day', 'label', 'catalognum',
                             'country', 'media', 'albumdisambig']:
                    if not getattr(merged, field) and getattr(other, field):
                        self._log.debug(f'Adding {field} from {other.data_source}: {getattr(other, field)}')
                        setattr(merged, field, getattr(other, field))

                # Merge genre/style lists if present
                if other.genre and merged.genre:
                    merged.genre = list(set(merged.genre + other.genre))
                    self._log.debug(f'Merged genres: {merged.genre}')
                elif other.genre:
                    merged.genre = other.genre
                    self._log.debug(f'Added genres from {other.data_source}: {other.genre}')

                if other.style and merged.style:
                    merged.style = list(set(merged.style + other.style))
                    self._log.debug(f'Merged styles: {merged.style}')
                elif other.style:
                    merged.style = other.style
                    self._log.debug(f'Added styles from {other.data_source}: {other.style}')

            self._log.debug(f'Successfully merged metadata from {len(albums)} sources')
            return merged
        except Exception as e:
            self._log.debug(f'Error merging album info: {str(e)}')
            return None

    def _search_album(self, source, artist, album):
        """Search for an album using various search strategies."""
        plugin = self.source_plugins[source]
        results = []

        self._log.debug(f'Searching {source} for album: {album} by {artist}')

        # Try exact search first
        results = plugin._search_api('album', keywords=album, filters={'artist': artist})
        if results:
            self._log.debug(f'Found {len(results)} results with exact search')
            return results

        # Try without special characters
        clean_album = self._normalize_string(album)
        clean_artist = self._normalize_string(artist)
        results = plugin._search_api('album', keywords=clean_album, filters={'artist': clean_artist})
        if results:
            self._log.debug(f'Found {len(results)} results with cleaned strings')
            return results

        # Try just the main part of the album name (before any dash)
        main_album = album.split('-')[0].strip()
        results = plugin._search_api('album', keywords=main_album, filters={'artist': artist})
        if results:
            self._log.debug(f'Found {len(results)} results with main album name')
            return results

        self._log.debug(f'No results found from {source}')
        return []

    def candidates(self, items, artist, album, va_likely, extra_tags=None):
        """Hook for providing metadata matches during import."""
        self._log.debug(f'\nSearching for matches: {album} by {artist}')

        # Group matches by album to detect same album across sources
        album_groups = defaultdict(list)

        for source in self.sources:
            try:
                # Search for the album using various strategies
                results = self._search_album(source, artist, album)

                if results:
                    self._log.debug(f'Processing {len(results)} results from {source}')
                    for result in results:
                        try:
                            # Get album info from the source plugin
                            album_id = str(result['id'])
                            album_info = self.source_plugins[source].album_for_id(album_id)

                            if album_info:
                                # Set the data source
                                album_info.data_source = source

                                # Try to find matching album group
                                matched = False
                                for group_id, group in album_groups.items():
                                    if any(self._albums_match(album_info, a) for a in group):
                                        self._log.debug(f'Matched album from {source} to existing group {group_id}')
                                        group.append(album_info)
                                        matched = True
                                        break

                                # Create new group if no match found
                                if not matched:
                                    group_id = f"group_{len(album_groups)}"
                                    self._log.debug(f'Creating new group {group_id} for album from {source}')
                                    album_groups[group_id].append(album_info)

                                self._log.debug(f'Found metadata from {source}: {album_info.album} by {album_info.artist}')
                        except Exception as e:
                            self._log.debug(f'Error processing result from {source}: {str(e)}')
                            continue
            except Exception as e:
                self._log.warning('Error getting metadata from {}: {}',
                                source, str(e))

        # Merge matches and return candidates
        self._log.debug(f'\nFound {len(album_groups)} distinct album groups')
        merged_albums = []
        for group_id, group in album_groups.items():
            try:
                self._log.debug(f'\nProcessing group {group_id} with {len(group)} matches')
                merged = self._merge_album_info(group)
                if merged:
                    self._log.debug(f'Successfully merged group {group_id}')
                    merged_albums.append(merged)
            except Exception as e:
                self._log.debug(f'Error processing group {group_id}: {str(e)}')
                continue

        self._log.debug(f'\nReturning {len(merged_albums)} merged candidates')
        return merged_albums
