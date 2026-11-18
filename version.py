# -*- coding: utf-8 -*-
"""Single source of truth for the application version.

Every place that needs a version string imports APP_VERSION from here:
  - installer.py  (DisplayVersion, install info)
  - server.py     (/api/status -> UI sidebar)
Bump this once to release a new version.
"""
APP_VERSION = "1.0.0"
