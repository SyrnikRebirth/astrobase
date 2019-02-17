#!/usr/bin/env python
# -*- coding: utf-8 -*-

'''pkl_utils.py - Waqas Bhatti (wbhatti@astro.princeton.edu) - Feb 2019
License: MIT.

This contains utility functions that support checkplot.pkl public functions.

'''

#############
## LOGGING ##
#############

import logging
from astrobase import log_sub, log_fmt, log_date_fmt

DEBUG = False
if DEBUG:
    level = logging.DEBUG
else:
    level = logging.INFO
LOGGER = logging.getLogger(__name__)
logging.basicConfig(
    level=level,
    style=log_sub,
    format=log_fmt,
    datefmt=log_date_fmt,
)

LOGDEBUG = LOGGER.debug
LOGINFO = LOGGER.info
LOGWARNING = LOGGER.warning
LOGERROR = LOGGER.error
LOGEXCEPTION = LOGGER.exception



#############
## IMPORTS ##
#############

import os
import os.path
import gzip
import base64
import json

try:
    import cPickle as pickle
    from cStringIO import StringIO as StrIO
except Exception as e:
    import pickle
    from io import BytesIO as StrIO

import numpy as np
from numpy import min as npmin, max as npmax, abs as npabs

# we're going to plot using Agg only
import matplotlib
MPLVERSION = tuple([int(x) for x in matplotlib.__version__.split('.')])
matplotlib.use('Agg')

import matplotlib.pyplot as plt
from mpl_toolkits.axes_grid1.inset_locator import inset_axes

# import this to get neighbors and their x,y coords from the Skyview FITS
from astropy.wcs import WCS



###################
## LOCAL IMPORTS ##
###################

from ..lcmath import phase_magseries, phase_bin_magseries
from ..varbase.lcfit import spline_fit_magseries, savgol_fit_magseries

from ..varclass.starfeatures import coord_features, color_features, \
    color_classification, neighbor_gaia_features

from ..plotbase import skyview_stamp, \
    PLOTYLABELS, METHODLABELS, METHODSHORTLABELS

from ..services.mast import tic_conesearch
from .. import magnitudes



########################################
## PICKLE CHECKPLOT UTILITY FUNCTIONS ##
########################################

def _xyzdist_to_distarcsec(xyzdist):
    '''
    This just inverts the xyz unit vector distance -> angular distance relation.

    '''

    return np.degrees(2.0*np.arcsin(xyzdist/2.0))*3600.0



