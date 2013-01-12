import os
import re
import glob
import json
import shutil
import random
import binascii
import mimetypes
import webbrowser
import multiprocessing as mp
import numpy as np
from scipy.stats import scoreatpercentile

from tornado import web, template

from .. import utils

import serve

sloader = template.Loader(serve.cwd)
lloader = template.Loader("./")

name_parse = re.compile(r".*/(\w+).png")
colormaps = glob.glob(os.path.join(serve.cwd, "resources/colormaps/*.png"))
colormaps = [(name_parse.match(cm).group(1), serve.make_base64(cm)) for cm in sorted(colormaps)]

def _normalize_data(data, mask):
    if not isinstance(data, dict):
        data = dict(data0=data)

    json = dict()
    json['__order__'] = data.keys()
    for name, dat in data.items():
        ds = dict(__class__="Dataset")

        if isinstance(dat, dict):
            data = _fixarray(dat['data'], mask)
            if 'stim' in dat:
                ds['stim'] = dat['stim']
            ds['delay'] = dat['delay'] if 'delay' in dat else 0
        elif isinstance(dat, np.ndarray):
            data = _fixarray(dat, mask)

        ds['data'] = data
        ds['min'] = scoreatpercentile(data.ravel(), 1) if 'min' not in dat else dat['min']
        ds['max'] = scoreatpercentile(data.ravel(), 99) if 'max' not in dat else dat['max']
        if 'cmap' in dat:
            ds['cmap'] = dat['cmap']
        if 'rate' in dat:
            ds['rate'] = dat['rate']

        json[name] = ds

    return json

def _make_bindat(json, fmt="%s.bin"):
    newjs, bindat = dict(), dict()
    for name, data in json.items():
        if "data" in data:
            newjs[name] = dict(data)
            newjs[name]['data'] = fmt%name
            bindat[name] = serve.make_binarray(data['data'])
        else:
            newjs[name] = data

    return newjs, bindat

def _fixarray(data, mask):
    if isinstance(data, str):
        import nibabel
        data = nibabel.load(data).get_data().T
    if data.dtype == np.float:
        data = data.astype(np.float32)

    raw = data.dtype.type == np.uint8
    assert raw and data.shape[0] in [3, 4] or not raw
    shape = data.shape[1:] if raw else data.shape
    movie = len(shape) in [2, 4]
    volume = len(shape) in [3, 4]

    if raw:
        if volume:
            if movie:
                return data[:, :, mask].transpose(1, 2, 0)
            else:
                return data[:, mask].T
        else: #cortical
            if movie:
                return data.transpose(1,2,0)
            else:
                return data.T
    else: #regular
        if volume:
            if movie:
                return data[:, mask]
            else:
                return data[mask]
        else: #cortical
            return data

def make_movie(stim, outfile, fps=15, size="640x480"):
    import shlex
    import subprocess as sp
    cmd = "ffmpeg -r {fps} -i {infile} -b 4800k -g 30 -s {size} -vcodec libtheora {outfile}.ogv"
    fcmd = cmd.format(infile=stim, size=size, fps=fps, outfile=outfile)
    sp.call(shlex.split(fcmd))

