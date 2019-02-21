#!/usr/bin/env python
# -*- coding: utf-8 -*-
# tfa.py - Waqas Bhatti (wbhatti@astro.princeton.edu) - Feb 2019

'''
This contains functions to run the Trend Filtering Algorithm (TFA) in a
parallelized manner on large collections of light curves.

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

try:
    import cPickle as pickle
except Exception as e:
    import pickle

import os
import os.path
import glob
import multiprocessing as mp
import gzip

from tornado.escape import squeeze

import numpy as np
import numpy.random as npr
npr.seed(0xc0ffee)

import scipy.interpolate as spi
from scipy import linalg as spla

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# to turn a list of keys into a dict address
# from https://stackoverflow.com/a/14692747
from functools import reduce
from operator import getitem
def _dict_get(datadict, keylist):
    return reduce(getitem, keylist, datadict)



############
## CONFIG ##
############

NCPUS = mp.cpu_count()



###################
## LOCAL IMPORTS ##
###################

from astrobase.varclass import starfeatures, varfeatures
from astrobase.lcmath import (
    normalize_magseries,
    sigclip_magseries
)

from astrobase.lcproc import get_lcformat



##################################
## LIGHT CURVE DETRENDING - TFA ##
##################################

def collect_tfa_stats(task):
    '''
    This is a parallel worker to gather LC stats.

    task[0] = lcfile
    task[1] = lcformat
    task[2] = lcformatdir
    task[3] = timecols
    task[4] = magcols
    task[5] = errcols
    task[6] = custom_bandpasses

    '''

    try:

        (lcfile, lcformat, lcformatdir,
         timecols, magcols, errcols,
         custom_bandpasses) = task

        try:
            formatinfo = get_lcformat(lcformat,
                                      use_lcformat_dir=lcformatdir)
            if formatinfo:
                (dfileglob, readerfunc,
                 dtimecols, dmagcols, derrcols,
                 magsarefluxes, normfunc) = formatinfo
            else:
                LOGERROR("can't figure out the light curve format")
                return None
        except Exception as e:
            LOGEXCEPTION("can't figure out the light curve format")
            return None

        # override the default timecols, magcols, and errcols
        # using the ones provided to the function
        if timecols is None:
            timecols = dtimecols
        if magcols is None:
            magcols = dmagcols
        if errcols is None:
            errcols = derrcols

        # get the LC into a dict
        lcdict = readerfunc(lcfile)

        # this should handle lists/tuples being returned by readerfunc
        # we assume that the first element is the actual lcdict
        # FIXME: figure out how to not need this assumption
        if ( (isinstance(lcdict, (list, tuple))) and
             (isinstance(lcdict[0], dict)) ):
            lcdict = lcdict[0]

        #
        # collect the necessary stats for this light curve
        #

        # 1. number of observations
        # 2. median mag
        # 3. eta_normal
        # 4. MAD
        # 5. objectid
        # 6. get mags and colors from objectinfo if there's one in lcdict

        if 'objectid' in lcdict:
            objectid = lcdict['objectid']
        elif 'objectinfo' in lcdict and 'objectid' in lcdict['objectinfo']:
            objectid = lcdict['objectinfo']['objectid']
        elif 'objectinfo' in lcdict and 'hatid' in lcdict['objectinfo']:
            objectid = lcdict['objectinfo']['hatid']
        else:
            LOGERROR('no objectid present in lcdict for LC %s, '
                     'using filename prefix as objectid' % lcfile)
            objectid = os.path.splitext(os.path.basename(lcfile))[0]

        if 'objectinfo' in lcdict:

            colorfeat = starfeatures.color_features(
                lcdict['objectinfo'],
                deredden=False,
                custom_bandpasses=custom_bandpasses
            )

        else:
            LOGERROR('no objectinfo dict in lcdict, '
                     'could not get magnitudes for LC %s, '
                     'cannot use for TFA template ensemble' %
                     lcfile)
            return None


        # this is the initial dict
        resultdict = {'objectid':objectid,
                      'ra':lcdict['objectinfo']['ra'],
                      'decl':lcdict['objectinfo']['decl'],
                      'colorfeat':colorfeat,
                      'lcfpath':os.path.abspath(lcfile),
                      'lcformat':lcformat,
                      'lcformatdir':lcformatdir,
                      'timecols':timecols,
                      'magcols':magcols,
                      'errcols':errcols}

        for tcol, mcol, ecol in zip(timecols, magcols, errcols):

            try:

                # dereference the columns and get them from the lcdict
                if '.' in tcol:
                    tcolget = tcol.split('.')
                else:
                    tcolget = [tcol]
                times = _dict_get(lcdict, tcolget)

                if '.' in mcol:
                    mcolget = mcol.split('.')
                else:
                    mcolget = [mcol]
                mags = _dict_get(lcdict, mcolget)

                if '.' in ecol:
                    ecolget = ecol.split('.')
                else:
                    ecolget = [ecol]
                errs = _dict_get(lcdict, ecolget)

                # normalize here if not using special normalization
                if normfunc is None:
                    ntimes, nmags = normalize_magseries(
                        times, mags,
                        magsarefluxes=magsarefluxes
                    )

                    times, mags, errs = ntimes, nmags, errs

                # get the variability features for this object
                varfeat = varfeatures.all_nonperiodic_features(
                    times, mags, errs
                )

                resultdict[mcol] = varfeat

            except Exception as e:

                LOGEXCEPTION('%s, magcol: %s, probably ran into all-nans' %
                             (lcfile, mcol))
                resultdict[mcol] = {'ndet':0,
                                    'mad':np.nan,
                                    'eta_normal':np.nan}


        return resultdict

    except Exception as e:

        LOGEXCEPTION('could not execute get_tfa_stats for task: %s' %
                     repr(task))
        return None



def reform_templatelc_for_tfa(task):
    '''
    This is a parallel worker that reforms light curves for TFA.

    task[0] = lcfile
    task[1] = lcformat
    task[2] = lcformatdir
    task[3] = timecol
    task[4] = magcol
    task[5] = errcol
    task[6] = timebase
    task[7] = interpolate_type
    task[8] = sigclip

    '''

    try:

        (lcfile, lcformat, lcformatdir,
         tcol, mcol, ecol,
         timebase, interpolate_type, sigclip) = task

        try:
            formatinfo = get_lcformat(lcformat,
                                      use_lcformat_dir=lcformatdir)
            if formatinfo:
                (dfileglob, readerfunc,
                 dtimecols, dmagcols, derrcols,
                 magsarefluxes, normfunc) = formatinfo
            else:
                LOGERROR("can't figure out the light curve format")
                return None
        except Exception as e:
            LOGEXCEPTION("can't figure out the light curve format")
            return None

        # get the LC into a dict
        lcdict = readerfunc(lcfile)

        # this should handle lists/tuples being returned by readerfunc
        # we assume that the first element is the actual lcdict
        # FIXME: figure out how to not need this assumption
        if ( (isinstance(lcdict, (list, tuple))) and
             (isinstance(lcdict[0], dict)) ):
            lcdict = lcdict[0]

        outdict = {}

        # dereference the columns and get them from the lcdict
        if '.' in tcol:
            tcolget = tcol.split('.')
        else:
            tcolget = [tcol]
        times = _dict_get(lcdict, tcolget)

        if '.' in mcol:
            mcolget = mcol.split('.')
        else:
            mcolget = [mcol]
        mags = _dict_get(lcdict, mcolget)

        if '.' in ecol:
            ecolget = ecol.split('.')
        else:
            ecolget = [ecol]
        errs = _dict_get(lcdict, ecolget)

        # normalize here if not using special normalization
        if normfunc is None:
            ntimes, nmags = normalize_magseries(
                times, mags,
                magsarefluxes=magsarefluxes
            )

        times, mags, errs = ntimes, nmags, errs

        #
        # now we'll do: 1. sigclip, 2. reform to timebase, 3. renorm to zero
        #

        # 1. sigclip as requested
        stimes, smags, serrs = sigclip_magseries(times,
                                                 mags,
                                                 errs,
                                                 sigclip=sigclip)

        # 2. now, we'll renorm to the timebase
        mags_interpolator = spi.interp1d(stimes, smags,
                                         kind=interpolate_type,
                                         fill_value='extrapolate')
        errs_interpolator = spi.interp1d(stimes, serrs,
                                         kind=interpolate_type,
                                         fill_value='extrapolate')

        interpolated_mags = mags_interpolator(timebase)
        interpolated_errs = errs_interpolator(timebase)

        # 3. renorm to zero
        magmedian = np.median(interpolated_mags)

        renormed_mags = interpolated_mags - magmedian

        # update the dict
        outdict = {'mags':renormed_mags,
                   'errs':interpolated_errs,
                   'origmags':interpolated_mags}

        #
        # done with this magcol
        #
        return outdict

    except Exception as e:

        LOGEXCEPTION('reform LC task failed: %s' % repr(task))
        return None



def tfa_templates_lclist(
        lclist,
        outfile=None,
        target_template_frac=0.1,
        max_target_frac_obs=0.25,
        min_template_number=10,
        max_template_number=1000,
        max_rms=0.15,
        max_mult_above_magmad=1.5,
        max_mult_above_mageta=1.5,
        mag_bandpass='sdssr',
        custom_bandpasses=None,
        mag_bright_limit=10.0,
        mag_faint_limit=12.0,
        template_sigclip=5.0,
        template_interpolate='nearest',
        lcformat='hat-sql',
        lcformatdir=None,
        timecols=None,
        magcols=None,
        errcols=None,
        nworkers=NCPUS,
        maxworkertasks=1000,
):
    '''This selects template objects for TFA.

    lclist is a list of light curves to use as input to generate the template
    set.

    outfile is a pickle filename to which the TFA template list will be written
    to.

    target_template_frac is the fraction of total objects in lclist to use for
    the number of templates.

    max_target_frac_obs sets the number of templates to generate if the number
    of observations for the light curves is smaller than the number of objects
    in the collection. The number of templates will be set to this fraction of
    the number of observations if this is the case.

    min_template_number is the minimum number of templates to generate.

    max_template_number is the maximum number of templates to generate. If
    target_template_frac times the number of objects is greater than
    max_template_number, only max_template_number templates will be used.

    max_rms is the maximum light curve RMS for an object to consider it as a
    possible template ensemble member.

    max_mult_above_magmad is the maximum multiplier above the mag-RMS fit to
    consider an object as variable and thus not part of the template ensemble.

    max_mult_above_mageta is the maximum multiplier above the mag-eta (variable
    index) fit to consider an object as variable and thus not part of the
    template ensemble.

    mag_bandpass sets the key in the light curve dict's objectinfo dict to use
    as the canonical magnitude for the object and apply any magnitude limits to.

    custom_bandpasses can be used to provide any custom band name keys to the
    star feature collection function.

    mag_bright_limit sets the brightest mag for a potential member of the TFA
    template ensemble.

    mag_faint_limit sets the faintest mag for a potential member of the TFA
    template ensemble.

    template_sigclip sets the sigma-clip to be applied to the template light
    curves.

    template_interpolate sets the kwarg to pass to scipy.interpolate.interp1d to
    set the kind of interpolation to use when reforming light curves to the TFA
    template timebase.

    lcformat sets the key in LCFORM to use to read the light curves. Use the
    lcproc.register_custom_lcformat function to register a custom light curve
    format in the lcproc.LCFORM dict.

    timecols, magcols, errcols are lists of lcdict keys to use to generate the
    TFA template ensemble. These will be the light curve magnitude columns that
    TFA will be ultimately applied to by apply_tfa_magseries below.

    nworkers and maxworkertasks control the number of parallel workers and tasks
    per worker used by this function to collect light curve information and to
    reform light curves to the TFA template's timebase.

    Selection criteria for TFA template ensemble objects:

    - not variable: use a poly fit to the mag-MAD relation and eta-normal
      variability index to get nonvar objects
    - not more than 10% of the total number of objects in the field or
      maxtfatemplates at most
    - allow shuffling of the templates if the target ends up in them
    - nothing with less than the median number of observations in the field
    - sigma-clip the input time series observations
    - TODO: uniform sampling in tangent plane coordinates (we'll need ra and
      decl)

    This also determines the effective cadence that all TFA LCs will be binned
    to as the template LC with the largest number of non-nan observations will
    be used. All template LCs will be renormed to zero.

    This function returns a dict that can be passed directly to
    apply_tfa_magseries below. It can optionally produce a pickle with the same
    dict, which can also be passed to that function.

    '''
    try:
        formatinfo = get_lcformat(lcformat,
                                  use_lcformat_dir=lcformatdir)
        if formatinfo:
            (dfileglob, readerfunc,
             dtimecols, dmagcols, derrcols,
             magsarefluxes, normfunc) = formatinfo
        else:
            LOGERROR("can't figure out the light curve format")
            return None
    except Exception as e:
        LOGEXCEPTION("can't figure out the light curve format")
        return None

    # override the default timecols, magcols, and errcols
    # using the ones provided to the function
    if timecols is None:
        timecols = dtimecols
    if magcols is None:
        magcols = dmagcols
    if errcols is None:
        errcols = derrcols

    LOGINFO('collecting light curve information for %s in list...' %
            len(lclist))

    # first, we'll collect the light curve info
    tasks = [(x, lcformat, lcformat,
              timecols, magcols, errcols,
              custom_bandpasses) for x in lclist]

    pool = mp.Pool(nworkers, maxtasksperchild=maxworkertasks)
    results = pool.map(collect_tfa_stats, tasks)
    pool.close()
    pool.join()

    # now, go through the light curves

    outdict = {
        'timecols':[],
        'magcols':[],
        'errcols':[],
    }

    # for each magcol, we'll generate a separate template list
    for tcol, mcol, ecol in zip(timecols, magcols, errcols):

        if '.' in tcol:
            tcolget = tcol.split('.')
        else:
            tcolget = [tcol]


        # these are the containers for possible template collection LC info
        (lcmag, lcmad, lceta,
         lcndet, lcobj, lcfpaths,
         lcra, lcdecl) = [], [], [], [], [], [], [], []

        outdict['timecols'].append(tcol)
        outdict['magcols'].append(mcol)
        outdict['errcols'].append(ecol)

        # add to the collection of all light curves
        outdict[mcol] = {'collection':{'mag':[],
                                       'mad':[],
                                       'eta':[],
                                       'ndet':[],
                                       'obj':[],
                                       'lcf':[],
                                       'ra':[],
                                       'decl':[]}}

        LOGINFO('magcol: %s, collecting prospective template LC info...' %
                mcol)


        # collect the template LCs for this magcol
        for result in results:

            # we'll only append objects that have all of these elements
            try:

                thismag = result['colorfeat'][mag_bandpass]
                thismad = result[mcol]['mad']
                thiseta = result[mcol]['eta_normal']
                thisndet = result[mcol]['ndet']
                thisobj = result['objectid']
                thislcf = result['lcfpath']
                thisra = result['ra']
                thisdecl = result['decl']

                outdict[mcol]['collection']['mag'].append(thismag)
                outdict[mcol]['collection']['mad'].append(thismad)
                outdict[mcol]['collection']['eta'].append(thiseta)
                outdict[mcol]['collection']['ndet'].append(thisndet)
                outdict[mcol]['collection']['obj'].append(thisobj)
                outdict[mcol]['collection']['lcf'].append(thislcf)
                outdict[mcol]['collection']['ra'].append(thisra)
                outdict[mcol]['collection']['decl'].append(thisdecl)

                # make sure the object lies in the mag limits and RMS limits we
                # set before to try to accept it into the TFA ensemble
                if ((mag_bright_limit < thismag < mag_faint_limit) and
                    (1.4826*thismad < max_rms)):

                    lcmag.append(thismag)
                    lcmad.append(thismad)
                    lceta.append(thiseta)
                    lcndet.append(thisndet)
                    lcobj.append(thisobj)
                    lcfpaths.append(thislcf)
                    lcra.append(thisra)
                    lcdecl.append(thisdecl)

            except Exception as e:
                pass

        # make sure we have enough LCs to work on
        if len(lcobj) >= min_template_number:

            LOGINFO('magcol: %s, %s objects eligible for '
                    'template selection after filtering on mag '
                    'limits (%s, %s) and max RMS (%s)' %
                    (mcol, len(lcobj),
                     mag_bright_limit, mag_faint_limit, max_rms))

            lcmag = np.array(lcmag)
            lcmad = np.array(lcmad)
            lceta = np.array(lceta)
            lcndet = np.array(lcndet)
            lcobj = np.array(lcobj)
            lcfpaths = np.array(lcfpaths)
            lcra = np.array(lcra)
            lcdecl = np.array(lcdecl)

            sortind = np.argsort(lcmag)
            lcmag = lcmag[sortind]
            lcmad = lcmad[sortind]
            lceta = lceta[sortind]
            lcndet = lcndet[sortind]
            lcobj = lcobj[sortind]
            lcfpaths = lcfpaths[sortind]
            lcra = lcra[sortind]
            lcdecl = lcdecl[sortind]

            # 1. get the mag-MAD relation

            # this is needed for spline fitting
            # should take care of the pesky 'x must be strictly increasing' bit
            splfit_ind = np.diff(lcmag) > 0.0
            splfit_ind = np.concatenate((np.array([True]), splfit_ind))

            fit_lcmag = lcmag[splfit_ind]
            fit_lcmad = lcmad[splfit_ind]
            fit_lceta = lceta[splfit_ind]

            magmadfit = np.poly1d(np.polyfit(
                fit_lcmag,
                fit_lcmad,
                2
            ))
            magmadind = lcmad/magmadfit(lcmag) < max_mult_above_magmad

            # 2. get the mag-eta relation
            magetafit = np.poly1d(np.polyfit(
                fit_lcmag,
                fit_lceta,
                2
            ))
            magetaind = magetafit(lcmag)/lceta < max_mult_above_mageta

            # 3. get the median ndet
            median_ndet = np.median(lcndet)
            ndetind = lcndet >= median_ndet

            # form the final template ensemble
            templateind = magmadind & magetaind & ndetind

            # check again if we have enough LCs in the template
            if templateind.sum() >= min_template_number:

                LOGINFO('magcol: %s, %s objects selectable for TFA templates' %
                        (mcol, templateind.sum()))

                templatemag = lcmag[templateind]
                templatemad = lcmad[templateind]
                templateeta = lceta[templateind]
                templatendet = lcndet[templateind]
                templateobj = lcobj[templateind]
                templatelcf = lcfpaths[templateind]
                templatera = lcra[templateind]
                templatedecl = lcdecl[templateind]

                # now, check if we have no more than the required fraction of
                # TFA templates
                target_number_templates = int(target_template_frac*len(lclist))

                if target_number_templates > max_template_number:
                    target_number_templates = max_template_number

                LOGINFO('magcol: %s, selecting %s TFA templates randomly' %
                        (mcol, target_number_templates))

                # FIXME: how do we select uniformly in xi-eta?

                # select random uniform objects from the template candidates
                targetind = npr.choice(templateobj.size,
                                       target_number_templates,
                                       replace=False)

                templatemag = templatemag[targetind]
                templatemad = templatemad[targetind]
                templateeta = templateeta[targetind]
                templatendet = templatendet[targetind]
                templateobj = templateobj[targetind]
                templatelcf = templatelcf[targetind]
                templatera = templatera[targetind]
                templatedecl = templatedecl[targetind]

                # get the max ndet so far to use that LC as the timebase
                maxndetind = templatendet == templatendet.max()
                timebaselcf = templatelcf[maxndetind][0]
                timebasendet = templatendet[maxndetind][0]
                LOGINFO('magcol: %s, selected %s as template time '
                        'base LC with %s observations' %
                        (mcol, timebaselcf, timebasendet))

                timebaselcdict = readerfunc(timebaselcf)

                if ( (isinstance(timebaselcdict, (list, tuple))) and
                     (isinstance(timebaselcdict[0], dict)) ):
                    timebaselcdict = timebaselcdict[0]

                # this is the timebase to use for all of the templates
                timebase = _dict_get(timebaselcdict, tcolget)

                # also check if the number of templates is longer than the
                # actual timebase of the observations. this will cause issues
                # with overcorrections and will probably break TFA
                if target_number_templates > timebasendet:

                    LOGWARNING('the number of TFA templates (%s) is '
                               'larger than the number of observations '
                               'of the time base (%s). This will likely '
                               'overcorrect all light curves to a '
                               'constant level. '
                               'Will use up to %s x timebase ndet '
                               'templates instead' %
                               (target_number_templates,
                                timebasendet,
                                max_target_frac_obs))

                    # regen the templates based on the new number
                    newmaxtemplates = int(max_target_frac_obs*timebasendet)

                    # choose this number out of the already chosen templates
                    # randomly

                    LOGWARNING('magcol: %s, re-selecting %s TFA '
                               'templates randomly' %
                               (mcol, newmaxtemplates))

                    # select random uniform objects from the template candidates
                    targetind = npr.choice(templateobj.size,
                                           newmaxtemplates,
                                           replace=False)

                    templatemag = templatemag[targetind]
                    templatemad = templatemad[targetind]
                    templateeta = templateeta[targetind]
                    templatendet = templatendet[targetind]
                    templateobj = templateobj[targetind]
                    templatelcf = templatelcf[targetind]
                    templatera = templatera[targetind]
                    templatedecl = templatedecl[targetind]

                    # get the max ndet so far to use that LC as the timebase
                    maxndetind = templatendet == templatendet.max()
                    timebaselcf = templatelcf[maxndetind][0]
                    timebasendet = templatendet[maxndetind][0]
                    LOGWARNING('magcol: %s, re-selected %s as template time '
                               'base LC with %s observations' %
                               (mcol, timebaselcf, timebasendet))

                    timebaselcdict = readerfunc(timebaselcf)

                    if ( (isinstance(timebaselcdict, (list, tuple))) and
                         (isinstance(timebaselcdict[0], dict)) ):
                        timebaselcdict = timebaselcdict[0]

                    # this is the timebase to use for all of the templates
                    timebase = _dict_get(timebaselcdict, tcolget)

                LOGINFO('magcol: %s, reforming TFA template LCs to '
                        ' chosen timebase...' % mcol)

                # reform all template LCs to this time base, normalize to
                # zero, and sigclip as requested. this is a parallel op
                # first, we'll collect the light curve info
                tasks = [(x, lcformat, lcformatdir,
                          tcol, mcol, ecol,
                          timebase, template_interpolate,
                          template_sigclip) for x
                         in templatelcf]

                pool = mp.Pool(nworkers, maxtasksperchild=maxworkertasks)
                results = pool.map(reform_templatelc_for_tfa, tasks)
                pool.close()
                pool.join()

                # generate a 2D array for the template magseries with dimensions
                # = (n_objects, n_lcpoints)
                template_magseries = np.array([x['mags'] for x in results])
                template_errseries = np.array([x['errs'] for x in results])

                # put everything into a templateinfo dict for this magcol
                outdict[mcol].update({
                    'timebaselcf':timebaselcf,
                    'timebase':timebase,
                    'trendfits':{'mag-mad':magmadfit,
                                 'mag-eta':magetafit},
                    'template_objects':templateobj,
                    'template_ra':templatera,
                    'template_decl':templatedecl,
                    'template_mag':templatemag,
                    'template_mad':templatemad,
                    'template_eta':templateeta,
                    'template_ndet':templatendet,
                    'template_magseries':template_magseries,
                    'template_errseries':template_errseries
                })

            # if we don't have enough, return nothing for this magcol
            else:
                LOGERROR('not enough objects meeting requested '
                         'MAD, eta, ndet conditions to '
                         'select templates for magcol: %s' % mcol)
                continue

        else:

            LOGERROR('nobjects: %s, not enough in requested mag range to '
                     'select templates for magcol: %s' % (len(lcobj),mcol))
            continue

        # make the plots for mag-MAD/mag-eta relation and fits used
        plt.plot(lcmag, lcmad, marker='o', linestyle='none', ms=1.0)
        modelmags = np.linspace(lcmag.min(), lcmag.max(), num=1000)
        plt.plot(modelmags, outdict[mcol]['trendfits']['mag-mad'](modelmags))
        plt.yscale('log')
        plt.xlabel('catalog magnitude')
        plt.ylabel('light curve MAD')
        plt.title('catalog mag vs. light curve MAD and fit')
        plt.savefig('catmag-lcmad-fit.png',bbox_inches='tight')
        plt.close('all')

        plt.plot(lcmag, lceta, marker='o', linestyle='none', ms=1.0)
        modelmags = np.linspace(lcmag.min(), lcmag.max(), num=1000)
        plt.plot(modelmags, outdict[mcol]['trendfits']['mag-eta'](modelmags))
        plt.yscale('log')
        plt.xlabel('catalog magnitude')
        plt.ylabel('light curve eta variable index')
        plt.title('catalog mag vs. light curve eta and fit')
        plt.savefig('catmag-lceta-fit.png',bbox_inches='tight')
        plt.close('all')


    #
    # end of operating on each magcol
    #

    # save the templateinfo dict to a pickle if requested
    if outfile:

        if outfile.endswith('.gz'):
            outfd = gzip.open(outfile,'wb')
        else:
            outfd = open(outfile,'wb')

        with outfd:
            pickle.dump(outdict, outfd, protocol=pickle.HIGHEST_PROTOCOL)

    # return the templateinfo dict
    return outdict



def apply_tfa_magseries(lcfile,
                        timecol,
                        magcol,
                        errcol,
                        templateinfo,
                        mintemplatedist_arcmin=1.0,
                        lcformat='hat-sql',
                        lcformatdir=None,
                        interp='nearest',
                        sigclip=5.0):
    '''This applies the TFA correction to an LC given TFA template information.

    lcfile is the light curve file to apply the TFA correction to.

    timecol, magcol, errcol are the column keys in the lcdict for the LC file to
    apply the TFA correction to.

    templateinfo is either the dict produced by tfa_templates_lclist or the
    pickle produced by the same function.

    TODO: mintemplatedist_arcmin sets the minimum distance required from the
    target object for objects in the TFA template ensemble. Objects closer than
    this distance will be removed from the ensemble.

    lcformat is the LCFORM dict key for the light curve format of lcfile.

    interp is passed to scipy.interpolate.interp1d as the kind of interpolation
    to use when reforming this light curve to the timebase of the TFA templates.

    sigclip is the sigma clip to apply to this light curve before running TFA on
    it.

    This returns the filename of the light curve file generated after TFA
    applications. This is a pickle (that can be read by lcproc.read_pklc) in the
    same directory as lcfile. The magcol will be encoded in the filename, so
    each magcol in lcfile gets its own output file.

    '''

    try:
        formatinfo = get_lcformat(lcformat,
                                  use_lcformat_dir=lcformatdir)
        if formatinfo:
            (dfileglob, readerfunc,
             dtimecols, dmagcols, derrcols,
             magsarefluxes, normfunc) = formatinfo
        else:
            LOGERROR("can't figure out the light curve format")
            return None
    except Exception as e:
        LOGEXCEPTION("can't figure out the light curve format")
        return None

    # get the templateinfo from a pickle if necessary
    if isinstance(templateinfo,str) and os.path.exists(templateinfo):
        with open(templateinfo,'rb') as infd:
            templateinfo = pickle.load(infd)

    lcdict = readerfunc(lcfile)
    if ((isinstance(lcdict, (tuple, list))) and
        isinstance(lcdict[0], dict)):
        lcdict = lcdict[0]

    objectid = lcdict['objectid']

    # if the object itself is in the template ensemble, remove it

    # TODO: also remove objects from the template that lie within some radius of
    # the target object (let's make this 1 arcminute by default)

    if objectid in templateinfo[magcol]['template_objects']:

        LOGWARNING('object %s found in the TFA template ensemble, removing...' %
                   objectid)

        templateind = templateinfo[magcol]['template_objects'] == objectid

        # we need to copy over this template instance
        tmagseries = templateinfo[magcol][
            'template_magseries'
        ][~templateind,:][::]

    # otherwise, get the full ensemble
    else:

        tmagseries = templateinfo[magcol][
            'template_magseries'
        ][::]

    # this is the normal matrix
    normal_matrix = np.dot(tmagseries, tmagseries.T)

    # get the inverse of the matrix
    normal_matrix_inverse = spla.pinv2(normal_matrix)

    # get the timebase from the template
    timebase = templateinfo[magcol]['timebase']

    # use this to reform the target lc in the same manner as that for a TFA
    # template LC
    reformed_targetlc = reform_templatelc_for_tfa((
        lcfile,
        lcformat,
        lcformatdir,
        timecol,
        magcol,
        errcol,
        timebase,
        interp,
        sigclip
    ))

    # calculate the scalar products of the target and template magseries
    scalar_products = np.dot(tmagseries, reformed_targetlc['mags'])

    # calculate the corrections
    corrections = np.dot(normal_matrix_inverse, scalar_products)

    # finally, get the corrected time series for the target object
    corrected_magseries = (
        reformed_targetlc['origmags'] -
        np.dot(tmagseries.T, corrections)
    )

    outdict = {
        'times':timebase,
        'mags':corrected_magseries,
        'errs':reformed_targetlc['errs'],
        'mags_median':np.median(corrected_magseries),
        'mags_mad': np.median(np.abs(corrected_magseries -
                                     np.median(corrected_magseries))),
        'work':{'tmagseries':tmagseries,
                'normal_matrix':normal_matrix,
                'normal_matrix_inverse':normal_matrix_inverse,
                'scalar_products':scalar_products,
                'corrections':corrections,
                'reformed_targetlc':reformed_targetlc},
    }


    # we'll write back the tfa times and mags to the lcdict
    lcdict['tfa'] = outdict
    outfile = os.path.join(
        os.path.dirname(lcfile),
        '%s-tfa-%s-pklc.pkl' % (
            squeeze(objectid).replace(' ','-'),
            magcol
        )
    )
    with open(outfile,'wb') as outfd:
        pickle.dump(lcdict, outfd, pickle.HIGHEST_PROTOCOL)

    return outfile



def parallel_tfa_worker(task):
    '''
    This is a parallel worker for the function below.

    task[0] = lcfile
    task[1] = timecol
    task[2] = magcol
    task[3] = errcol
    task[4] = templateinfo
    task[5] = lcformat
    task[6] = lcformatdir
    task[6] = interp
    task[7] = sigclip

    '''

    (lcfile, timecol, magcol, errcol,
     templateinfo, lcformat, lcformatdir,
     interp, sigclip) = task

    try:

        res = apply_tfa_magseries(lcfile, timecol, magcol, errcol,
                                  templateinfo,
                                  lcformat=lcformat,
                                  lcformatdir=lcformatdir,
                                  interp=interp,
                                  sigclip=sigclip)
        if res:
            LOGINFO('%s -> %s TFA OK' % (lcfile, res))
        return res

    except Exception as e:

        LOGEXCEPTION('TFA failed for %s' % lcfile)
        return None



def parallel_tfa_lclist(lclist,
                        templateinfo,
                        timecols=None,
                        magcols=None,
                        errcols=None,
                        lcformat='hat-sql',
                        lcformatdir=None,
                        interp='nearest',
                        sigclip=5.0,
                        nworkers=NCPUS,
                        maxworkertasks=1000):
    '''This applies TFA in parallel to all LCs in lclist.

    lclist is a list of light curve files to apply the TFA correction to.

    templateinfo is either the dict produced by tfa_templates_lclist or the
    pickle produced by the same function.

    timecols, magcols, errcols are lists of column keys in the lcdict for each
    LC file to apply the TFA correction to. each magcol will get their own
    output TFA light curve file. If these are None, then magcols used for the
    TFA template will be re-used for TFA application.

    lcformat is the LCFORM dict key for the light curve format of lcfile.

    interp is passed to scipy.interpolate.interp1d as the kind of interpolation
    to use when reforming this light curve to the timebase of the TFA templates.

    sigclip is the sigma clip to apply to this light curve before running TFA on
    it.

    nworkers and maxworkertasks set the number of parallel workers and max tasks
    per worker used to run TFA in parallel.

    '''

    # open the templateinfo first
    if isinstance(templateinfo,str) and os.path.exists(templateinfo):
        with open(templateinfo,'rb') as infd:
            templateinfo = pickle.load(infd)

    try:
        formatinfo = get_lcformat(lcformat,
                                  use_lcformat_dir=lcformatdir)
        if formatinfo:
            (dfileglob, readerfunc,
             dtimecols, dmagcols, derrcols,
             magsarefluxes, normfunc) = formatinfo
        else:
            LOGERROR("can't figure out the light curve format")
            return None
    except Exception as e:
        LOGEXCEPTION("can't figure out the light curve format")
        return None

    # override the default timecols, magcols, and errcols
    # using the ones provided to the function
    # we'll get the defaults from the templateinfo object
    if timecols is None:
        timecols = templateinfo['timecols']
    if magcols is None:
        magcols = templateinfo['magcols']
    if errcols is None:
        errcols = templateinfo['errcols']

    outdict = {}

    # run by magcol
    for t, m, e in zip(timecols, magcols, errcols):

        tasks = [(x, t, m, e, templateinfo,
                  lcformat, lcformatdir,
                  interp, sigclip) for
                 x in lclist]

        pool = mp.Pool(nworkers, maxtasksperchild=maxworkertasks)
        results = pool.map(parallel_tfa_worker, tasks)
        pool.close()
        pool.join()

        outdict[m] = results

    return outdict



def parallel_tfa_lcdir(lcdir,
                       templateinfo,
                       lcfileglob=None,
                       timecols=None,
                       magcols=None,
                       errcols=None,
                       lcformat='hat-sql',
                       lcformatdir=None,
                       interp='nearest',
                       sigclip=5.0,
                       nworkers=NCPUS,
                       maxworkertasks=1000):
    '''This applies TFA in parallel to all LCs in lcdir.

    lcfileglob is the glob to use to find the target light curves in lcdir. If
    this is None, the default fileglob provided in the LC format registration in
    lcproc.LCFORM will be used instead.

    '''

    # open the templateinfo first
    if isinstance(templateinfo,str) and os.path.exists(templateinfo):
        with open(templateinfo,'rb') as infd:
            templateinfo = pickle.load(infd)

    try:
        formatinfo = get_lcformat(lcformat,
                                  use_lcformat_dir=lcformatdir)
        if formatinfo:
            (dfileglob, readerfunc,
             dtimecols, dmagcols, derrcols,
             magsarefluxes, normfunc) = formatinfo
        else:
            LOGERROR("can't figure out the light curve format")
            return None
    except Exception as e:
        LOGEXCEPTION("can't figure out the light curve format")
        return None

    # find all the files matching the lcglob in lcdir
    if lcfileglob is None:
        lcfileglob = dfileglob

    lclist = sorted(glob.glob(os.path.join(lcdir, lcfileglob)))

    return parallel_tfa_lclist(
        lclist,
        templateinfo,
        timecols=timecols,
        magcols=magcols,
        errcols=errcols,
        lcformat=lcformat,
        lcformatdir=None,
        interp=interp,
        sigclip=sigclip,
        nworkers=nworkers,
        maxworkertasks=maxworkertasks
    )
