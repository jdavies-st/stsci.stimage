import copy,os
import numpy as np

import pywcs
import stwcs
import pyfits

from stwcs import distortion
from stwcs.distortion import utils
from stwcs.wcsutil import wcscorr
from stsci.tools import fileutil as fu
from stsci.stimage import xyxymatch

import catalogs
import linearfit
import updatehdr
import util
import tweakutils

class Image(object):
    """ Primary class to keep track of all WCS and catalog information for
        a single input image. This class also performs all matching and fitting.
    """
    def __init__(self,filename,input_catalogs=None,**kwargs):
        """
        Parameters
        ----------
        filename : str
            filename for image

        input_catalogs : list of str or None
            filename of catalog files for each chip, if specified by user

        kwargs : dict
            parameters necessary for processing derived from input configObj object

        """
        self.name = filename
        self.rootname = filename[:filename.find('.')]
        self.origin = 1
        self.pars = kwargs
        if input_catalogs is not None and kwargs['xyunits'] == 'degrees':
            # Input was a catalog of sky positions, so no WCS or image needed
            use_wcs = False
            num_sci = 0
        else:
            # WCS required, so verify that we can get one
            # Need to count number of SCI extensions
            #  (assume a valid WCS with each SCI extension)
            num_sci,extname = count_sci_extensions(filename)
            if num_sci < 1:
                print 'ERROR: No Valid WCS available for %s',filename
                raise InputError
            use_wcs = True
        # Record this for use with methods
        self.use_wcs = use_wcs
        self.num_sci = num_sci
        self.ext_name = extname

        # Need to generate a separate catalog for each chip
        self.chip_catalogs = {}
        num_sources = 0
        # For each SCI extension, generate a catalog and WCS
        for sci_extn in range(1,num_sci+1):
            extnum = fu.findExtname(pyfits.open(filename),extname,extver=sci_extn)
            if extnum is None: extnum = 0
            chip_filename = filename+'[%d]'%(extnum)
            if use_wcs:
                wcs = stwcs.wcsutil.HSTWCS(chip_filename)
            if input_catalogs is None:
                # if we already have a set of catalogs provided on input,
                #  we only need the array to get original XY input positions
                source = chip_filename
                catalog_mode='automatic'
            else:
                source = input_catalogs[sci_extn-1]
                catalog_mode='user'
            kwargs['start_id'] = num_sources
            catalog = catalogs.generateCatalog(wcs,mode=catalog_mode,catalog=source,**kwargs)
            catalog.buildCatalogs() # read in and convert all catalog positions to RA/Dec
            num_sources += catalog.num_objects
            self.chip_catalogs[sci_extn] = {'catalog':catalog,'wcs':wcs}

        self.catalog_names = {}
        # Build full list of all sky positions from all chips
        self.buildSkyCatalog()
        if self.pars['writecat']:
            catname = self.rootname+"_sky_catalog.coo"
            self.catalog_names['match'] = self.rootname+"_xy_catalog.match"
            self.write_skycatalog(catname)
            self.catalog_names['sky'] = catname # Keep track of catalogs being written out
            for nsci in range(1,num_sci+1):
                catname = "%s_sci%d_xy_catalog.coo"%(self.rootname,nsci)
                self.chip_catalogs[nsci]['catalog'].writeXYCatalog(catname)
                # Keep track of catalogs being written out
                if 'input_xy' not in self.catalog_names:
                    self.catalog_names['input_xy'] = []
                self.catalog_names['input_xy'].append(catname)
            self.catalog_names['fitmatch'] = self.rootname+"_catalog_fit.match"

        # Set up products which need to be computed by methods of this class
        self.outxy = None
        self.refWCS = None # reference WCS assigned for the final fit
        self.matches = {'image':None,'ref':None} # stores matched list of coordinates for fitting
        self.fit = None # stores result of fit
        self.match_pars = None
        self.fit_pars = None
        self.identityfit = False # set to True when matching/fitting to itself
        self.goodmatch = True # keep track of whether enough matches were found for a fit
        
        self.perform_update = True
    def get_wcs(self):
        """ Helper method to return a list of all the input WCS objects associated
            with this image
        """
        wcslist = []
        for chip in self.chip_catalogs:
            wcslist.append(self.chip_catalogs[chip]['wcs'])
        return wcslist

    def buildSkyCatalog(self):
        """ Convert sky catalog for all chips into a single catalog for
            the entire field-of-view of this image
        """
        ralist = []
        declist = []
        fluxlist = []
        idlist = []
        for scichip in self.chip_catalogs:
            skycat = self.chip_catalogs[scichip]['catalog'].radec
            xycat = self.chip_catalogs[scichip]['catalog'].xypos
            if skycat is not None:
                ralist.append(skycat[0])
                declist.append(skycat[1])
                if len(xycat) > 2:
                    fluxlist.append(xycat[2])
                    idlist.append(xycat[3])
                else:
                    fluxlist.append([999.0]*len(skycat[0]))
                    idlist.append(np.arange(len(skycat[0])))

        self.all_radec = [np.concatenate(ralist),np.concatenate(declist),
                        np.concatenate(fluxlist),np.concatenate(idlist)]
        self.all_radec_orig = copy.deepcopy(self.all_radec)
        

    def buildDefaultRefWCS(self):
        """ Generate a default reference WCS for this image
        """
        self.default_refWCS = None
        if self.use_wcs:
            wcslist = []
            for scichip in self.chip_catalogs:
                wcslist.append(self.chip_catalogs[scichip]['wcs'])
            self.default_refWCS = utils.output_wcs(wcslist)

    def transformToRef(self,ref_wcs,force=False):
        """ Transform sky coords from ALL chips into X,Y coords in reference WCS.
        """
        if not isinstance(ref_wcs, pywcs.WCS):
            print 'Reference WCS not a valid HSTWCS object'
            raise ValueError
        # Need to concatenate catalogs from each input
        if self.outxy is None or force:
            outxy = ref_wcs.wcs_sky2pix(self.all_radec[0],self.all_radec[1],self.origin)
            # convert outxy list to a Nx2 array
            self.outxy = np.column_stack([outxy[0][:,np.newaxis],outxy[1][:,np.newaxis]])
            if self.pars['writecat']:
                catname = self.rootname+"_refxy_catalog.coo"
                self.write_outxy(catname)
                self.catalog_names['ref_xy'] = catname

    def sortSkyCatalog(self):
        """ Sort and clip the source catalog based on the flux range specified
            by the user
            It keeps a copy of the original full list in order to support iteration
        """
        _sortKeys = ['fluxmax','fluxmin','nbright']
        clip_catalog = False
        clip_prefix = ''
        for k in _sortKeys:
            for p in self.pars.keys():
                pindx = p.find(k)
                if pindx >= 0 and self.pars[p] is not None:
                    clip_catalog = True
                    print 'found a match for ',p,self.pars[p]
                    # find prefix (if any)
                    clip_prefix = p[:pindx].strip()

        all_radec = None
        if clip_catalog:
            # Start by clipping by any specified flux range
            if self.pars[clip_prefix+'fluxmax'] is not None or \
                    self.pars[clip_prefix+'fluxmin'] is not None:
                clip_catalog = True
                if self.pars[clip_prefix+'fluxmin'] is not None:
                    fluxmin = self.pars[clip_prefix+'fluxmin']
                else:
                    fluxmin = self.all_radec[2].min()

                if self.pars[clip_prefix+'fluxmax'] is not None:
                    fluxmax = self.pars[clip_prefix+'fluxmax']
                else:
                    fluxmax = self.all_radec[2].max()
                
                # apply flux limit clipping
                minindx = self.all_radec_orig[2] >= fluxmin
                maxindx = self.all_radec_orig[2] <= fluxmax
                flux_indx = np.bitwise_and(minindx,maxindx)
                all_radec = []
                all_radec.append(self.all_radec_orig[0][flux_indx])
                all_radec.append(self.all_radec_orig[1][flux_indx])
                all_radec.append(self.all_radec_orig[2][flux_indx])
                all_radec.append(self.all_radec_orig[3][flux_indx])

            if self.pars.has_key(clip_prefix+'nbright') and \
                    self.pars[clip_prefix+'nbright'] is not None:
                clip_catalog = True
                # pick out only the brightest 'nbright' sources
                if self.pars[clip_prefix+'fluxunits'] == 'mag':
                    nbslice = slice(None,nbright)
                else:
                    nbslice = slice(nbright,None)
                
                if all_radec is None:
                    # work on copy of all original data
                    all_radec = copy.deepcopy(self.all_radec_orig)
                # find indices of brightest
                nbright_indx = np.argsort(all_radec[2])[nbslice] 
                self.all_radec[0] = all_radec[0][nbright_indx]
                self.all_radec[1] = all_radec[1][nbright_indx]
                self.all_radec[2] = all_radec[2][nbright_indx]
                self.all_radec[3] = all_radec[3][nbright_indx]
    
            else:
                if all_radec is not None:
                    self.all_radec = copy.deepcopy(all_radec)            


    def match(self,ref_outxy, refWCS, refname, **kwargs):
        """ Uses xyxymatch to cross-match sources between this catalog and
            a reference catalog (refCatalog).
        """
        self.sortSkyCatalog() # apply any catalog sorting specified by the user
        self.transformToRef(refWCS)
        self.refWCS = refWCS
        # extract xyxymatch parameters from input parameters
        matchpars = kwargs.copy()
        self.match_pars = matchpars
        minobj = matchpars['minobj'] # needed for later
        del matchpars['minobj'] # not needed in xyxymatch

        # Check to see whether or not it is being matched to itself
        if (refname.strip() == self.name.strip()) or (
                ref_outxy.shape == self.outxy.shape) and (
                ref_outxy == self.outxy).all():
            self.identityfit = True
            print 'NO fit performed for reference image: ',self.name,'\n'
        else:
            # convert tolerance from units of arcseconds to pixels, as needed
            radius = matchpars['searchrad']
            if matchpars['searchunits'] == 'arcseconds':
                radius /= refWCS.pscale

            # Determine xyoff (X,Y offset) and tolerance to be used with xyxymatch
            use2d = True
            if matchpars['use2dhist']:
                zpxoff,zpyoff,flux,zpqual = tweakutils.build_xy_zeropoint(self.outxy,
                                    ref_outxy,searchrad=radius,histplot=matchpars['see2dplot'])
                if zpqual > 2.0:
                    xyoff = (zpxoff,zpyoff)
                    # set tolerance as well
                    xyxytolerance = 3.0
                    xyxysep = 0.0
                else:
                    use2d = False
            if not use2d:
                xoff = 0.
                yoff = 0.
                if matchpars['xoffset'] is not None:
                    xoff = matchpars['xoffset']
                if matchpars['yoffset'] is not None:
                    yoff = matchpars['yoffset']
                xyoff = (xoff,yoff)
                # set tolerance 
                xyxytolerance = matchpars['tolerance']
                xyxysep = matchpars['separation']

            matches = xyxymatch(self.outxy,ref_outxy,origin=xyoff,
                                tolerance=xyxytolerance,separation=xyxysep)
            if len(matches) > minobj:
                self.matches['image'] = np.column_stack([matches['input_x'][:,
                                np.newaxis],matches['input_y'][:,np.newaxis]])
                self.matches['ref'] = np.column_stack([matches['ref_x'][:,
                                np.newaxis],matches['ref_y'][:,np.newaxis]])
                self.matches['ref_indx'] = matches['ref_idx']
                self.matches['img_indx'] = self.all_radec[3][matches['input_idx']]
                print 'Found %d matches for %s...'%(len(matches),self.name)

                if self.pars['writecat']:
                    matchfile = open(self.catalog_names['match'],mode='w+')
                    matchfile.write('#Reference: %s\n'%refname)
                    matchfile.write('#Input: %s\n'%self.name)
                    matchfile.write('#Ref_X        Ref_Y            Input_X        Input_Y         Ref_ID    Input_ID\n')
                    for i in xrange(len(matches['input_x'])):
                        linestr = "%0.6f    %0.6f        %0.6f    %0.6f        %d    %d\n"%\
                            (matches['ref_x'][i],matches['ref_y'][i],\
                             matches['input_x'][i],matches['input_y'][i],
                            matches['ref_idx'][i],matches['input_idx'][i])
                        matchfile.write(linestr)
                    matchfile.close()
            else:
                print 'WARNING: Not enough matches found for input image: ',self.name
                self.goodmatch = False


    def performFit(self,**kwargs):
        """ Perform a fit between the matched sources

            Parameters
            ----------
            kwargs : dict
                Parameter necessary to perform the fit; namely, *fitgeometry*

            Notes
            -----
            This task still needs to implement (eventually) interactive iteration of
                   the fit to remove outliers
        """
        pars = kwargs.copy()
        self.fit_pars = pars

        if not self.identityfit:
            if self.matches is not None and self.goodmatch:
                self.fit = linearfit.iter_fit_all(
                    self.matches['image'],self.matches['ref'],
                    self.matches['img_indx'],self.matches['ref_indx'],
                    mode=pars['fitgeometry'],nclip=pars['nclip'],
                    sigma=pars['sigma'],minobj=pars['minobj'],
                    center=self.refWCS.wcs.crpix)

                print 'Computed ',pars['fitgeometry'],' fit for ',self.name,': '
                print 'XSH: %0.6g  YSH: %0.6g    ROT: %0.6g    SCALE: %0.6g'%(
                    self.fit['offset'][0],self.fit['offset'][1], 
                    self.fit['rot'],self.fit['scale'][0])
                print 'XRMS: %0.6g    YRMS: %0.6g\n'%(
                        self.fit['rms'][0],self.fit['rms'][1])
                print 'Final solution based on ',self.fit['img_coords'].shape[0],' objects.'
                
                self.write_fit_catalog()

                # Plot residuals, if requested by the user
                if pars.has_key('residplot') and "No" not in pars['residplot']:
                    xy = self.fit['img_coords']
                    #resids = linearfit.compute_resids(xy,self.fit['ref_coords'],self.fit)
                    resids = self.fit['resids']
                    xy_fit = xy + resids
                    title_str = 'Residuals\ for\ %s'%(self.name.replace('_','\_'))
                    if pars['residplot'] == 'vector':
                        ptype = True
                    else:
                        ptype = False
                    tweakutils.make_vector_plot(None,data=[xy[:,0],xy[:,1],xy_fit[:,0],xy_fit[:,1]],
                            vector=ptype,title=title_str)
                    a = raw_input("Press ENTER to continue to the next image's fit or 'q' to quit immediately...")
                    if 'q' in a.lower():
                        self.perform_update = False
                        
        else:
            self.fit = {'offset':[0.0,0.0],'rot':0.0,'scale':[1.0]}

    def updateHeader(self,wcsname=None):
        """ Update header of image with shifts computed by *perform_fit()*
        """
        if not self.perform_update:
            return
        # Create WCSCORR table to keep track of WCS revisions anyway
        wcscorr.init_wcscorr(self.name)

        if not self.identityfit and self.goodmatch:
            updatehdr.updatewcs_with_shift(self.name,self.refWCS,wcsname=wcsname,
                xsh=self.fit['offset'][0],ysh=self.fit['offset'][1],rot=self.fit['rot'],scale=self.fit['scale'][0])
        if self.identityfit:
            # archive current WCS as alternate WCS with specified WCSNAME
            extlist = []
            if self.num_sci == 1 and self.ext_name == "PRIMARY":
                extlist = [0]
            else:
                for ext in range(1,self.num_sci+1):
                    extlist.append((self.ext_name,ext))

            next_key = stwcs.wcsutil.altwcs.next_wcskey(pyfits.getheader(self.name,extlist[0]))
            stwcs.wcsutil.altwcs.archiveWCS(self.name,extlist,wcskey=next_key,wcsname=wcsname)

            # copy updated WCS info to WCSCORR table
            if self.num_sci > 0 and self.ext_name != "PRIMARY":
                fimg = pyfits.open(self.name,mode='update')
                stwcs.wcsutil.wcscorr.update_wcscorr(fimg,wcs_id=wcsname)
                fimg.close()

    def write_skycatalog(self,filename):
        """ Write out the all_radec catalog for this image to a file
        """
        ralist = self.all_radec[0].tolist()
        declist = self.all_radec[1].tolist()
        f = open(filename,'w')
        f.write("#Sky positions for: "+self.name+'\n')
        f.write("#RA        Dec\n")
        f.write("#(deg)     (deg)\n")
        for i in xrange(len(ralist)):
            f.write('%0.8g  %0.8g\n'%(ralist[i],declist[i]))
        f.close()

    def write_fit_catalog(self):
        """ Write out the catalog of all sources and resids used in the final fit.
        """
        if self.pars['writecat']:
            print 'Creating catalog for the fit: ',self.catalog_names['fitmatch']
            f = open(self.catalog_names['fitmatch'],'w')
            f.write('# Input image: %s\n'%self.rootname)
            f.write('# Coordinate mapping parameters: \n')
            f.write('#    X and Y rms: %20.6g  %20.6g\n'%(self.fit['rms'][0],self.fit['rms'][1]))
            f.write('#    X and Y shift: %20.6g  %20.6g\n '%(self.fit['offset'][0],self.fit['offset'][1]))
            f.write('#    X and Y scale: %20.6g  %20.6g\n'%(self.fit['scale'][0],self.fit['scale'][1]))
            f.write('#    X and Y rotation: %20.6g \n'%(self.fit['rot']))
            f.write('# \n# Input Coordinate Listing\n')
            f.write('#     Column 1: X (reference)\n') 
            f.write('#     Column 2: Y (reference)\n')
            f.write('#     Column 3: X (input)\n')
            f.write('#     Column 4: Y (input)\n')
            f.write('#     Column 5: X (fit)\n')
            f.write('#     Column 6: Y (fit)\n')
            f.write('#     Column 7: X (residual)\n')
            f.write('#     Column 8: Y (residual)\n')
            f.write('#     Column 9: Ref ID\n')
            f.write('#     Column 10: Input ID\n')
            
            f.write('#\n')
            f.close()
            fitvals = self.fit['img_coords']+self.fit['resids']
            xydata = [[self.fit['ref_coords'][:,0],self.fit['ref_coords'][:,1],
                      self.fit['img_coords'][:,0],self.fit['img_coords'][:,1],
                      fitvals[:,0],fitvals[:,1],
                      self.fit['resids'][:,0],self.fit['resids'][:,1]],
                      [self.fit['ref_indx'],self.fit['img_indx']]
                    ]
            tweakutils.write_xy_file(self.catalog_names['fitmatch'],xydata,append=True,format=["%20.6f","%8d"])
        
    def write_outxy(self,filename):
        """ Write out the output(transformed) XY catalog for this image to a file
        """
        f = open(filename,'w')
        f.write("#Pixel positions for: "+self.name+'\n')
        f.write("#X           Y\n")
        f.write("#(pix)       (pix)\n")
        for i in xrange(self.all_radec[0].shape[0]):
            f.write('%g  %g\n'%(self.outxy[i,0],self.outxy[i,1]))
        f.close()

    def get_shiftfile_row(self):
        """ Return the information for a shiftfile for this image to provide
            compatability with the IRAF-based MultiDrizzle
        """
        if self.fit is not None:
            rowstr = '%s    %0.6g  %0.6g    %0.6g     %0.6g\n'%(self.name,self.fit['offset'][0],self.fit['offset'][1],self.fit['rot'],self.fit['scale'][0])
        else:
            rowstr = None
        return rowstr


    def clean(self):
        """ Remove intermediate files created
        """            
        for f in self.catalog_names:
            if 'match' in f:
                if os.path.exists(self.catalog_names[f]): 
                    print 'Deleting intermediate match file: ',self.catalog_names[f]
                    os.remove(self.catalog_names[f])
            else:
                for extn in f:
                    if os.path.exists(extn): 
                        print 'Deleting intermediate catalog: ',extn
                        os.remove(extn)