def make_static(outpath, data, subject, xfmname, types=("inflated",), recache=False, cmap="RdBu_r", template="static.html", anonymize=False, **kwargs):
    '''
    Creates a static instance of the webGL MRI viewer that can easily be posted 
    or shared. 

    Parameters
    ----------
    outpath : string
        The directory where the static viewer will be saved. Will be created if it
        doesn't already exist.
    data : array_like or dict
        The data to be displayed on the surface. For details see docs for show().
    subject : string
        Subject identifier (e.g. "JG").
    xfmname : string
        Name of functional -> anatomical transform.
    types : tuple, optional
        Types of surfaces to include. Fiducial and flat surfaces are automatically
        included. Default ("inflated",)
    recache : bool, optional
        Whether to recreate CTM and SVG files for surfaces. Default False
    cmap : string, optional
        Name of default colormap used to show data. Default "RdBu_r"
    template : string, optional
        Name of template HTML file. Default "static.html"
    anonymize : bool, optional
        Whether to rename CTM and SVG files generically, for public distribution.
        Default False
    **kwargs : dict, optional
        All additional keyword arguments are passed to the template renderer.
    '''
    print "You'll probably need nginx to view this, since file:// paths don't handle xsrf correctly"
    outpath = os.path.abspath(os.path.expanduser(outpath)) # To handle ~ expansion
    if not os.path.exists(outpath):
        os.makedirs(outpath)

    #Create a new mg2 compressed CTM and move it into the outpath
    ctmfile = utils.get_ctmpack(subject, xfmname, types, method='mg2', level=9, recache=recache)
    oldpath, fname = os.path.split(ctmfile)
    fname, ext = os.path.splitext(fname)

    ## Rename files to anonymize?
    if anonymize:
        newfname = "surface"
    else:
        newfname = fname

    for ext in ['json','ctm', 'svg']:
        newfile = os.path.join(outpath, "%s.%s"%(newfname, ext))
        if os.path.exists(newfile):
            os.unlink(newfile)
        
        shutil.copy2(os.path.join(oldpath, "%s.%s"%(fname, ext)), newfile)

        if ext == "json" and anonymize:
            ## change filenames in json
            nfh = open(newfile)
            jsoncontents = nfh.read()
            nfh.close()
            
            ofh = open(newfile, "w")
            ofh.write(jsoncontents.replace(fname, newfname))
            ofh.close()

    #ctmfile = os.path.split(ctmfile)[1]
    ctmfile = newfname+".json"

    #Generate the data binary objects and save them into the outpath
    mask = utils.get_cortical_mask(subject, xfmname)
    json, sdat = _make_bindat(_normalize_data(data, mask))
    for name, dat in sdat.items():
        with open(os.path.join(outpath, "%s.bin"%name), "w") as binfile:
            binfile.write(dat)
    
    #Parse the html file and paste all the js and css files directly into the html
    import htmlembed
    if os.path.exists(os.path.join("./", template)):
        template = lloader.load(template)
    else:
        template = sloader.load(template)
    html = template.generate(ctmfile=ctmfile, data=json, colormaps=colormaps, default_cmap=cmap, python_interface=False, **kwargs)
    htmlembed.embed(html, os.path.join(outpath, "index.html"))