def _pkl_finder_objectinfo(objectinfo,
                           varinfo,
                           findercmap,
                           finderconvolve,
                           sigclip,
                           normto,
                           normmingap,
                           deredden_object=True,
                           custom_bandpasses=None,
                           lclistpkl=None,
                           nbrradiusarcsec=30.0,
                           maxnumneighbors=5,
                           plotdpi=100,
                           findercachedir='~/.astrobase/stamp-cache',
                           verbose=True,
                           gaia_submit_timeout=10.0,
                           gaia_submit_tries=3,
                           gaia_max_timeout=180.0,
                           gaia_mirror=None,
                           fast_mode=False,
                           complete_query_later=True):
    '''This returns the finder chart and object information as a dict.

    '''

    # optional mode to hit external services and fail fast if they timeout
    if fast_mode is True:
        skyview_lookup = False
        skyview_timeout = 10.0
        skyview_retry_failed = False
        dust_timeout = 10.0
        gaia_submit_timeout = 7.0
        gaia_max_timeout = 10.0
        gaia_submit_tries = 2
        complete_query_later = False
        search_simbad = False

    elif isinstance(fast_mode, (int, float)) and fast_mode > 0.0:
        skyview_lookup = True
        skyview_timeout = fast_mode
        skyview_retry_failed = False
        dust_timeout = fast_mode
        gaia_submit_timeout = 0.66*fast_mode
        gaia_max_timeout = fast_mode
        gaia_submit_tries = 2
        complete_query_later = False
        search_simbad = False

    else:
        skyview_lookup = True
        skyview_timeout = 10.0
        skyview_retry_failed = True
        dust_timeout = 10.0
        search_simbad = True


    if (isinstance(objectinfo, dict) and
        ('objectid' in objectinfo or 'hatid' in objectinfo) and
        'ra' in objectinfo and 'decl' in objectinfo and
        objectinfo['ra'] and objectinfo['decl']):

        if 'objectid' not in objectinfo:
            objectid = objectinfo['hatid']
        else:
            objectid = objectinfo['objectid']

        if verbose and skyview_lookup:
            LOGINFO('adding in object information and '
                    'finder chart for %s at RA: %.3f, DEC: %.3f' %
                    (objectid, objectinfo['ra'], objectinfo['decl']))
        elif verbose and not skyview_lookup:
            LOGINFO('adding in object information '
                    'for %s at RA: %.3f, DEC: %.3f. '
                    'skipping finder chart because skyview_lookup = False' %
                    (objectid, objectinfo['ra'], objectinfo['decl']))

        # get the finder chart
        try:

            if skyview_lookup:

                try:

                    # generate the finder chart
                    finder, finderheader = skyview_stamp(
                        objectinfo['ra'],
                        objectinfo['decl'],
                        convolvewith=finderconvolve,
                        verbose=verbose,
                        flip=False,
                        cachedir=findercachedir,
                        timeout=skyview_timeout,
                        retry_failed=skyview_retry_failed,
                    )

                except OSError as e:

                    if not fast_mode:

                        LOGERROR(
                            'finder image appears to be corrupt, retrying...'
                        )

                        # generate the finder chart
                        finder, finderheader = skyview_stamp(
                            objectinfo['ra'],
                            objectinfo['decl'],
                            convolvewith=finderconvolve,
                            verbose=verbose,
                            flip=False,
                            cachedir=findercachedir,
                            forcefetch=True,
                            timeout=skyview_timeout,
                            retry_failed=False  # do not start an infinite loop
                        )


                finderfig = plt.figure(figsize=(3,3),dpi=plotdpi)

                # initialize the finder WCS
                finderwcs = WCS(finderheader)

                # use the WCS transform for the plot
                ax = finderfig.add_subplot(111, frameon=False)
                ax.imshow(finder, cmap=findercmap, origin='lower')

            else:
                finder, finderheader, finderfig, finderwcs = (
                    None, None, None, None
                )

            # skip down to after nbr stuff for the rest of the finderchart...

            # search around the target's location and get its neighbors if
            # lclistpkl is provided and it exists
            if (lclistpkl is not None and
                nbrradiusarcsec is not None and
                nbrradiusarcsec > 0.0):

                # if lclistpkl is a string, open it as a pickle
                if isinstance(lclistpkl, str) and os.path.exists(lclistpkl):

                    if lclistpkl.endswith('.gz'):
                        infd = gzip.open(lclistpkl,'rb')
                    else:
                        infd = open(lclistpkl,'rb')

                    lclist = pickle.load(infd)
                    infd.close()

                # otherwise, if it's a dict, we get it directly
                elif isinstance(lclistpkl, dict):

                    lclist = lclistpkl

                # finally, if it's nothing we recognize, ignore it
                else:

                    LOGERROR('could not understand lclistpkl kwarg, '
                             'not getting neighbor info')

                    lclist = dict()

                # check if we have a KDTree to use
                # if we don't, skip neighbor stuff
                if 'kdtree' not in lclist:

                    LOGERROR('neighbors within %.1f arcsec for %s could '
                             'not be found, no kdtree in lclistpkl: %s'
                             % (objectid, lclistpkl))
                    neighbors = None
                    kdt = None

                # otherwise, do neighbor processing
                else:

                    kdt = lclist['kdtree']

                    obj_cosdecl = np.cos(np.radians(objectinfo['decl']))
                    obj_sindecl = np.sin(np.radians(objectinfo['decl']))
                    obj_cosra = np.cos(np.radians(objectinfo['ra']))
                    obj_sinra = np.sin(np.radians(objectinfo['ra']))

                    obj_xyz = np.column_stack((obj_cosra*obj_cosdecl,
                                               obj_sinra*obj_cosdecl,
                                               obj_sindecl))
                    match_xyzdist = (
                        2.0 * np.sin(np.radians(nbrradiusarcsec/3600.0)/2.0)
                    )
                    matchdists, matchinds = kdt.query(
                        obj_xyz,
                        k=maxnumneighbors+1,  # get maxnumneighbors + tgt
                        distance_upper_bound=match_xyzdist
                    )

                    # sort by matchdist
                    mdsorted = np.argsort(matchdists[0])
                    matchdists = matchdists[0][mdsorted]
                    matchinds = matchinds[0][mdsorted]

                    # luckily, the indices to the kdtree are the same as that
                    # for the objects (I think)
                    neighbors = []

                    nbrind = 0

                    for md, mi in zip(matchdists, matchinds):

                        if np.isfinite(md) and md > 0.0:

                            if skyview_lookup:

                                # generate the xy for the finder we'll use a
                                # HTML5 canvas and these pixcoords to highlight
                                # each neighbor when we mouse over its row in
                                # the neighbors tab

                                # we use coord origin = 0 here and not the usual
                                # 1 because we're annotating a numpy array
                                pixcoords = finderwcs.all_world2pix(
                                    np.array([[lclist['objects']['ra'][mi],
                                               lclist['objects']['decl'][mi]]]),
                                    0
                                )

                                # each elem is {'objectid',
                                #               'ra','decl',
                                #               'xpix','ypix',
                                #               'dist','lcfpath'}
                                thisnbr = {
                                    'objectid':(
                                        lclist['objects']['objectid'][mi]
                                    ),
                                    'ra':lclist['objects']['ra'][mi],
                                    'decl':lclist['objects']['decl'][mi],
                                    'xpix':pixcoords[0,0],
                                    'ypix':300.0 - pixcoords[0,1],
                                    'dist':_xyzdist_to_distarcsec(md),
                                    'lcfpath': lclist['objects']['lcfname'][mi]
                                }
                                neighbors.append(thisnbr)
                                nbrind = nbrind+1

                                # put in a nice marker for this neighbor into
                                # the overall finder chart
                                annotatex = pixcoords[0,0]
                                annotatey = pixcoords[0,1]

                                if ((300.0 - annotatex) > 50.0):
                                    offx = annotatex + 30.0
                                    xha = 'center'
                                else:
                                    offx = annotatex - 30.0
                                    xha = 'center'
                                if ((300.0 - annotatey) > 50.0):
                                    offy = annotatey - 30.0
                                    yha = 'center'
                                else:
                                    offy = annotatey + 30.0
                                    yha = 'center'

                                ax.annotate('N%s' % nbrind,
                                            (annotatex, annotatey),
                                            xytext=(offx, offy),
                                            arrowprops={'facecolor':'blue',
                                                        'edgecolor':'blue',
                                                        'width':1.0,
                                                        'headwidth':1.0,
                                                        'headlength':0.1,
                                                        'shrink':0.0},
                                            color='blue',
                                            horizontalalignment=xha,
                                            verticalalignment=yha)

                            else:

                                thisnbr = {
                                    'objectid':(
                                        lclist['objects']['objectid'][mi]
                                    ),
                                    'ra':lclist['objects']['ra'][mi],
                                    'decl':lclist['objects']['decl'][mi],
                                    'xpix':0.0,
                                    'ypix':0.0,
                                    'dist':_xyzdist_to_distarcsec(md),
                                    'lcfpath': lclist['objects']['lcfname'][mi]
                                }
                                neighbors.append(thisnbr)
                                nbrind = nbrind+1

            # if there are no neighbors, set the 'neighbors' key to None
            else:

                neighbors = None
                kdt = None

            if skyview_lookup:

                #
                # finish up the finder chart after neighbors are processed
                #
                ax.set_xticks([])
                ax.set_yticks([])

                # add a reticle pointing to the object's coordinates
                # we use coord origin = 0 here and not the usual
                # 1 because we're annotating a numpy array
                object_pixcoords = finderwcs.all_world2pix(
                    [[objectinfo['ra'],
                      objectinfo['decl']]],
                    0
                )

                ax.axvline(
                    # x=150.0,
                    x=object_pixcoords[0,0],
                    ymin=0.375,
                    ymax=0.45,
                    linewidth=1,
                    color='b'
                )
                ax.axhline(
                    # y=150.0,
                    y=object_pixcoords[0,1],
                    xmin=0.375,
                    xmax=0.45,
                    linewidth=1,
                    color='b'
                )
                ax.set_frame_on(False)

                # this is the output instance
                finderpng = StrIO()
                finderfig.savefig(finderpng,
                                  bbox_inches='tight',
                                  pad_inches=0.0, format='png')
                plt.close()

                # encode the finderpng instance to base64
                finderpng.seek(0)
                finderb64 = base64.b64encode(finderpng.read())

                # close the stringio buffer
                finderpng.close()

            else:

                finderb64 = None

        except Exception as e:

            LOGEXCEPTION('could not fetch a DSS stamp for this '
                         'object %s using coords (%.3f,%.3f)' %
                         (objectid, objectinfo['ra'], objectinfo['decl']))
            finderb64 = None
            neighbors = None
            kdt = None

    # if we don't have ra, dec info, then everything is none up to this point
    else:

        finderb64 = None
        neighbors = None
        kdt = None

    #
    # end of finder chart operations
    #

    # now that we have the finder chart, get the rest of the object
    # information

    # get the rest of the features, these don't necessarily rely on ra, dec and
    # should degrade gracefully if these aren't provided
    if isinstance(objectinfo, dict):

        if 'objectid' not in objectinfo and 'hatid' in objectinfo:
            objectid = objectinfo['hatid']
            objectinfo['objectid'] = objectid
        elif 'objectid' in objectinfo:
            objectid = objectinfo['objectid']
        else:
            objectid = os.urandom(12).hex()[:7]
            objectinfo['objectid'] = objectid
            LOGWARNING('no objectid found in objectinfo dict, '
                       'making up a random one: %s')


        # get the neighbor features and GAIA info
        nbrfeat = neighbor_gaia_features(
            objectinfo,
            kdt,
            nbrradiusarcsec,
            verbose=False,
            gaia_submit_timeout=gaia_submit_timeout,
            gaia_submit_tries=gaia_submit_tries,
            gaia_max_timeout=gaia_max_timeout,
            gaia_mirror=gaia_mirror,
            complete_query_later=complete_query_later,
            search_simbad=search_simbad
        )
        objectinfo.update(nbrfeat)

        # see if the objectinfo dict has pmra/pmdecl entries.  if it doesn't,
        # then we'll see if the nbrfeat dict has pmra/pmdecl from GAIA. we'll
        # set the appropriate provenance keys as well so we know where the PM
        # came from
        if ( ('pmra' not in objectinfo) or
             ( ('pmra' in objectinfo) and
               ( (objectinfo['pmra'] is None) or
                 (not np.isfinite(objectinfo['pmra'])) ) ) ):

            if 'ok' in nbrfeat['gaia_status']:

                objectinfo['pmra'] = nbrfeat['gaia_pmras'][0]
                objectinfo['pmra_err'] = nbrfeat['gaia_pmra_errs'][0]
                objectinfo['pmra_source'] = 'gaia'

                if verbose:
                    LOGWARNING('pmRA not found in provided objectinfo dict, '
                               'using value from GAIA')

        else:
            objectinfo['pmra_source'] = 'light curve'

        if ( ('pmdecl' not in objectinfo) or
             ( ('pmdecl' in objectinfo) and
               ( (objectinfo['pmdecl'] is None) or
                 (not np.isfinite(objectinfo['pmdecl'])) ) ) ):

            if 'ok' in nbrfeat['gaia_status']:

                objectinfo['pmdecl'] = nbrfeat['gaia_pmdecls'][0]
                objectinfo['pmdecl_err'] = nbrfeat['gaia_pmdecl_errs'][0]
                objectinfo['pmdecl_source'] = 'gaia'

                if verbose:
                    LOGWARNING('pmDEC not found in provided objectinfo dict, '
                               'using value from GAIA')

        else:
            objectinfo['pmdecl_source'] = 'light curve'

        #
        # update GAIA info so it's available at the first level
        #
        if 'ok' in objectinfo['gaia_status']:
            objectinfo['gaiaid'] = objectinfo['gaia_ids'][0]
            objectinfo['gaiamag'] = objectinfo['gaia_mags'][0]
            objectinfo['gaia_absmag'] = objectinfo['gaia_absolute_mags'][0]
            objectinfo['gaia_parallax'] = objectinfo['gaia_parallaxes'][0]
            objectinfo['gaia_parallax_err'] = (
                objectinfo['gaia_parallax_errs'][0]
            )
            objectinfo['gaia_pmra'] = objectinfo['gaia_pmras'][0]
            objectinfo['gaia_pmra_err'] = objectinfo['gaia_pmra_errs'][0]
            objectinfo['gaia_pmdecl'] = objectinfo['gaia_pmdecls'][0]
            objectinfo['gaia_pmdecl_err'] = objectinfo['gaia_pmdecl_errs'][0]

        else:
            objectinfo['gaiaid'] = None
            objectinfo['gaiamag'] = np.nan
            objectinfo['gaia_absmag'] = np.nan
            objectinfo['gaia_parallax'] = np.nan
            objectinfo['gaia_parallax_err'] = np.nan
            objectinfo['gaia_pmra'] = np.nan
            objectinfo['gaia_pmra_err'] = np.nan
            objectinfo['gaia_pmdecl'] = np.nan
            objectinfo['gaia_pmdecl_err'] = np.nan

        #
        # get the object's TIC information
        #
        if ('ra' in objectinfo and
            objectinfo['ra'] is not None and
            np.isfinite(objectinfo['ra']) and
            'decl' in objectinfo and
            objectinfo['decl'] is not None and
            np.isfinite(objectinfo['decl'])):

            try:
                ticres = tic_conesearch(objectinfo['ra'],
                                        objectinfo['decl'],
                                        radius_arcmin=5.0/60.0,
                                        verbose=verbose,
                                        timeout=gaia_max_timeout,
                                        maxtries=gaia_submit_tries)

                if ticres is not None:

                    with open(ticres['cachefname'],'r') as infd:
                        ticinfo = json.load(infd)

                    if ('data' in ticinfo and
                        len(ticinfo['data']) > 0 and
                        isinstance(ticinfo['data'][0], dict)):

                        objectinfo['ticid'] = str(ticinfo['data'][0]['ID'])
                        objectinfo['tessmag'] = ticinfo['data'][0]['Tmag']
                        objectinfo['tic_version'] = (
                            ticinfo['data'][0]['version']
                        )
                        objectinfo['tic_distarcsec'] = (
                            ticinfo['data'][0]['dstArcSec']
                        )
                        objectinfo['tessmag_origin'] = (
                            ticinfo['data'][0]['TESSflag']
                        )

                        objectinfo['tic_starprop_origin'] = (
                            ticinfo['data'][0]['SPFlag']
                        )
                        objectinfo['tic_lumclass'] = (
                            ticinfo['data'][0]['lumclass']
                        )
                        objectinfo['tic_teff'] = (
                            ticinfo['data'][0]['Teff']
                        )
                        objectinfo['tic_teff_err'] = (
                            ticinfo['data'][0]['e_Teff']
                        )
                        objectinfo['tic_logg'] = (
                            ticinfo['data'][0]['logg']
                        )
                        objectinfo['tic_logg_err'] = (
                            ticinfo['data'][0]['e_logg']
                        )
                        objectinfo['tic_mh'] = (
                            ticinfo['data'][0]['MH']
                        )
                        objectinfo['tic_mh_err'] = (
                            ticinfo['data'][0]['e_MH']
                        )
                        objectinfo['tic_radius'] = (
                            ticinfo['data'][0]['rad']
                        )
                        objectinfo['tic_radius_err'] = (
                            ticinfo['data'][0]['e_rad']
                        )
                        objectinfo['tic_mass'] = (
                            ticinfo['data'][0]['mass']
                        )
                        objectinfo['tic_mass_err'] = (
                            ticinfo['data'][0]['e_mass']
                        )
                        objectinfo['tic_density'] = (
                            ticinfo['data'][0]['rho']
                        )
                        objectinfo['tic_density_err'] = (
                            ticinfo['data'][0]['e_rho']
                        )
                        objectinfo['tic_luminosity'] = (
                            ticinfo['data'][0]['lum']
                        )
                        objectinfo['tic_luminosity_err'] = (
                            ticinfo['data'][0]['e_lum']
                        )
                        objectinfo['tic_distancepc'] = (
                            ticinfo['data'][0]['d']
                        )
                        objectinfo['tic_distancepc_err'] = (
                            ticinfo['data'][0]['e_d']
                        )

                        #
                        # fill in any missing info using the TIC entry
                        #
                        if ('gaiaid' not in objectinfo or
                            ('gaiaid' in objectinfo and
                             (objectinfo['gaiaid'] is None))):
                            objectinfo['gaiaid'] = ticinfo['data'][0]['GAIA']

                        if ('gaiamag' not in objectinfo or
                            ('gaiamag' in objectinfo and
                             (objectinfo['gaiamag'] is None or
                              not np.isfinite(objectinfo['gaiamag'])))):
                            objectinfo['gaiamag'] = (
                                ticinfo['data'][0]['GAIAmag']
                            )
                            objectinfo['gaiamag_err'] = (
                                ticinfo['data'][0]['e_GAIAmag']
                            )

                        if ('gaia_parallax' not in objectinfo or
                            ('gaia_parallax' in objectinfo and
                             (objectinfo['gaia_parallax'] is None or
                              not np.isfinite(objectinfo['gaia_parallax'])))):

                            objectinfo['gaia_parallax'] = (
                                ticinfo['data'][0]['plx']
                            )
                            objectinfo['gaia_parallax_err'] = (
                                ticinfo['data'][0]['e_plx']
                            )

                            if (objectinfo['gaiamag'] is not None and
                                np.isfinite(objectinfo['gaiamag']) and
                                objectinfo['gaia_parallax'] is not None and
                                np.isfinite(objectinfo['gaia_parallax'])):

                                objectinfo['gaia_absmag'] = (
                                    magnitudes.absolute_gaia_magnitude(
                                        objectinfo['gaiamag'],
                                        objectinfo['gaia_parallax']
                                    )
                                )

                        if ('pmra' not in objectinfo or
                            ('pmra' in objectinfo and
                             (objectinfo['pmra'] is None or
                              not np.isfinite(objectinfo['pmra'])))):
                            objectinfo['pmra'] = ticinfo['data'][0]['pmRA']
                            objectinfo['pmra_err'] = (
                                ticinfo['data'][0]['e_pmRA']
                            )
                            objectinfo['pmra_source'] = 'TIC'

                        if ('pmdecl' not in objectinfo or
                            ('pmdecl' in objectinfo and
                             (objectinfo['pmdecl'] is None or
                              not np.isfinite(objectinfo['pmdecl'])))):
                            objectinfo['pmdecl'] = ticinfo['data'][0]['pmDEC']
                            objectinfo['pmdecl_err'] = (
                                ticinfo['data'][0]['e_pmDEC']
                            )
                            objectinfo['pmdecl_source'] = 'TIC'

                        if ('bmag' not in objectinfo or
                            ('bmag' in objectinfo and
                             (objectinfo['bmag'] is None or
                              not np.isfinite(objectinfo['bmag'])))):
                            objectinfo['bmag'] = ticinfo['data'][0]['Bmag']
                            objectinfo['bmag_err'] = (
                                ticinfo['data'][0]['e_Bmag']
                            )

                        if ('vmag' not in objectinfo or
                            ('vmag' in objectinfo and
                             (objectinfo['vmag'] is None or
                              not np.isfinite(objectinfo['vmag'])))):
                            objectinfo['vmag'] = ticinfo['data'][0]['Vmag']
                            objectinfo['vmag_err'] = (
                                ticinfo['data'][0]['e_Vmag']
                            )

                        if ('sdssu' not in objectinfo or
                            ('sdssu' in objectinfo and
                             (objectinfo['sdssu'] is None or
                              not np.isfinite(objectinfo['sdssu'])))):
                            objectinfo['sdssu'] = ticinfo['data'][0]['umag']
                            objectinfo['sdssu_err'] = (
                                ticinfo['data'][0]['e_umag']
                            )

                        if ('sdssg' not in objectinfo or
                            ('sdssg' in objectinfo and
                             (objectinfo['sdssg'] is None or
                              not np.isfinite(objectinfo['sdssg'])))):
                            objectinfo['sdssg'] = ticinfo['data'][0]['gmag']
                            objectinfo['sdssg_err'] = (
                                ticinfo['data'][0]['e_gmag']
                            )

                        if ('sdssr' not in objectinfo or
                            ('sdssr' in objectinfo and
                             (objectinfo['sdssr'] is None or
                              not np.isfinite(objectinfo['sdssr'])))):
                            objectinfo['sdssr'] = ticinfo['data'][0]['rmag']
                            objectinfo['sdssr_err'] = (
                                ticinfo['data'][0]['e_rmag']
                            )

                        if ('sdssi' not in objectinfo or
                            ('sdssi' in objectinfo and
                             (objectinfo['sdssi'] is None or
                              not np.isfinite(objectinfo['sdssi'])))):
                            objectinfo['sdssi'] = ticinfo['data'][0]['imag']
                            objectinfo['sdssi_err'] = (
                                ticinfo['data'][0]['e_imag']
                            )

                        if ('sdssz' not in objectinfo or
                            ('sdssz' in objectinfo and
                             (objectinfo['sdssz'] is None or
                              not np.isfinite(objectinfo['sdssz'])))):
                            objectinfo['sdssz'] = ticinfo['data'][0]['zmag']
                            objectinfo['sdssz_err'] = (
                                ticinfo['data'][0]['e_zmag']
                            )

                        if ('jmag' not in objectinfo or
                            ('jmag' in objectinfo and
                             (objectinfo['jmag'] is None or
                              not np.isfinite(objectinfo['jmag'])))):
                            objectinfo['jmag'] = ticinfo['data'][0]['Jmag']
                            objectinfo['jmag_err'] = (
                                ticinfo['data'][0]['e_Jmag']
                            )

                        if ('hmag' not in objectinfo or
                            ('hmag' in objectinfo and
                             (objectinfo['hmag'] is None or
                              not np.isfinite(objectinfo['hmag'])))):
                            objectinfo['hmag'] = ticinfo['data'][0]['Hmag']
                            objectinfo['hmag_err'] = (
                                ticinfo['data'][0]['e_Hmag']
                            )

                        if ('kmag' not in objectinfo or
                            ('kmag' in objectinfo and
                             (objectinfo['kmag'] is None or
                              not np.isfinite(objectinfo['kmag'])))):
                            objectinfo['kmag'] = ticinfo['data'][0]['Kmag']
                            objectinfo['kmag_err'] = (
                                ticinfo['data'][0]['e_Kmag']
                            )

                        if ('wise1' not in objectinfo or
                            ('wise1' in objectinfo and
                             (objectinfo['wise1'] is None or
                              not np.isfinite(objectinfo['wise1'])))):
                            objectinfo['wise1'] = ticinfo['data'][0]['w1mag']
                            objectinfo['wise1_err'] = (
                                ticinfo['data'][0]['e_w1mag']
                            )

                        if ('wise2' not in objectinfo or
                            ('wise2' in objectinfo and
                             (objectinfo['wise2'] is None or
                              not np.isfinite(objectinfo['wise2'])))):
                            objectinfo['wise2'] = ticinfo['data'][0]['w2mag']
                            objectinfo['wise2_err'] = (
                                ticinfo['data'][0]['e_w2mag']
                            )

                        if ('wise3' not in objectinfo or
                            ('wise3' in objectinfo and
                             (objectinfo['wise3'] is None or
                              not np.isfinite(objectinfo['wise3'])))):
                            objectinfo['wise3'] = ticinfo['data'][0]['w3mag']
                            objectinfo['wise3_err'] = (
                                ticinfo['data'][0]['e_w3mag']
                            )

                        if ('wise4' not in objectinfo or
                            ('wise4' in objectinfo and
                             (objectinfo['wise4'] is None or
                              not np.isfinite(objectinfo['wise4'])))):
                            objectinfo['wise4'] = ticinfo['data'][0]['w4mag']
                            objectinfo['wise4_err'] = (
                                ticinfo['data'][0]['e_w4mag']
                            )

                else:
                    LOGERROR('could not look up TIC '
                             'information for object: %s '
                             'at (%.3f, %.3f)' %
                             (objectinfo['objectid'],
                              objectinfo['ra'],
                              objectinfo['decl']))

            except Exception as e:

                LOGEXCEPTION('could not look up TIC '
                             'information for object: %s '
                             'at (%.3f, %.3f)' %
                             (objectinfo['objectid'],
                              objectinfo['ra'],
                              objectinfo['decl']))


        # try to get the object's coord features
        coordfeat = coord_features(objectinfo)

        # get the color features
        colorfeat = color_features(objectinfo,
                                   deredden=deredden_object,
                                   custom_bandpasses=custom_bandpasses,
                                   dust_timeout=dust_timeout)

        # get the object's color classification
        colorclass = color_classification(colorfeat, coordfeat)

        # update the objectinfo dict with everything
        objectinfo.update(colorfeat)
        objectinfo.update(coordfeat)
        objectinfo.update(colorclass)

        # put together the initial checkplot pickle dictionary
        # this will be updated by the functions below as appropriate
        # and will written out as a gzipped pickle at the end of processing
        checkplotdict = {'objectid':objectid,
                         'neighbors':neighbors,
                         'objectinfo':objectinfo,
                         'finderchart':finderb64,
                         'sigclip':sigclip,
                         'normto':normto,
                         'normmingap':normmingap}

        # add the objecttags key to objectinfo
        checkplotdict['objectinfo']['objecttags'] = None

    # if there's no objectinfo, we can't do anything.
    else:

        # empty objectinfo dict
        checkplotdict = {'objectid':None,
                         'neighbors':None,
                         'objectinfo':{
                             'available_bands':[],
                             'available_band_labels':[],
                             'available_dereddened_bands':[],
                             'available_dereddened_band_labels':[],
                             'available_colors':[],
                             'available_color_labels':[],
                             'bmag':None,
                             'bmag-vmag':None,
                             'decl':None,
                             'hatid':None,
                             'hmag':None,
                             'imag-jmag':None,
                             'jmag-kmag':None,
                             'jmag':None,
                             'kmag':None,
                             'ndet':None,
                             'network':None,
                             'objecttags':None,
                             'pmdecl':None,
                             'pmdecl_err':None,
                             'pmra':None,
                             'pmra_err':None,
                             'propermotion':None,
                             'ra':None,
                             'rpmj':None,
                             'sdssg':None,
                             'sdssi':None,
                             'sdssr':None,
                             'stations':None,
                             'twomassid':None,
                             'ucac4id':None,
                             'vmag':None
                         },
                         'finderchart':None,
                         'sigclip':sigclip,
                         'normto':normto,
                         'normmingap':normmingap}

    # end of objectinfo processing

    # add the varinfo dict
    if isinstance(varinfo, dict):
        checkplotdict['varinfo'] = varinfo
    else:
        checkplotdict['varinfo'] = {
            'objectisvar':None,
            'vartags':None,
            'varisperiodic':None,
            'varperiod':None,
            'varepoch':None,
        }

    return checkplotdict