class RefImage(object):
    """ This class provides all the information needed by to define a reference
        tangent plane and list of source positions on the sky.
    """
    def __init__(self,wcs_list,catalog,**kwargs):
        if isinstance(wcs_list,list):
            # generate a reference tangent plane from a list of STWCS objects
            undistort = True
            if wcs_list[0].sip is None:
                undistort=False
            self.wcs = utils.output_wcs(wcs_list,undistort=undistort)
            self.wcs.filename = wcs_list[0].filename
        else:
            # only a single WCS provided, so use that as the definition
            if not isinstance(wcs_list,stwcs.wcsutil.HSTWCS): # User only provided a filename
                self.wcs = stwcs.wcsutil.HSTWCS(wcs_list)
            else: # User provided full HSTWCS object
                self.wcs = wcs_list

        self.name = self.wcs.filename
        self.refWCS = None
        # Interpret the provided catalog
        self.catalog = catalogs.RefCatalog(None,catalog,**kwargs)
        self.catalog.buildCatalogs()
        self.all_radec = self.catalog.radec
        self.origin = 1
        self.pars = kwargs
        
        # convert sky positions to X,Y positions on reference tangent plane
        self.transformToRef()

    def write_skycatalog(self,filename):
        """ Write out the all_radec catalog for this image to a file
        """
        f = open(filename,'w')
        f.write("#Sky positions for: "+self.name+'\n')
        f.write("#RA        Dec\n")
        f.write("#(deg)     (deg)\n")
        for i in xrange(self.all_radec[0].shape[0]):
            f.write('%g  %g\n'%(self.all_radec[0][i],self.all_radec[1][i]))
        f.close()

    def transformToRef(self):
        """ Transform reference catalog sky positions (self.all_radec)
        to reference tangent plane (self.wcs) to create output X,Y positions
        """
        if self.pars.has_key('refxyunits') and self.pars['refxyunits'] == 'pixels':
            print 'Creating RA/Dec positions for reference sources...'
            self.outxy = np.column_stack([self.all_radec[0][:,np.newaxis],self.all_radec[1][:,np.newaxis]])
            skypos = self.wcs.wcs_pix2sky(self.all_radec[0],self.all_radec[1],self.origin)
            self.all_radec = np.column_stack([skypos[0][:,np.newaxis],skypos[1][:,np.newaxis]])
        else:
            print 'Converting RA/Dec positions of reference sources to X,Y positions in reference WCS...'
            self.refWCS = self.wcs
            outxy = self.wcs.wcs_sky2pix(self.all_radec[0],self.all_radec[1],self.origin)
            # convert outxy list to a Nx2 array
            self.outxy = np.column_stack([outxy[0][:,np.newaxis],outxy[1][:,np.newaxis]])
            

    def get_shiftfile_row(self):
        """ Return the information for a shiftfile for this image to provide
            compatability with the IRAF-based MultiDrizzle
        """
        rowstr = '%s    0.0  0.0    0.0     1.0\n'%(self.name)
        return rowstr

    def clean(self):
        """ Remove intermediate files created
        """
        if not util.is_blank(self.catalog.catname) and os.path.exists(self.catalog.catname):
            os.remove(self.catalog.catname)

def build_referenceWCS(catalog_list):
    """ Compute default reference WCS from list of Catalog objects
    """
    wcslist = []
    for catalog in catalog_list:
        for scichip in catalog.catalogs:
            wcslist.append(catalog.catalogs[scichip]['wcs'])
    return utils.output_wcs(wcslist)

def count_sci_extensions(filename):
    """ Return the number of SCI extensions and the EXTNAME from a input MEF file
    """
    num_sci = 0
    extname = 'SCI'
    num_ext = 0
    for extn in fu.openImage(filename):
        num_ext += 1
        if extn.header.has_key('extname') and extn.header['extname'] == extname:
            num_sci += 1
    if num_sci == 0:
        extname = 'PRIMARY'
        num_sci = 1

    return num_sci,extname