def show(data, subject, xfmname, types=("inflated",), recache=False, cmap="RdBu_r", autoclose=True, open_browser=True, port=None):
    '''Data can be a dictionary of arrays. Alternatively, the dictionary can also contain a 
    sub dictionary with keys [data, stim, delay].

    Data array can be a variety of shapes:
    Raw volume movie:      [[3, 4], t, z, y, x]
    Raw volume image:      [[3, 4], z, y, x]
    Raw cortical movie:    [[3, 4], t, vox]
    Raw cortical image:    [[3, 4], vox]
    Regular volume movie:  [t, z, y, x]
    Regular volume image:  [z, y, x]
    Regular cortical movie: [t, vox]
    Regular cortical image: [vox]
    '''
    html = sloader.load("mixer.html")

    ctmfile = utils.get_ctmpack(subject, xfmname, types, method='raw', level=0, recache=recache)
    mask = utils.get_cortical_mask(subject, xfmname)
    jsondat, bindat = _make_bindat(_normalize_data(data, mask), fmt='data/%s/')

    saveevt = mp.Event()
    saveimg = mp.Array('c', 8192)
    queue = mp.Queue()
    
    class CTMHandler(web.RequestHandler):
        def get(self, path):
            fpath = os.path.split(ctmfile)[0]
            if path == '':
                self.set_header("Content-Type", "application/json")
                self.write(open(ctmfile).read())
            else:
                mtype = mimetypes.guess_type(os.path.join(fpath, path))[0]
                if mtype is None:
                    mtype = "application/octet-stream"
                self.set_header("Content-Type", mtype)
                self.write(open(os.path.join(fpath, path)).read())

    class DataHandler(web.RequestHandler):
        def get(self, path):
            path = path.strip("/")
            if not queue.empty():
                d = queue.get()
                print "Got new data: %r"%d.keys()
                bindat.update(d)

            if path in bindat:
                self.write(bindat[path])
            else:
                self.set_status(404)
                self.write_error(404)

    class MixerHandler(web.RequestHandler):
        def get(self):
            self.set_header("Content-Type", "text/html")
            self.write(html.generate(data=jsondat, colormaps=colormaps, default_cmap=cmap, python_interface=True))

        def post(self):
            print "saving file to %s"%saveimg.value
            data = self.get_argument("svg", default=None)
            png = self.get_argument("png", default=None)
            with open(saveimg.value, "wb") as svgfile:
                if png is not None:
                    data = png[22:].strip()
                    try:
                        data = binascii.a2b_base64(data)
                    except:
                        print "Error writing image!"
                        data = png
                svgfile.write(data)
            saveevt.set()

    class JSMixer(serve.JSProxy):
        def addData(self, **kwargs):
            Proxy = serve.JSProxy(self.send, "window.viewer.addData")
            json, data = _make_bindat(_normalize_data(kwargs, mask), fmt='data/%s/')
            queue.put(data)
            return Proxy(json)

        def saveflat(self, filename, height=1024):
            Proxy = serve.JSProxy(self.send, "window.viewer.saveflat")
            saveimg.value = filename
            return Proxy(height, "mixer.html")

        def saveIMG(self, filename):
            Proxy = serve.JSProxy(self.send, "window.viewer.saveIMG")
            saveimg.value = filename
            return Proxy("mixer.html")

        def makeMovie(self, animation, filename="brainmovie%07d.png", fps=30, shape=(1920, 1080)):
            state = dict()
            anim = []
            for f in sorted(animation, key=lambda x:x['idx']):
                if f['idx'] == 0:
                    self.setState(f['state'], f['value'])
                    state[f['state']] = dict(idx=f['idx'], val=f['value'])
                else:
                    if f['state'] not in state:
                        state[f['state']] = dict(idx=0, val=self.getState(f['state'])[0])
                    start = dict(idx=state[f['state']]['idx'], state=f['state'], value=state[f['state']]['val'])
                    end = dict(idx=f['idx'], state=f['state'], value=f['value'])
                    state[f['state']]['idx'] = f['idx']
                    state[f['state']]['val'] = f['value']
                    if start['value'] != end['value']:
                        anim.append((start, end))

            print anim
            self.resize(*shape)
            for i, sec in enumerate(np.arange(0, anim[-1][1]['idx'], 1./fps)):
                for start, end in anim:
                    if start['idx'] < sec < end['idx']:
                        idx = (sec - start['idx']) / (end['idx'] - start['idx'])
                        val = np.array(start['value']) * (1-idx) + np.array(end['value']) * idx
                        if isinstance(val, np.ndarray):
                            self.setState(start['state'], list(val))
                        else:
                            self.setState(start['state'], val)
                saveevt.clear()
                self.saveIMG(filename%i)
                saveevt.wait()

    class WebApp(serve.WebApp):
        disconnect_on_close = autoclose
        def get_client(self):
            self.c_evt.wait()
            self.c_evt.clear()
            return JSMixer(self.send, "window.viewer")
    if port is None:
        port = random.randint(1024, 65536)
        
    server = WebApp([
            (r'/ctm/(.*)', CTMHandler),
            (r'/data/(.*)', DataHandler),
            (r'/mixer.html', MixerHandler),
        ], port)
    server.start()
    print "Started server on port %d"%server.port
    if open_browser:
        webbrowser.open("http://%s:%d/mixer.html"%(serve.hostname, server.port))

        client = server.get_client()
        client.server = server
        return client