#!/usr/bin/env python
# -*- coding: utf-8 -*-
# services - Waqas Bhatti (wbhatti@astro.princeton.edu) - Oct 2017
# License: MIT. See the LICENSE file for more details.
'''This contains various modules to query online data services. These are not
exhaustive and are meant to support other astrobase modules.

- :py:mod:`astrobase.services.dust`: interface to the 2MASS DUST
  extinction/emission service.

- :py:mod:`astrobase.services.gaia`: interface to the GAIA TAP+ ADQL query
  service.

- :py:mod:`astrobase.services.lccs`: interface to the `LCC-Server
  <https://github.com/waqasbhatti/lcc-server>`_ API.

- :py:mod:`astrobase.services.mast`: interface to the MAST catalogs at STScI and
  the TESS Input Catalog in particular.

- :py:mod:`astrobase.services.simbad`: interface to the CDS SIMBAD service.

- :py:mod:`astrobase.services.skyview`: interface to the NASA SkyView
  finder-chart and cutout service.

- :py:mod:`astrobase.services.trilegal`: interface to the Girardi TRILEGAL
  galaxy model forms and service.

For a much broader interface to online data services, use the astroquery package
by A. Ginsburg, B. Sipocz, et al.:

http://astroquery.readthedocs.io

'''
