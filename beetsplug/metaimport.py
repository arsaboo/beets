"""MetaImport plugin: aggregate metadata from multiple sources."""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from beets import autotag, importer, metadata_plugins, plugins, ui
from beets.autotag import hooks as autotag_hooks
from beets.autotag.distance import Distance
from beets.autotag.match import assign_items
from beets.importer import ImportAbortError
from beets.importer.tasks import ImportTask
from beets.library import Album, Item
from beets.plugins import BeetsPlugin
from beets.ui import Subcommand
from beets.ui import commands as ui_commands
from beets.metadata_plugins import MetadataSourcePlugin

PREFIX_OVERRIDES: Dict[str, Tuple[str, ...]] = {
    "musicbrainz": ("mb_", "musicbrainz_"),
}

ID_FIELD_OVERRIDES: Dict[str, Tuple[str, ...]] = {
    "musicbrainz": ("mb_albumid",),
    "spotify": ("spotify_album_id", "spotify_albumid"),
    "deezer": ("deezer_album_id", "deezer_albumid"),
}

CACHE_TABLE = "metaimport_cache"


ALBUM_PASSTHROUGH_FIELDS = {"data_source", "data_url"}
TRACK_PASSTHROUGH_FIELDS = {"data_source", "data_url"}


@dataclass
class SourceMatchResult:
    """Outcome of processing a source for an album."""

    source: str
    plugin: MetadataSourcePlugin
    match: Optional[autotag_hooks.AlbumMatch]
    used_existing_id: bool
    skipped: bool = False
    reason: Optional[str] = None


@dataclass
class MetaImportContext:
    """Resolved configuration for a metaimport run."""

    sources: List[str]
    plugins: Dict[str, MetadataSourcePlugin]
    primary_source: str
    force: bool
    write: bool
    dry_run: bool
    max_distance: Optional[float]
    refresh_cache: bool


@dataclass
class CachedMatchPayload:
    data_source: str
    album_info: dict[str, Any]
    distance: Optional[float]


@dataclass
class CachedMatchRecord:
    data_source: str
    album_info: dict[str, Any]
    distance: Optional[float]
    created: float


@dataclass
class PendingCache:
    matches: Dict[str, CachedMatchPayload]
    created: float

