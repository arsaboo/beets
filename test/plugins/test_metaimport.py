from beets.autotag import hooks as autotag_hooks
from beets.library import Album, Item
from beets.test.helper import PluginTestCase
from beetsplug.metaimport import MetaImportContext, MetaImportPlugin


class MetaImportPluginTest(PluginTestCase):
    plugin = "metaimport"

    def setUp(self):
        super().setUp()
        self.plugin_instance = MetaImportPlugin()
        self.context = MetaImportContext(
            sources=["spotify", "gaana", "musicbrainz"],
            plugins={},
            primary_source="spotify",
            force=False,
            write=False,
            dry_run=False,
            max_distance=None,
            refresh_cache=False,
            provider_settings={
                "gaana": {
                    "exclude_album_fields": set(),
                    "exclude_track_fields": set(),
                },
                "musicbrainz": {
                    "exclude_album_fields": set(),
                    "exclude_track_fields": set(),
                },
            },
        )

    def test_ordered_sources_primary_first_musicbrainz_last(self):
        ordered = self.plugin_instance._ordered_sources(
            ["gaana", "musicbrainz", "spotify", "deezer"], "spotify"
        )

        assert ordered == ["spotify", "gaana", "deezer", "musicbrainz"]

    def test_secondary_provider_fills_missing_shared_album_field(self):
        album = Album()
        info = autotag_hooks.AlbumInfo(
            tracks=[],
            album="Gaana Album",
            cover_art_url="https://example.com/cover.jpg",
            gaana_album_id="gaana-1",
        )

        changes = self.plugin_instance._apply_album_fields(
            album, info, "gaana", self.context, dry_run=False
        )

        assert album["cover_art_url"] == "https://example.com/cover.jpg"
        assert album["gaana_album_id"] == "gaana-1"
        assert "cover_art_url" in changes
        assert "gaana_album_id" in changes

    def test_secondary_provider_does_not_override_existing_shared_album_field(self):
        album = Album(cover_art_url="https://example.com/primary.jpg")
        info = autotag_hooks.AlbumInfo(
            tracks=[],
            cover_art_url="https://example.com/secondary.jpg",
            gaana_album_id="gaana-1",
        )

        changes = self.plugin_instance._apply_album_fields(
            album, info, "gaana", self.context, dry_run=False
        )

        assert album["cover_art_url"] == "https://example.com/primary.jpg"
        assert album["gaana_album_id"] == "gaana-1"
        assert "cover_art_url" not in changes
        assert "gaana_album_id" in changes

    def test_primary_source_overrides_existing_shared_field(self):
        album = Album(label="Old Label")
        info = autotag_hooks.AlbumInfo(
            tracks=[],
            label="Spotify Label",
            spotify_album_id="spotify-1",
        )

        changes = self.plugin_instance._apply_album_fields(
            album, info, "spotify", self.context, dry_run=False
        )

        assert album["label"] == "Spotify Label"
        assert album["spotify_album_id"] == "spotify-1"
        assert "label" in changes

    def test_musicbrainz_always_writes_mb_fields(self):
        album = Album(mb_albumid="spotify-id")
        info = autotag_hooks.AlbumInfo(
            tracks=[],
            album_id="mb-release-id",
            releasegroup_id="mb-group-id",
        )

        changes = self.plugin_instance._apply_album_fields(
            album, info, "musicbrainz", self.context, dry_run=False
        )

        assert album["mb_albumid"] == "mb-release-id"
        assert album["mb_releasegroupid"] == "mb-group-id"
        assert "mb_albumid" in changes
        assert "mb_releasegroupid" in changes

    def test_provider_excluded_field_is_skipped(self):
        context = MetaImportContext(
            sources=self.context.sources,
            plugins={},
            primary_source="spotify",
            force=False,
            write=False,
            dry_run=False,
            max_distance=None,
            refresh_cache=False,
            provider_settings={
                "gaana": {
                    "exclude_album_fields": {"cover_art_url"},
                    "exclude_track_fields": set(),
                }
            },
        )
        album = Album()
        info = autotag_hooks.AlbumInfo(
            tracks=[],
            cover_art_url="https://example.com/cover.jpg",
            gaana_album_id="gaana-1",
        )

        changes = self.plugin_instance._apply_album_fields(
            album, info, "gaana", context, dry_run=False
        )

        assert album.get("cover_art_url") is None
        assert album["gaana_album_id"] == "gaana-1"
        assert "cover_art_url" not in changes

    def test_secondary_provider_fills_missing_shared_track_field(self):
        item = Item(title=None)
        info = autotag_hooks.TrackInfo(
            title="Fallback Title",
            gaana_track_id="gaana-track-1",
        )

        changes = self.plugin_instance._apply_track_fields(
            item, info, "gaana", self.context, dry_run=False
        )

        assert item["title"] == "Fallback Title"
        assert item["gaana_track_id"] == "gaana-track-1"
        assert "title" in changes
        assert "gaana_track_id" in changes
