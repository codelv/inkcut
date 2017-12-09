"""
Copyright (c) 2017, Jairus Martin.

Distributed under the terms of the GPL v3 License.

The full license is in the file LICENSE, distributed with this software.

Created on Jul 12, 2015

@author: jrm
"""
import os
import enaml
from atom.api import Instance, Enum, List, observe
from inkcut.core.api import Plugin, unit_conversions, log

from .models import Job, JobError, Material


class JobPlugin(Plugin):

    #: Units
    units = Enum(*unit_conversions.keys()).tag(config=True)

    #: Available materials
    materials = List(Material).tag(config=True)

    #: Current material
    material = Instance(Material, ()).tag(config=True)

    #: Previous jobs
    jobs = List(Job).tag(config=True)

    #: Current job
    job = Instance(Job).tag(config=True)

    def _default_job(self):
        return Job(material=self.material)

    def _default_units(self):
        return 'in'

    # -------------------------------------------------------------------------
    # Plugin API
    # -------------------------------------------------------------------------
    def start(self):
        """ Register the plugins this plugin depends on

        """
        w = self.workbench
        with enaml.imports():
            from inkcut.device.manifest import DeviceManifest
            w.register(DeviceManifest())

        #: Now load state
        super(JobPlugin, self).start()

        #: If we loaded from state, refresh
        if self.job.document:
            self._refresh_preview({})

    # -------------------------------------------------------------------------
    # Job API
    # -------------------------------------------------------------------------
    def open_document(self, path):
        """ Set the job.document if it is empty, otherwise close and create
        a  new Job instance.
        
        """
        if not os.path.exists(path):
            raise JobError("Cannot open %s, it does not exist!" % path)

        if not os.path.isfile(path):
            raise JobError("Cannot open %s, it is not a file!" % path)

        # Close any old docs
        self.close_document()

        log.info("Opening {doc}".format(doc=path))
        try:
            self.job.document = path
        except ValueError as e:
            #: Wrap in a JobError
            raise JobError(e)

        #: Copy so the ui's update
        jobs = self.jobs[:]
        jobs.append(self.job)
        self.jobs = jobs

    def close_document(self):
        """ If the job currently has a "document" add this to the jobs list
        and create a new Job instance. Otherwise no job is open so do nothing.
        
        """
        if not self.job.document:
            return

        log.info("Closing {doc}".format(doc=self.job.document))
        #: Create a new default job
        self.job = self._default_job()

    @observe('job.material')
    def _observe_material(self, change):
        """ Keep the job material and plugin material in sync.
        
        """
        m = self.material
        job = self.job
        if job.material != m:
            job.material = m

    @observe('job', 'job.model', 'job.material',
             'material.size', 'material.padding')
    def _refresh_preview(self, change):
        """ Redraw the preview on the screen 
        
        """
        log.info(change)
        view_items = []

        #: Transform used by the view
        preview_plugin = self.workbench.get_plugin('inkcut.preview')
        job = self.job
        plot = preview_plugin.plot
        t = preview_plugin.transform

        #: Draw the device
        plugin = self.workbench.get_plugin('inkcut.device')
        device = plugin.device
        if device and device.area:
            area = device.area
            view_items.append(
                dict(path=device.area.path*t, pen=plot.pen_device,
                     skip_autorange=(False, [area.size[0], 0]))
            )

        #: The model is only set when a document is open and has no errors
        if job.model:
            view_items.extend([
                dict(path=job.move_path, pen=plot.pen_up),
                dict(path=job.cut_path, pen=plot.pen_down)
            ])
            #: TODO: This
            #if self.show_offset_path:
            #    view_items.append(PainterPathPlotItem(
            # self.job.offset_path,pen=self.pen_offset))
        if job.material:
            # Also observe any change to job.media and job.device
            view_items.extend([
                dict(path=job.material.path*t, pen=plot.pen_media,
                     skip_autorange=(False, [0, job.size[1]])),
                dict(path=job.material.padding_path*t,
                     pen=plot.pen_media_padding, skip_autorange=True)
            ])

        #: Update the plot
        preview_plugin.set_preview(*view_items)