def _pkl_periodogram(lspinfo,
                     plotdpi=100,
                     override_pfmethod=None):
    '''This returns the periodogram plot PNG as base64, plus info as a dict.

    '''

    # get the appropriate plot ylabel
    pgramylabel = PLOTYLABELS[lspinfo['method']]

    # get the periods and lspvals from lspinfo
    periods = lspinfo['periods']
    lspvals = lspinfo['lspvals']
    bestperiod = lspinfo['bestperiod']
    nbestperiods = lspinfo['nbestperiods']
    nbestlspvals = lspinfo['nbestlspvals']

    # open the figure instance
    pgramfig = plt.figure(figsize=(7.5,4.8),dpi=plotdpi)

    # make the plot
    plt.plot(periods,lspvals)

    plt.xscale('log',basex=10)
    plt.xlabel('Period [days]')
    plt.ylabel(pgramylabel)
    plottitle = '%s - %.6f d' % (METHODLABELS[lspinfo['method']],
                                 bestperiod)
    plt.title(plottitle)

    # show the best five peaks on the plot
    for xbestperiod, xbestpeak in zip(nbestperiods,
                                      nbestlspvals):
        plt.annotate('%.6f' % xbestperiod,
                     xy=(xbestperiod, xbestpeak), xycoords='data',
                     xytext=(0.0,25.0), textcoords='offset points',
                     arrowprops=dict(arrowstyle="->"),fontsize='14.0')

    # make a grid
    plt.grid(color='#a9a9a9',
             alpha=0.9,
             zorder=0,
             linewidth=1.0,
             linestyle=':')

    # this is the output instance
    pgrampng = StrIO()
    pgramfig.savefig(pgrampng,
                     # bbox_inches='tight',
                     pad_inches=0.0, format='png')
    plt.close()

    # encode the finderpng instance to base64
    pgrampng.seek(0)
    pgramb64 = base64.b64encode(pgrampng.read())

    # close the stringio buffer
    pgrampng.close()

    if not override_pfmethod:

        # this is the dict to return
        checkplotdict = {
            lspinfo['method']:{
                'periods':periods,
                'lspvals':lspvals,
                'bestperiod':bestperiod,
                'nbestperiods':nbestperiods,
                'nbestlspvals':nbestlspvals,
                'periodogram':pgramb64,
            }
        }

    else:

        # this is the dict to return
        checkplotdict = {
            override_pfmethod:{
                'periods':periods,
                'lspvals':lspvals,
                'bestperiod':bestperiod,
                'nbestperiods':nbestperiods,
                'nbestlspvals':nbestlspvals,
                'periodogram':pgramb64,
            }
        }

    return checkplotdict



