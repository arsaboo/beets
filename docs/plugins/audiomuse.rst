AudioMuse
=========

The ``audiomuse`` plugin integrates with an AudioMuse-AI server to enrich your
music library with AI-powered sonic analysis. It retrieves unique track
identifiers (``item_id``), embedding vectors for semantic similarity, and rich
score metadata (energy, tempo, key, scale, mood labels).

AudioMuse-AI uses deep learning to analyze your music's sonic characteristics,
enabling advanced querying and playlist generation based on audio features
rather than just metadata tags.

Prerequisites
-------------

This plugin assumes you have an AudioMuse-AI Core instance running and reachable
from your beets host. For setup instructions, installation details, and API
capabilities, see the AudioMuse-AI project:

    https://github.com/NeptuneHub/AudioMuse-AI

Be sure the service URL (host:port) is accessible before invoking any
``audiomuse_*`` commands; otherwise requests will fail gracefully with debug
logs.

Setup
-----

Enable the plugin in your :doc:`beets configuration </reference/config>`:

.. code-block:: yaml

    plugins: audiomuse

Configuration
-------------

Add an ``audiomuse`` section to point at your AudioMuse-AI Core server:

.. code-block:: yaml

    audiomuse:
      url: "http://192.168.2.162:8001"  # Base URL of your AudioMuse-AI Core

If omitted, the plugin defaults to ``http://127.0.0.1:8001``.

Commands
--------

Resolve and Store Item IDs
~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: sh

    beet audiomuse_match QUERY [-p]

Resolves and stores ``audiomuse_item_id`` for each selected item by querying
``/api/search_tracks`` with the item's title and artist. Matching is tolerant to
common formatting differences (extra spaces, featuring separators, etc.).
Matched item IDs are stored in the beets database.

Options
+++++++

- ``-p``, ``--pretend``: Preview matches without storing to the database. Useful
  for testing the matching logic before committing changes.

Fetch Embeddings
~~~~~~~~~~~~~~~~

.. code-block:: sh

    beet audiomuse_get_embedding QUERY [-p]

For each item with an ``audiomuse_item_id``, fetches the audio embedding vector
from ``/external/get_embedding?id=<item_id>`` and stores it in
``audiomuse_embedding`` for similarity and analysis workflows.

Options
+++++++

- ``-p``, ``--pretend``: Preview embeddings without storing to the database.
  Shows the dimension count without saving data.

Fetch Score Metadata
~~~~~~~~~~~~~~~~~~~~

.. code-block:: sh

    beet audiomuse_get_score QUERY [-p]

For each item with an ``audiomuse_item_id``, fetches comprehensive audio
analysis metadata from ``/external/get_score?id=<item_id>``. AudioMuse-AI
computes these features using signal processing and machine learning models.

**API Response Structure**:

- ``energy`` (float): Overall intensity and activity level
- ``tempo`` (float): Estimated beats per minute (BPM)
- ``key`` (string): Detected musical key (e.g., "C", "F#")
- ``scale`` (string): Detected scale mode (e.g., "major", "minor")
- ``mood_vector`` (string): Comma-separated ``label:value`` pairs for mood
  dimensions (e.g., ``"valence:0.8,arousal:0.6"``). Common mood labels include
  valence, arousal, danceability, acousticness.
- ``other_features`` (string): Comma-separated ``label:value`` pairs for
  additional audio features (e.g., ``"speechiness:0.1,instrumentalness:0.95"``).

**Field Storage**:

- ``audiomuse_energy`` (float)
- ``audiomuse_tempo`` (float)
- ``audiomuse_key`` (string)
- ``audiomuse_scale`` (string)
- Dynamic fields from ``mood_vector`` and ``other_features`` are automatically
  parsed, slugified (labels converted to ``audiomuse_<lowercase_label_name>``),
  and stored. Values are stored as floats if numeric, otherwise as strings.

Example dynamic fields: ``audiomuse_valence``, ``audiomuse_arousal``,
``audiomuse_danceability``, ``audiomuse_speechiness``.

Options
+++++++

- ``-p``, ``--pretend``: Preview score data without storing to the database.
  Shows field names and values that would be stored.

Find Similar Tracks
~~~~~~~~~~~~~~~~~~~

.. code-block:: sh

    beet audiomuse_similar QUERY [-n COUNT]

Finds similar tracks using AudioMuse-AI's embedding-based similarity search. For
each item with an ``audiomuse_item_id``, it queries
``/api/similar_tracks?item_id=<item_id>&n=<count>`` to retrieve tracks that
sound alike.

**Use Cases**:

- Discover new music similar to your favorite tracks
- Build better radio stations and playlists based on audio characteristics
- Find tracks that "sound similar" even across different genres or artists

Options
+++++++

- ``-n``, ``--count``: Number of similar tracks to retrieve (default: 20, max
  typically 100 depending on server configuration)

Examples
--------

**Preview Before Storing**

Preview matches before storing to database:

.. code-block:: sh

    beet audiomuse_match artist:"Daft Punk" --pretend

Preview embedding dimensions without storing:

.. code-block:: sh

    beet audiomuse_get_embedding album:"Discovery" --pretend

**Resolve Item IDs for Library**

Resolve item IDs for all tracks in your library:

.. code-block:: sh

    beet audiomuse_match

Resolve item IDs for tracks missing this field:

.. code-block:: sh

    beet audiomuse_match ^audiomuse_item_id:

**Fetch Embeddings and Scores**

Fetch embeddings for all tracks with item IDs:

.. code-block:: sh

    beet audiomuse_get_embedding audiomuse_item_id::

Fetch score metadata for a specific album:

.. code-block:: sh

    beet audiomuse_get_score album:"Discovery"

**Advanced Querying with Flexible Fields**

After fetching scores, you can query tracks using the stored audio features:

.. code-block:: sh

    # High-energy tracks
    beet ls audiomuse_energy:0.8..1.0

    # Fast tempo tracks (140+ BPM)
    beet ls audiomuse_tempo:140..

    # Tracks in C major
    beet ls audiomuse_key:C audiomuse_scale:major

    # High valence (positive mood) tracks
    beet ls audiomuse_valence:0.7..1.0

    # Create a smart playlist of danceable tracks
    beet ls -p audiomuse_danceability:0.8.. audiomuse_tempo:120..140 > danceable.m3u

**Find Similar Tracks**

Discover tracks similar to a specific song:

.. code-block:: sh

    beet audiomuse_similar title:"Voyager" artist:"Daft Punk" -n 10

Find similar tracks for your entire library (useful for building recommendation
data):

.. code-block:: sh

    beet audiomuse_similar audiomuse_item_id:: -n 50

Build a playlist of similar tracks from a starting song:

.. code-block:: sh

    # Find song ID first
    beet ls -f '$id $title - $artist' title:"Get Lucky"
    # Find 30 similar tracks
    beet audiomuse_similar id:12345 -n 30
