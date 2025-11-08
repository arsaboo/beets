AudioMuse
=========

The ``audiomuse`` plugin enriches your music library with AI-powered audio
analysis. It retrieves and stores a track identifier, a semantic embedding
vector, and a set of audio feature fields (energy, tempo, key, scale, and
flexible mood/feature scores) that you can query in beets.

Requirements
------------

This plugin expects a running AudioMuse service reachable from your beets host.
Ensure the configured URL is accessible before using the commands; otherwise
requests will be skipped and logged at debug level.

Setup
-----

Enable the plugin in your :doc:`beets configuration </reference/config>`:

.. code-block:: yaml

    plugins: audiomuse

Configuration
-------------

Add an ``audiomuse`` section to point at your AudioMuse server:

.. code-block:: yaml

    audiomuse:
      url: "http://192.168.2.162:8001"  # Base URL of your AudioMuse service

If omitted, the plugin defaults to ``http://127.0.0.1:8001``.

Commands
--------

Resolve and Store Item IDs
~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: sh

    beet audiomuse_match QUERY [-p]

Resolves and stores ``audiomuse_item_id`` for each selected item by searching
the configured service using the item's title and artist. Matching is tolerant
to common formatting differences (extra spaces, featuring separators, etc.).
Matched item IDs are stored in your beets database.

Stored field:

- ``audiomuse_item_id`` – Unique identifier for the track on the AudioMuse
  service.

Options
+++++++

- ``-p``, ``--pretend``: Preview matches without storing to the database. Useful
  for testing the matching logic before committing changes.

Fetch Embeddings
~~~~~~~~~~~~~~~~

.. code-block:: sh

    beet audiomuse_get_embedding QUERY [-p]

For each item with an ``audiomuse_item_id``, fetches the audio embedding vector
from the service and stores it as JSON in ``audiomuse_embedding`` for similarity
and analysis workflows.

Stored field:

- ``audiomuse_embedding`` – JSON array (list of floats) representing the
  semantic audio embedding used for similarity comparisons.

Options
+++++++

- ``-p``, ``--pretend``: Preview embeddings without storing to the database.
  Shows the dimension count without saving data.

Fetch Score Metadata
~~~~~~~~~~~~~~~~~~~~

.. code-block:: sh

    beet audiomuse_get_score QUERY [-p]

For each item with an ``audiomuse_item_id``, fetches comprehensive audio
analysis metadata from the service and stores core and dynamic feature fields.

Stored fields:

- ``audiomuse_energy`` – Float intensity/activity estimate.
- ``audiomuse_tempo`` – Float BPM estimate.
- ``audiomuse_key`` – Detected key (e.g. ``C``, ``F#``).
- ``audiomuse_scale`` – Mode (e.g. ``major``, ``minor``).
- ``audiomuse_mood_vector`` – Original comma-separated mood labels string.
- ``audiomuse_other_features`` – Comma-separated ``label:value`` pairs for
  additional audio features (e.g., ``"speechiness:0.1,instrumentalness:0.95"``).
- ``audiomuse_mood_<label>`` – Parsed per-mood scores (e.g.
  ``audiomuse_mood_valence``).
- ``audiomuse_<label>`` – Parsed per-feature scores (e.g.
  ``audiomuse_danceability``).

Parsing notes:

- Labels are lowercased and slugified; numeric values stored as floats when
  possible.
- Dynamic labels depend on what the service returns; you can query them directly
  once stored.

Options
+++++++

- ``-p``, ``--pretend``: Preview score data without storing to the database.
  Shows field names and values that would be stored.

Find Similar Tracks
~~~~~~~~~~~~~~~~~~~

.. code-block:: sh

    beet audiomuse_similar QUERY [-n COUNT]

Finds similar tracks using embedding-based similarity search provided by the
service. For each item with an ``audiomuse_item_id``, it retrieves tracks that
sound alike.

This command does not store new fields; it only lists similar tracks based on
the existing ``audiomuse_embedding`` (or server-side similarity).

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