def _pkl_magseries_plot(stimes, smags, serrs,
                        plotdpi=100,
                        magsarefluxes=False):
    '''This returns the magseries plot PNG as base64, plus arrays as dict.

    '''

    scaledplottime = stimes - npmin(stimes)

    # open the figure instance
    magseriesfig = plt.figure(figsize=(7.5,4.8),dpi=plotdpi)

    plt.plot(scaledplottime,
             smags,
             marker='o',
             ms=2.0, ls='None',mew=0,
             color='green',
             rasterized=True)

    # flip y axis for mags
    if not magsarefluxes:
        plot_ylim = plt.ylim()
        plt.ylim((plot_ylim[1], plot_ylim[0]))

    # set the x axis limit
    plt.xlim((npmin(scaledplottime)-2.0,
              npmax(scaledplottime)+2.0))

    # make a grid
    plt.grid(color='#a9a9a9',
             alpha=0.9,
             zorder=0,
             linewidth=1.0,
             linestyle=':')

    # make the x and y axis labels
    plot_xlabel = 'JD - %.3f' % npmin(stimes)
    if magsarefluxes:
        plot_ylabel = 'flux'
    else:
        plot_ylabel = 'magnitude'

    plt.xlabel(plot_xlabel)
    plt.ylabel(plot_ylabel)

    # fix the yaxis ticks (turns off offset and uses the full
    # value of the yaxis tick)
    plt.gca().get_yaxis().get_major_formatter().set_useOffset(False)
    plt.gca().get_xaxis().get_major_formatter().set_useOffset(False)

    # this is the output instance
    magseriespng = StrIO()
    magseriesfig.savefig(magseriespng,
                         # bbox_inches='tight',
                         pad_inches=0.05, format='png')
    plt.close()

    # encode the finderpng instance to base64
    magseriespng.seek(0)
    magseriesb64 = base64.b64encode(magseriespng.read())

    # close the stringio buffer
    magseriespng.close()

    checkplotdict = {
        'magseries':{
            'plot':magseriesb64,
            'times':stimes,
            'mags':smags,
            'errs':serrs
        }
    }

    return checkplotdict



