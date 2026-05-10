MetaImport Plugin
=================

The ``metaimport`` plugin enriches albums with metadata from multiple configured
metadata source plugins while keeping a preferred provider as the starting
point.

This is useful when you want one provider, such as Spotify, to supply the main
match and provider-specific fields, while other providers fill in missing data.
When the ``musicbrainz`` plugin is enabled, ``metaimport`` always applies it
last so ``mb_*`` fields end up containing MusicBrainz identifiers and related
metadata.

Installation
------------

Enable the ``metaimport`` plugin in your configuration (see
:ref:`using-plugins`).

Configuration
-------------

To configure the plugin, add a ``metaimport:`` section to your configuration
file.

Default
~~~~~~~

.. code-block:: yaml

    metaimport:
        sources: auto
        primary_source: null
        providers: {}
        write: yes
        max_distance: null
        pretend: no
        run_during_import: no
        cache_ttl_days: 30

.. conf:: sources
    :default: auto

    Which metadata source plugins to use. ``auto`` means all loaded metadata
    source plugins. You can also provide a list such as ``[spotify,
    musicbrainz, gaana]``.

.. conf:: primary_source
    :default: null

    The provider whose metadata should be applied first. If unset,
    ``metaimport`` uses the last configured source as the primary source.

.. conf:: providers
    :default: {}

    Per-provider field exclusions. This lets you suppress fields from a specific
    metadata source when they are noisy or undesirable.

    Supported keys per provider are:

    - ``exclude_album_fields``
    - ``exclude_track_fields``

    Example:

    .. code-block:: yaml

        metaimport:
            providers:
                gaana:
                    exclude_album_fields: [label]
                    exclude_track_fields: []

.. conf:: write
    :default: yes

    Write changed metadata back to files after storing it in the library.

.. conf:: max_distance
    :default: null

    Maximum candidate distance accepted for a source. Candidates above this
    threshold are skipped.

.. conf:: pretend
    :default: no

    Preview changes without storing them.

.. conf:: run_during_import
    :default: no

    Reuse cached importer matches and apply metadata during import.

.. conf:: cache_ttl_days
    :default: 30

    Number of days to keep cached source matches.

Field Precedence
----------------

``metaimport`` separates lookup order from apply order.

Apply order is always:

1. the configured ``primary_source``
2. every other non-MusicBrainz provider
3. ``musicbrainz`` last, if enabled

This ordering is enforced even if ``musicbrainz`` appears earlier in the
``sources`` list.

Field application follows these rules:

1. The primary source may write all mapped fields it provides.
2. Secondary non-MusicBrainz sources always write provider-specific fields such
   as ``spotify_*``, ``deezer_*``, or third-party fields like ``gaana_*``.
3. Secondary sources may fill missing shared fields, but they do not overwrite
   shared values that are already populated.
4. MusicBrainz always writes ``mb_*`` fields when available.
5. Provider exclusions configured under ``providers`` always take precedence and
   prevent a field from being written.

This lets you keep provider-specific metadata from multiple sources while still
ensuring MusicBrainz identifiers are stored in ``mb_*`` fields.

Examples
--------

Spotify Primary With MusicBrainz Enrichment
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This configuration uses Spotify as the primary source, allows Gaana to fill in
missing shared fields such as ``cover_art_url``, and always applies MusicBrainz
last:

.. code-block:: yaml

    plugins: spotify musicbrainz metaimport gaana

    metaimport:
        primary_source: spotify
        sources: [spotify, gaana, musicbrainz]
        providers:
            gaana:
                exclude_album_fields: []
                exclude_track_fields: []

In this setup:

- Spotify provides the initial metadata and writes ``spotify_*`` fields.
- Gaana keeps writing ``gaana_*`` fields and may fill missing shared fields.
- MusicBrainz runs last and writes ``mb_*`` fields.

Exclude Fields From One Provider
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If a provider returns a field you do not want, exclude it explicitly:

.. code-block:: yaml

    metaimport:
        providers:
            gaana:
                exclude_album_fields: [label, cover_art_url]
                exclude_track_fields: []

Usage
-----

Run ``beet metaimport QUERY`` to enrich matching albums.

Useful command-line options:

- ``-f`` / ``--force``: ignore existing source IDs and re-run lookups
- ``-p`` / ``--pretend``: preview changes without storing them
- ``--primary-source``: temporarily override the configured primary source
- ``--max-distance``: reject source matches above a threshold
- ``--refresh-cache``: ignore cached importer matches and force fresh lookups
