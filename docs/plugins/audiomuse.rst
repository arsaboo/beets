AudioMuse
=========

The ``audiomuse`` plugin integrates with an AudioMuse-AI server to look up
tracks by title and artist and retrieve the media server's ``item_id``.

The plugin adds a command that can search AudioMuse for items matching your
beets library query and optionally store the returned ``item_id`` in a flexible
field named ``audiomuse_item_id`` on each matched track.

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
      url: "http://192.168.2.162:8001"  # Base URL of your AudioMuse server

If omitted, the plugin defaults to ``http://127.0.0.1:8001``.

Command
-------

.. code-block:: sh

    beet audiomusesearch QUERY [--set] [--write]

Searches the AudioMuse endpoint ``/api/search_tracks`` with the beets item's
``title`` and ``artist``. AudioMuse uses the field name ``author`` for artist
internally; the plugin maps beets' ``artist`` to ``author`` automatically.

Options
~~~~~~~

- ``--set``: Store the returned ``item_id`` to the flexible field
  ``audiomuse_item_id`` on the matched item(s).
- ``--write``: After setting the field, also write tags to the file(s) if your
  configuration allows writing.

Examples
--------

- Find ``item_id`` for all tracks by a specific artist and store the value:

  .. code-block:: sh

      beet audiomusesearch artist:"Daft Punk" --set

- Preview matches for a single track without storing any field:

  .. code-block:: sh

      beet audiomusesearch title:"Voyager" artist:"Daft Punk"

Notes
-----

- The current implementation performs a simple search using ``title`` and
  ``artist`` and picks the best match (exact, case-insensitive on both fields
  when available, otherwise the first result).
- Additional endpoints (e.g., similar tracks, playlist creation) may be
  supported in future versions.