def _pkl_phased_magseries_plot(checkplotdict,
                               lspmethod,
                               periodind,
                               stimes, smags, serrs,
                               varperiod, varepoch,
                               lspmethodind=0,
                               phasewrap=True,
                               phasesort=True,
                               phasebin=0.002,
                               minbinelems=7,
                               plotxlim=(-0.8,0.8),
                               plotdpi=100,
                               bestperiodhighlight=None,
                               xgridlines=None,
                               xliminsetmode=False,
                               magsarefluxes=False,
                               directreturn=False,
                               overplotfit=None,
                               verbose=True,
                               override_pfmethod=None):
    '''This returns the phased magseries plot PNG as base64 plus info as a dict.

    checkplotdict is an existing checkplotdict to update. If it's None or
    directreturn = True, then the generated dict result for this magseries plot
    will be returned directly.

    lspmethod is a string indicating the type of period-finding algorithm that
    produced the period. If this is not in METHODSHORTLABELS, it will be used
    verbatim.

    periodind is the index of the period.

      If == 0  -> best period and bestperiodhighlight is applied if not None
      If > 0   -> some other peak of the periodogram
      If == -1 -> special mode w/ no periodogram labels and enabled highlight

    overplotfit is a result dict returned from one of the XXXX_fit_magseries
    functions in astrobase.varbase.lcfit. If this is not None, then the fit will
    be overplotted on the phased light curve plot.

    overplotfit must have the following structure and at least the keys below if
    not originally from one of these functions:

    {'fittype':<str: name of fit method>,
     'fitchisq':<float: the chi-squared value of the fit>,
     'fitredchisq':<float: the reduced chi-squared value of the fit>,
     'fitinfo':{'fitmags':<ndarray: model mags or fluxes from fit function>},
     'magseries':{'times':<ndarray: times at which the fitmags are evaluated>}}

    fitmags and times should all be of the same size. overplotfit is copied over
    to the checkplot dict for each specific phased LC plot to save all of this
    information.

    '''
    # open the figure instance
    phasedseriesfig = plt.figure(figsize=(7.5,4.8),dpi=plotdpi)

    plotvarepoch = None

    # figure out the epoch, if it's None, use the min of the time
    if varepoch is None:
        plotvarepoch = npmin(stimes)

    # if the varepoch is 'min', then fit a spline to the light curve
    # phased using the min of the time, find the fit mag minimum and use
    # the time for that as the varepoch
    elif isinstance(varepoch,str) and varepoch == 'min':

        try:
            spfit = spline_fit_magseries(stimes,
                                         smags,
                                         serrs,
                                         varperiod,
                                         magsarefluxes=magsarefluxes,
                                         sigclip=None,
                                         verbose=verbose)
            plotvarepoch = spfit['fitinfo']['fitepoch']
            if len(plotvarepoch) != 1:
                plotvarepoch = plotvarepoch[0]


        except Exception as e:

            LOGERROR('spline fit failed, trying SavGol fit')

            sgfit = savgol_fit_magseries(stimes,
                                         smags,
                                         serrs,
                                         varperiod,
                                         sigclip=None,
                                         magsarefluxes=magsarefluxes,
                                         verbose=verbose)
            plotvarepoch = sgfit['fitinfo']['fitepoch']
            if len(plotvarepoch) != 1:
                plotvarepoch = plotvarepoch[0]

        finally:

            if plotvarepoch is None:

                LOGERROR('could not find a min epoch time, '
                         'using min(times) as the epoch for '
                         'the phase-folded LC')

                plotvarepoch = npmin(stimes)

    # special case with varepoch lists per each period-finder method
    elif isinstance(varepoch, list):

        try:
            thisvarepochlist = varepoch[lspmethodind]
            plotvarepoch = thisvarepochlist[periodind]
        except Exception as e:
            LOGEXCEPTION(
                "varepoch provided in list form either doesn't match "
                "the length of nbestperiods from the period-finder "
                "result, or something else went wrong. using min(times) "
                "as the epoch instead"
            )
            plotvarepoch = npmin(stimes)

    # the final case is to use the provided varepoch directly
    else:
        plotvarepoch = varepoch


    if verbose:
        LOGINFO('plotting %s phased LC with period %s: %.6f, epoch: %.5f' %
                (lspmethod, periodind, varperiod, plotvarepoch))

    # make the plot title based on the lspmethod
    if periodind == 0:
        plottitle = '%s best period: %.6f d - epoch: %.5f' % (
            (METHODSHORTLABELS[lspmethod] if lspmethod in METHODSHORTLABELS
             else lspmethod),
            varperiod,
            plotvarepoch
        )
    elif periodind > 0:
        plottitle = '%s peak %s: %.6f d - epoch: %.5f' % (
            (METHODSHORTLABELS[lspmethod] if lspmethod in METHODSHORTLABELS
             else lspmethod),
            periodind+1,
            varperiod,
            plotvarepoch
        )
    elif periodind == -1:
        plottitle = '%s period: %.6f d - epoch: %.5f' % (
            lspmethod,
            varperiod,
            plotvarepoch
        )


    # phase the magseries
    phasedlc = phase_magseries(stimes,
                               smags,
                               varperiod,
                               plotvarepoch,
                               wrap=phasewrap,
                               sort=phasesort)
    plotphase = phasedlc['phase']
    plotmags = phasedlc['mags']

    # if we're supposed to bin the phases, do so
    if phasebin:

        binphasedlc = phase_bin_magseries(plotphase,
                                          plotmags,
                                          binsize=phasebin,
                                          minbinelems=minbinelems)
        binplotphase = binphasedlc['binnedphases']
        binplotmags = binphasedlc['binnedmags']

    else:
        binplotphase = None
        binplotmags = None


    # finally, make the phased LC plot
    plt.plot(plotphase,
             plotmags,
             marker='o',
             ms=2.0, ls='None',mew=0,
             color='gray',
             rasterized=True)

    # overlay the binned phased LC plot if we're making one
    if phasebin:
        plt.plot(binplotphase,
                 binplotmags,
                 marker='o',
                 ms=4.0, ls='None',mew=0,
                 color='#1c1e57',
                 rasterized=True)


    # if we're making a overplotfit, then plot the fit over the other stuff
    if overplotfit and isinstance(overplotfit, dict):

        fitmethod = overplotfit['fittype']
        fitredchisq = overplotfit['fitredchisq']

        plotfitmags = overplotfit['fitinfo']['fitmags']
        plotfittimes = overplotfit['magseries']['times']

        # phase the fit magseries
        fitphasedlc = phase_magseries(plotfittimes,
                                      plotfitmags,
                                      varperiod,
                                      plotvarepoch,
                                      wrap=phasewrap,
                                      sort=phasesort)
        plotfitphase = fitphasedlc['phase']
        plotfitmags = fitphasedlc['mags']

        plotfitlabel = (r'%s fit ${\chi}^2/{\mathrm{dof}} = %.3f$' %
                        (fitmethod, fitredchisq))

        # plot the fit phase and mags
        plt.plot(plotfitphase, plotfitmags,'k-',
                 linewidth=3, rasterized=True,label=plotfitlabel)

        plt.legend(loc='upper left', frameon=False)

    # flip y axis for mags
    if not magsarefluxes:
        plot_ylim = plt.ylim()
        plt.ylim((plot_ylim[1], plot_ylim[0]))

    # set the x axis limit
    if not plotxlim:
        plt.xlim((npmin(plotphase)-0.1,
                  npmax(plotphase)+0.1))
    else:
        plt.xlim((plotxlim[0],plotxlim[1]))

    # make a grid
    ax = plt.gca()
    if isinstance(xgridlines, (list, tuple)):
        ax.set_xticks(xgridlines, minor=False)

    plt.grid(color='#a9a9a9',
             alpha=0.9,
             zorder=0,
             linewidth=1.0,
             linestyle=':')


    # make the x and y axis labels
    plot_xlabel = 'phase'
    if magsarefluxes:
        plot_ylabel = 'flux'
    else:
        plot_ylabel = 'magnitude'

    plt.xlabel(plot_xlabel)
    plt.ylabel(plot_ylabel)

    # fix the yaxis ticks (turns off offset and uses the full
    # value of the yaxis tick)
    plt.gca().get_yaxis().get_major_formatter().set_useOffset(False)
    plt.gca().get_xaxis().get_major_formatter().set_useOffset(False)

    # set the plot title
    plt.title(plottitle)

    # make sure the best period phased LC plot stands out
    if (periodind == 0 or periodind == -1) and bestperiodhighlight:
        if MPLVERSION >= (2,0,0):
            plt.gca().set_facecolor(bestperiodhighlight)
        else:
            plt.gca().set_axis_bgcolor(bestperiodhighlight)

    # if we're making an inset plot showing the full range
    if (plotxlim and isinstance(plotxlim, (list, tuple)) and
        len(plotxlim) == 2 and xliminsetmode is True):

        # bump the ylim of the plot so that the inset can fit in this axes plot
        axesylim = plt.gca().get_ylim()

        if magsarefluxes:
            plt.gca().set_ylim(
                axesylim[0],
                axesylim[1] + 0.5*npabs(axesylim[1]-axesylim[0])
            )
        else:
            plt.gca().set_ylim(
                axesylim[0],
                axesylim[1] - 0.5*npabs(axesylim[1]-axesylim[0])
            )

        # put the inset axes in
        inset = inset_axes(plt.gca(), width="40%", height="40%", loc=1)

        # make the scatter plot for the phased LC plot
        inset.plot(plotphase,
                   plotmags,
                   marker='o',
                   ms=2.0, ls='None',mew=0,
                   color='gray',
                   rasterized=True)

        if phasebin:
            # make the scatter plot for the phased LC plot
            inset.plot(binplotphase,
                       binplotmags,
                       marker='o',
                       ms=4.0, ls='None',mew=0,
                       color='#1c1e57',
                       rasterized=True)

        # show the full phase coverage
        if phasewrap:
            inset.set_xlim(-0.2,0.8)
        else:
            inset.set_xlim(-0.1,1.1)

        # flip y axis for mags
        if not magsarefluxes:
            inset_ylim = inset.get_ylim()
            inset.set_ylim((inset_ylim[1], inset_ylim[0]))

        # set the plot title
        inset.text(0.5,0.9,'full phased light curve',
                   ha='center',va='center',transform=inset.transAxes)
        # don't show axes labels or ticks
        inset.set_xticks([])
        inset.set_yticks([])

    # this is the output instance
    phasedseriespng = StrIO()
    phasedseriesfig.savefig(phasedseriespng,
                            # bbox_inches='tight',
                            pad_inches=0.0, format='png')
    plt.close()

    # encode the finderpng instance to base64
    phasedseriespng.seek(0)
    phasedseriesb64 = base64.b64encode(phasedseriespng.read())

    # close the stringio buffer
    phasedseriespng.close()

    # this includes a fitinfo dict if one is provided in overplotfit
    retdict = {
        'plot':phasedseriesb64,
        'period':varperiod,
        'epoch':plotvarepoch,
        'phase':plotphase,
        'phasedmags':plotmags,
        'binphase':binplotphase,
        'binphasedmags':binplotmags,
        'phasewrap':phasewrap,
        'phasesort':phasesort,
        'phasebin':phasebin,
        'minbinelems':minbinelems,
        'plotxlim':plotxlim,
        'lcfit':overplotfit,
    }

    # if we're returning stuff directly, i.e. not being used embedded within
    # the checkplot_dict function
    if directreturn or checkplotdict is None:

        return retdict

    # this requires the checkplotdict to be present already, we'll just update
    # it at the appropriate lspmethod and periodind
    else:

        if override_pfmethod:
            checkplotdict[override_pfmethod][periodind] = retdict
        else:
            checkplotdict[lspmethod][periodind] = retdict

        return checkplotdict