class MetaImportPlugin(BeetsPlugin):
    """Aggregate metadata from all configured metadata sources."""

    def __init__(self) -> None:
        super().__init__()
        self.config.add(
            {
                "sources": "auto",
                "primary_source": None,
                "write": True,
                "max_distance": None,
                "pretend": False,
                "run_during_import": False,
                "cache_ttl_days": 30,
            }
        )
        self._terminal_session: ui_commands.TerminalImportSession | None = None
        self.register_listener("library_opened", self._on_library_opened)
        self.register_listener("import_task_choice", self._on_import_task_choice)
        self.register_listener("import_task_apply", self._on_import_task_apply)
        self.import_stages.append(self._run_during_import_stage)

        self._pending_cache: Dict[int, PendingCache] = {}
        self._recent_import_matches: Dict[int, PendingCache] = {}
        self._cache_lock = threading.Lock()


    # --------------------------------- Commands ---------------------------------

    def commands(self) -> List[Subcommand]:
        cmd = Subcommand("metaimport", help="merge metadata from configured sources")
        cmd.parser.add_option(
            "-f",
            "--force",
            action="store_true",
            dest="force",
            default=False,
            help="re-run lookups even when source IDs already exist",
        )
        cmd.parser.add_option(
            "-p",
            "--pretend",
            action="store_true",
            dest="pretend",
            default=False,
            help="show planned changes without storing them",
        )
        cmd.parser.add_option(
            "--primary-source",
            action="store",
            dest="primary_source",
            help="override the primary source for this run",
        )
        cmd.parser.add_option(
            "--max-distance",
            action="store",
            dest="max_distance",
            type="float",
            help="maximum distance to accept automatically per source",
        )

        cmd.parser.add_option(
            "--refresh-cache",
            action="store_true",
            dest="refresh_cache",
            default=False,
            help="ignore cached importer matches and force fresh lookups",
        )

        def func(lib, opts, args):
            query = list(args)
            context = self._build_context(opts)
            if not context.sources:
                self._log.warning("No metadata sources available; nothing to do")
                return

            joined_sources = ", ".join(context.sources)
            self._log.debug(
                f"Metaimport starting for {len(context.sources)} sources: {joined_sources}"
            )

            self._run(lib, query, context)

        cmd.func = func
        return [cmd]

    # ----------------------------- Context Utilities ----------------------------

    def _build_context(self, opts) -> MetaImportContext:
        configured_sources = self.config["sources"].get()
        override_list: Optional[Sequence[str]] = None
        if isinstance(configured_sources, str):
            if configured_sources.lower() != "auto":
                override_list = [configured_sources]
        else:
            override_list = [str(s) for s in configured_sources]

        sources, plugins_by_key = self._resolve_sources(override_list)

        primary_source_cfg = opts.primary_source or self.config["primary_source"].get()
        primary_source: Optional[str]
        if primary_source_cfg:
            candidate = self._normalize_source(primary_source_cfg)
            if candidate not in plugins_by_key:
                self._log.warning(
                    f"Configured primary source '{primary_source_cfg}' is not available; ignoring"
                )
                primary_source = None
            else:
                primary_source = candidate
        else:
            primary_source = None

        if not primary_source and sources:
            primary_source = sources[-1]

        force = bool(opts.force)
        pretend = bool(opts.pretend) or self.config["pretend"].get(bool)
        write = self.config["write"].get(bool)
        max_distance_opt = opts.max_distance
        if max_distance_opt is None:
            max_distance_cfg = self.config["max_distance"].get()
            max_distance: Optional[float]
            if max_distance_cfg is None:
                max_distance = None
            else:
                try:
                    max_distance = float(max_distance_cfg)
                except (TypeError, ValueError):
                    self._log.warning(
                        f"Invalid max_distance value {max_distance_cfg}; ignoring"
                    )
                    max_distance = None
        else:
            max_distance = float(max_distance_opt)

        refresh_cache = bool(getattr(opts, "refresh_cache", False))

        return MetaImportContext(
            sources=sources,
            plugins=plugins_by_key,
            primary_source=primary_source or "",
            force=force,
            write=write,
            dry_run=pretend,
            max_distance=max_distance,
            refresh_cache=refresh_cache,
        )

    def _resolve_sources(
        self, override: Optional[Sequence[str]]
    ) -> Tuple[List[str], Dict[str, MetadataSourcePlugin]]:
        available_plugins = metadata_plugins.find_metadata_source_plugins()
        source_map: Dict[str, MetadataSourcePlugin] = {}
        ordered_keys: List[str] = []

        for plugin in available_plugins:
            key = self._normalize_source(plugin.data_source)
            if key not in source_map:
                source_map[key] = plugin
                ordered_keys.append(key)

        if override is None:
            return ordered_keys, source_map

        resolved: List[str] = []
        for name in override:
            key = self._normalize_source(name)
            if key in source_map:
                resolved.append(key)
            else:
                self._log.warning(
                    f"Configured metadata source '{name}' is not loaded; skipping"
                )

        return resolved, source_map

    @staticmethod
    def _normalize_source(name: str) -> str:
        return name.replace("_", "").replace("-", "").replace(" ", "").lower()

    # ------------------------------ Cache Utilities ------------------------------

    def _on_library_opened(self, lib) -> None:
        with lib.transaction():
            conn = lib._connection()
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {CACHE_TABLE} (
                    album_id INTEGER NOT NULL,
                    source_key TEXT NOT NULL,
                    data_source TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    distance REAL,
                    created REAL NOT NULL,
                    PRIMARY KEY (album_id, source_key)
                )
                """
            )

    def _on_import_task_choice(self, session, task) -> None:
        if not getattr(task, "is_album", False):
            return

        candidates = getattr(task, "candidates", None)
        if not candidates:
            return

        matches: Dict[str, CachedMatchPayload] = {}
        for candidate in candidates:
            if not isinstance(candidate, autotag_hooks.AlbumMatch):
                continue
            data_source = getattr(candidate.info, "data_source", None)
            if not data_source:
                continue
            source_key = self._normalize_source(data_source)
            if source_key in matches:
                continue
            distance_obj = getattr(candidate, "distance", None)
            if distance_obj is None:
                distance_value: Optional[float] = None
            else:
                try:
                    distance_value = float(distance_obj.distance)
                except Exception:
                    distance_value = None
            matches[source_key] = CachedMatchPayload(
                data_source=data_source,
                album_info=self._serialize_album_info(candidate.info),
                distance=distance_value,
            )

        if not matches:
            return

        if getattr(task, "choice_flag", None) is importer.Action.SKIP:
            return

        pending = PendingCache(matches=matches, created=time.time())
        with self._cache_lock:
            self._pending_cache[id(task)] = pending

    def _on_import_task_apply(self, session, task) -> None:
        if not getattr(task, "is_album", False):
            return

        album = getattr(task, "album", None)
        if album is None or album.id is None:
            return

        with self._cache_lock:
            pending = self._pending_cache.pop(id(task), None)
        if not pending:
            return

        self._write_cache_entries(session.lib, album.id, pending)
        with self._cache_lock:
            self._recent_import_matches[id(task)] = pending

    def _write_cache_entries(self, lib, album_id: int, pending: PendingCache) -> None:
        if not pending.matches:
            return

        rows = [
            (
                album_id,
                source_key,
                match.data_source,
                json.dumps(match.album_info, separators=(",", ":"), sort_keys=True),
                match.distance,
                pending.created,
            )
            for source_key, match in pending.matches.items()
        ]

        with lib.transaction():
            conn = lib._connection()
            conn.execute(f"DELETE FROM {CACHE_TABLE} WHERE album_id = ?", (album_id,))
            conn.executemany(
                f"INSERT OR REPLACE INTO {CACHE_TABLE} (album_id, source_key, data_source, payload, distance, created) VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )

    def _load_cached_matches_for_album(self, lib, album_id: int) -> Dict[str, CachedMatchPayload]:
        conn = lib._connection()
        rows = conn.execute(
            f"SELECT source_key, data_source, payload, distance, created FROM {CACHE_TABLE} WHERE album_id = ?",
            (album_id,),
        ).fetchall()

        if not rows:
            return {}

        ttl_days = self.config["cache_ttl_days"].get(int)
        ttl_seconds = ttl_days * 86400 if ttl_days else None
        now = time.time()
        expired: list[str] = []
        matches: Dict[str, CachedMatchPayload] = {}

        for source_key, data_source, payload, distance, created in rows:
            if ttl_seconds and now - created > ttl_seconds:
                expired.append(source_key)
                continue
            try:
                album_info = json.loads(payload)
            except Exception:
                self._log.debug("Failed to decode cached payload for %s; ignoring", source_key)
                expired.append(source_key)
                continue
            matches[source_key] = CachedMatchPayload(
                data_source=data_source,
                album_info=album_info,
                distance=distance,
            )

        if expired:
            with lib.transaction():
                conn.executemany(
                    f"DELETE FROM {CACHE_TABLE} WHERE album_id = ? AND source_key = ?",
                    [(album_id, key) for key in expired],
                )

        return matches

    def _serialize_album_info(self, album_info: autotag_hooks.AlbumInfo) -> dict[str, Any]:
        return self._serialize_info(album_info)

    def _serialize_info(self, info: autotag_hooks.Info) -> dict[str, Any]:
        data: dict[str, Any] = {"__class__": info.__class__.__name__}
        for key, value in info.items():
            data[key] = self._serialize_value(value)
        return data

    def _serialize_value(self, value: Any) -> Any:
        if isinstance(value, autotag_hooks.Info):
            return self._serialize_info(value)
        if isinstance(value, list):
            return [self._serialize_value(v) for v in value]
        return value

    def _deserialize_album_info(self, data: dict[str, Any]) -> autotag_hooks.AlbumInfo:
        info = self._deserialize_info(data)
        assert isinstance(info, autotag_hooks.AlbumInfo)
        return info

    def _deserialize_info(self, data: dict[str, Any]) -> autotag_hooks.Info:
        cls_name = data.get("__class__", "Info")
        payload = {
            key: self._deserialize_value(value)
            for key, value in data.items()
            if key != "__class__"
        }
        if cls_name == "AlbumInfo":
            tracks = payload.pop("tracks", [])
            track_infos: List[autotag_hooks.TrackInfo] = []
            for track in tracks:
                if isinstance(track, autotag_hooks.TrackInfo):
                    track_infos.append(track)
                elif isinstance(track, dict):
                    track_infos.append(autotag_hooks.TrackInfo(**track))
                else:
                    raise TypeError(f"Unexpected track payload type: {type(track)!r}")
            return autotag_hooks.AlbumInfo(track_infos, **payload)
        if cls_name == "TrackInfo":
            return autotag_hooks.TrackInfo(**payload)
        return autotag_hooks.Info(**payload)

    def _deserialize_value(self, value: Any) -> Any:
        if isinstance(value, dict):
            if "__class__" in value:
                return self._deserialize_info(value)
            return {k: self._deserialize_value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._deserialize_value(v) for v in value]
        return value

    def _build_context_for_session(self, session: importer.ImportSession) -> MetaImportContext:
        opts = SimpleNamespace(
            force=False,
            pretend=session.config.get("pretend", False),
            primary_source=self.config["primary_source"].get(),
            max_distance=self.config["max_distance"].get(),
            refresh_cache=False,
        )
        context = self._build_context(opts)
        context.write = context.write and bool(session.config.get("write", True))
        context.dry_run = context.dry_run or bool(session.config.get("pretend", False))
        return context

    def _run_during_import_stage(self, session, task: ImportTask) -> None:
        if not self.config["run_during_import"].get(bool):
            return

        if not getattr(task, "is_album", False):
            return

        album = getattr(task, "album", None)
        if album is None or album.id is None:
            return

        if getattr(task, "choice_flag", None) is not importer.Action.APPLY:
            return

        pending: PendingCache | None = None
        from_pending_pool = False
        with self._cache_lock:
            cached = self._recent_import_matches.pop(id(task), None)
            if cached is not None:
                pending = cached
            else:
                cached = self._pending_cache.pop(id(task), None)
                if cached is not None:
                    pending = cached
                    from_pending_pool = True

        if pending and from_pending_pool:
            self._write_cache_entries(session.lib, album.id, pending)

        if getattr(task, "choice_flag", None) is not importer.Action.APPLY:
            return

        if pending:
            matches = pending.matches
        else:
            matches = self._load_cached_matches_for_album(session.lib, album.id)

        if not matches:
            return

        context = self._build_context_for_session(session)
        items = list(album.items())
        self._apply_cached_candidates(album, items, matches, context)

    def _apply_cached_candidates(
        self,
        album: Album,
        items: Sequence[Item],
        matches: Dict[str, CachedMatchPayload],
        context: MetaImportContext,
    ) -> None:
        for source_key in context.sources:
            payload = matches.get(source_key)
            if not payload:
                continue

            plugin = context.plugins.get(source_key)
            if not plugin:
                continue

            if (
                context.max_distance is not None
                and payload.distance is not None
                and payload.distance > context.max_distance
            ):
                self._log.info(
                    "%s cached match distance %.3f above threshold %.3f; skipping",
                    plugin.data_source,
                    payload.distance,
                    context.max_distance,
                )
                continue

            album_info = self._deserialize_album_info(payload.album_info)
            mapping, extra_items, extra_tracks = self._assign_tracks(items, album_info, plugin)
            if not mapping:
                self._log.debug(
                    "Cached %s metadata has no usable track mapping; skipping",
                    plugin.data_source,
                )
                continue

            match = autotag_hooks.AlbumMatch(
                distance=Distance(),
                info=album_info,
                mapping=mapping,
                extra_items=extra_items,
                extra_tracks=extra_tracks,
            )

            result = SourceMatchResult(
                source=source_key,
                plugin=plugin,
                match=match,
                used_existing_id=False,
            )
            self._apply_result(album, result, context)

    @staticmethod
    def _distance_value(distance_obj: Distance | float | None) -> Optional[float]:
        if distance_obj is None:
            return None
        if isinstance(distance_obj, Distance):
            try:
                return float(distance_obj.distance)
            except Exception:
                return None
        try:
            return float(distance_obj)
        except Exception:
            return None

    # ------------------------------ Core Execution ------------------------------

    def _run(
        self,
        lib,
        query: Sequence[str],
        context: MetaImportContext,
    ) -> None:
        terminal_session = self._ensure_terminal_session(lib)
        album_iter = lib.albums(query) if query else lib.albums()

        processed = 0
        for album in album_iter:
            processed += 1
            self._log.info(
                f"Metaimport {album.albumartist or album.artist} - {album.album}"
            )
            try:
                self._process_album(album, context, terminal_session)
            except ImportAbortError:
                self._log.warning("Metaimport aborted by user")
                break
            except Exception:
                album_label = f"{album.albumartist or album.artist} - {album.album}"
                self._log.exception(
                    f"Unexpected error processing album {album_label}"
                )

        if processed == 0:
            self._log.info("No albums matched the query; nothing processed")

    def _ensure_terminal_session(self, lib) -> ui_commands.TerminalImportSession:
        if self._terminal_session is None:
            self._terminal_session = ui_commands.TerminalImportSession(lib, None, [], None)
        return self._terminal_session

    # ----------------------------- Album Processing -----------------------------

    def _process_album(
        self,
        album: Album,
        context: MetaImportContext,
        terminal_session: ui_commands.TerminalImportSession,
    ) -> None:
        items = list(album.items())
        if not items:
            self._log.debug(f"Album {album.id} has no items; skipping")
            return

        cached_matches: Dict[str, CachedMatchPayload] = {}
        if (
            album.id is not None
            and not context.force
            and not context.refresh_cache
        ):
            lib = getattr(album, "_db", None)
            if lib is not None:
                cached_matches = self._load_cached_matches_for_album(lib, album.id)

        applied_matches: Dict[str, CachedMatchPayload] = {}

        for source_key in context.sources:
            plugin = context.plugins.get(source_key)
            if not plugin:
                self._log.debug(f"Source {source_key} no longer available; skipping")
                continue

            result = self._process_source_for_album(
                album,
                items,
                plugin,
                source_key,
                context,
                terminal_session,
                cached_match=cached_matches.get(source_key),
            )
            if result.skipped:
                reason = f" ({result.reason})" if result.reason else ""
                self._log.info(
                    f"Skipping {plugin.data_source} for {format(album)}{reason}"
                )
                continue

            if result.match:
                applied_matches[source_key] = CachedMatchPayload(
                    data_source=plugin.data_source,
                    album_info=self._serialize_album_info(result.match.info),
                    distance=self._distance_value(result.match.distance),
                )

            self._apply_result(album, result, context)

    @staticmethod
    def _distance_value(distance_obj: Distance | float | None) -> Optional[float]:
        if distance_obj is None:
            return None
        if isinstance(distance_obj, Distance):
            try:
                return float(distance_obj.distance)
            except Exception:
                return None
        try:
            return float(distance_obj)
        except Exception:
            return None

        if applied_matches and album.id is not None:
            lib = getattr(album, "_db", None)
            if lib is not None:
                self._write_cache_entries(
                    lib,
                    album.id,
                    PendingCache(matches=applied_matches, created=time.time()),
                )

    def _process_source_for_album(
        self,
        album: Album,
        items: List[Item],
        plugin: MetadataSourcePlugin,
        source_key: str,
        context: MetaImportContext,
        terminal_session: ui_commands.TerminalImportSession,
        cached_match: CachedMatchPayload | None = None,
    ) -> SourceMatchResult:
        used_existing_id = False

        if cached_match and not context.force and not context.refresh_cache:
            if (
                context.max_distance is not None
                and cached_match.distance is not None
                and cached_match.distance > context.max_distance
            ):
                return SourceMatchResult(
                    source=source_key,
                    plugin=plugin,
                    match=None,
                    used_existing_id=False,
                    skipped=True,
                    reason="cached distance threshold",
                )

            album_info = self._deserialize_album_info(cached_match.album_info)
            mapping, extra_items, extra_tracks = self._assign_tracks(
                items, album_info, plugin
            )
            if mapping:
                self._log.debug("Using cached %s metadata", plugin.data_source)
                match = autotag_hooks.AlbumMatch(
                    distance=Distance(),
                    info=album_info,
                    mapping=mapping,
                    extra_items=extra_items,
                    extra_tracks=extra_tracks,
                )
                return SourceMatchResult(
                    source=source_key,
                    plugin=plugin,
                    match=match,
                    used_existing_id=False,
                )

            self._log.debug(
                "Cached %s metadata could not be aligned; falling back to lookup",
                plugin.data_source,
            )

        existing_id = self._current_source_id(album, source_key)

        if existing_id and not context.force:
            self._log.debug(
                f"{plugin.data_source} already has ID {existing_id}; loading existing metadata"
            )
            try:
                album_info = plugin.album_for_id(existing_id)
            except Exception:
                self._log.exception(
                    f"Failed fetching album info for {plugin.data_source} id {existing_id}; falling back to search"
                )
                album_info = None

            if album_info:
                used_existing_id = True
                mapping, extra_items, extra_tracks = self._assign_tracks(
                    items, album_info, plugin
                )
                if not mapping:
                    return SourceMatchResult(
                        source=source_key,
                        plugin=plugin,
                        match=None,
                        used_existing_id=True,
                        skipped=True,
                        reason="no track mapping",
                    )

                match = autotag_hooks.AlbumMatch(
                    distance=Distance(),
                    info=album_info,
                    mapping=mapping,
                    extra_items=extra_items,
                    extra_tracks=extra_tracks,
                )
                return SourceMatchResult(
                    source=source_key,
                    plugin=plugin,
                    match=match,
                    used_existing_id=True,
                )

        with self._limit_metadata_plugins(plugin):
            cur_artist, cur_album, proposal = autotag.tag_album(items)

        if not proposal.candidates:
            return SourceMatchResult(
                source=source_key,
                plugin=plugin,
                match=None,
                used_existing_id=used_existing_id,
                skipped=True,
                reason="no candidates",
            )

        if (
            context.max_distance is not None
            and proposal.candidates
            and proposal.candidates[0].distance > context.max_distance
        ):
            self._log.info(
                f"{plugin.data_source} candidate distance {proposal.candidates[0].distance:.3f} "
                f"above threshold {context.max_distance:.3f}; skipping"
            )
            return SourceMatchResult(
                source=source_key,
                plugin=plugin,
                match=None,
                used_existing_id=used_existing_id,
                skipped=True,
                reason="distance threshold",
            )

        task = ImportTask(
            None,
            [item.path for item in items],
            items,
        )
        task.cur_artist = cur_artist
        task.cur_album = cur_album
        task.candidates = proposal.candidates
        task.rec = proposal.recommendation

        plugins.send("import_task_start", session=terminal_session, task=task)
        try:
            choice = terminal_session.choose_match(task)
        except ImportAbortError:
            raise

        if choice is None:
            return SourceMatchResult(
                source=source_key,
                plugin=plugin,
                match=None,
                used_existing_id=used_existing_id,
                skipped=True,
                reason="no selection",
            )

        if isinstance(choice, importer.Action):
            task.set_choice(choice)
            plugins.send("import_task_choice", session=terminal_session, task=task)

            if choice in (importer.Action.SKIP, importer.Action.ASIS):
                return SourceMatchResult(
                    source=source_key,
                    plugin=plugin,
                    match=None,
                    used_existing_id=used_existing_id,
                    skipped=True,
                    reason="user skipped",
                )

            self._log.warning(
                f"Action {choice.name} is not supported in metaimport; skipping {plugin.data_source}"
            )
            return SourceMatchResult(
                source=source_key,
                plugin=plugin,
                match=None,
                used_existing_id=used_existing_id,
                skipped=True,
                reason="unsupported action",
            )

        assert isinstance(choice, autotag_hooks.AlbumMatch)
        task.set_choice(choice)
        plugins.send("import_task_choice", session=terminal_session, task=task)

        return SourceMatchResult(
            source=source_key,
            plugin=plugin,
            match=choice,
            used_existing_id=used_existing_id,
        )

    def _assign_tracks(
        self,
        items: Sequence[Item],
        album_info: autotag_hooks.AlbumInfo,
        plugin: MetadataSourcePlugin,
    ) -> Tuple[Dict[Item, autotag_hooks.TrackInfo], List[Item], List[autotag_hooks.TrackInfo]]:
        with self._limit_metadata_plugins(plugin):
            mapping, extra_items, extra_tracks = assign_items(items, album_info.tracks)
        return mapping, extra_items, extra_tracks

    @contextmanager
    def _limit_metadata_plugins(
        self, plugin: MetadataSourcePlugin
    ) -> Iterator[None]:
        original = metadata_plugins.find_metadata_source_plugins

        def _filtered() -> List[MetadataSourcePlugin]:
            return [plugin]

        metadata_plugins.find_metadata_source_plugins = _filtered  # type: ignore[assignment]
        try:
            yield
        finally:
            metadata_plugins.find_metadata_source_plugins = original  # type: ignore[assignment]

    # ------------------------------ Metadata Apply ------------------------------

    def _apply_result(
        self,
        album: Album,
        result: SourceMatchResult,
        context: MetaImportContext,
    ) -> None:
        if not result.match:
            return

        album_info = result.match.info
        mapping = result.match.mapping

        album_changes = self._apply_album_fields(
            album,
            album_info,
            result.source,
            context.dry_run,
        )

        track_changed = False
        for item, track_info in mapping.items():
            changes = self._apply_track_fields(
                item,
                track_info,
                result.source,
                context.dry_run,
            )
            if changes:
                track_changed = True
                if not context.dry_run:
                    item.store()
                    if context.write:
                        item.try_write()

        if album_changes and not context.dry_run:
            album.store()

        if album_changes or track_changed:
            suffix = " (pretend)" if context.dry_run else ""
            self._log.info(f"Applied {result.plugin.data_source} metadata{suffix}")

    def _apply_album_fields(
        self,
        album: Album,
        album_info: autotag_hooks.AlbumInfo,
        source_key: str,
        dry_run: bool,
    ) -> Dict[str, Tuple[object, object]]:
        changes: Dict[str, Tuple[object, object]] = {}

        for field, value in album_info.items():
            if value is None:
                continue

            current = album.get(field)
            if current == value:
                continue
            changes[field] = (current, value)
            if not dry_run:
                album[field] = value

        return changes

    def _apply_track_fields(
        self,
        item: Item,
        track_info: autotag_hooks.TrackInfo,
        source_key: str,
        dry_run: bool,
    ) -> Dict[str, Tuple[object, object]]:
        changes: Dict[str, Tuple[object, object]] = {}

        for field, value in track_info.items():
            if value is None:
                continue

            current = item.get(field)
            if current == value:
                continue
            changes[field] = (current, value)
            if not dry_run:
                item[field] = value

        return changes

    def _current_source_id(self, album: Album, source_key: str) -> Optional[str]:
        for field in ID_FIELD_OVERRIDES.get(
            source_key, (f"{source_key}_album_id", f"{source_key}_albumid")
        ):
            try:
                value = album.get(field)
            except KeyError:
                continue
            if value:
                return str(value)
        return None

