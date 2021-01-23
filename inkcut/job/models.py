# -*- coding: utf-8 -*-
"""
Copyright (c) 2015-2020, the Inkcut team.

Distributed under the terms of the GPL v3 License.

The full license is in the file LICENSE, distributed with this software.

Created on Jan 16, 2015

@author: jrm
"""
from __future__ import division
import os
import sys
from datetime import datetime, timedelta
from atom.api import (
    Enum, Float, Int, Bool, Instance, ContainerList, Range, Str,
    Dict, Callable, observe
)
from contextlib import contextmanager
from enaml.qt.QtGui import QPainterPath, QTransform
from enaml.qt.QtCore import QPointF, QRectF
from enaml.colors import ColorMember
from inkcut.core.api import Model, AreaBase
from inkcut.core.svg import QtSvgDoc
from inkcut.core.utils import split_painter_path, log


from . import filters
from . import ordering


class Material(AreaBase):
    """ Model representing the plot media
    """
    name = Str().tag(config=True)
    color = Str('#000000').tag(config=True)

    is_roll = Bool(False).tag(config=True)

    used = ContainerList(Float(), default=[0, 0]).tag(
        config=True, help="amount used already (to determine available size)")
    cost = Float(1).tag(config=True, help="cost per square unit")

    use_force = Bool(False).tag(config=True)
    use_speed = Bool(False).tag(config=True)
    force = Int(10).tag(config=True)
    speed = Int(10).tag(config=True)

    def reset(self):
        self.used = (0.0, 0.0)

    def unit_cost(self):
        return


class Padding:
    LEFT = 0
    TOP = 1
    RIGHT = 2
    BOTTOM = 3


class JobError(Exception):
    pass


class JobInfo(Model):
    """ Job metadata """
    #: Controls
    done = Bool()
    cancelled = Bool()
    paused = Bool()

    #: Flags
    status = Enum('staged', 'waiting', 'running', 'error',
                  'approved', 'cancelled', 'complete').tag(config=True)

    #: Stats
    created = Instance(datetime).tag(config=True)
    started = Instance(datetime).tag(config=True)
    ended = Instance(datetime).tag(config=True)
    progress = Range(0, 100, 0).tag(config=True)
    data = Str().tag(config=True)
    count = Int().tag(config=True)

    #: Device speed in px/s
    speed = Float(strict=False).tag(config=True)
    #: Length in px
    length = Float(strict=False).tag(config=True)

    #: Estimates based on length and speed
    duration = Instance(timedelta, ()).tag(config=True)

    #: Units
    units = Enum('in', 'cm', 'm', 'ft').tag(config=True)

    #: Callback to open the approval dialog
    auto_approve = Bool().tag(config=True)
    request_approval = Callable()

    def __init__(self, *args, **kwargs):
        super(JobInfo, self).__init__(*args, **kwargs)
        self.created = self._default_created()

    def _default_created(self):
        return datetime.now()

    def _default_request_approval(self):
        """ Request approval using the current job """
        from inkcut.core.workbench import InkcutWorkbench
        workbench = InkcutWorkbench.instance()
        plugin = workbench.get_plugin("inkcut.job")
        return lambda: plugin.request_approval(plugin.job)

    def reset(self):
        """ Reset to initial states"""
        #: TODO: This is a stupid design
        self.progress = 0
        self.paused = False
        self.cancelled = False
        self.done = False
        self.status = 'staged'

    def _observe_done(self, change):
        if change['type'] == 'update':
            #: Increment count every time it's completed
            if self.done:
                self.count += 1

    @observe('length', 'speed')
    def _update_duration(self, change):
        if not self.length or not self.speed:
            self.duration = timedelta()
            return
        dt = self.length/self.speed
        self.duration = timedelta(seconds=dt)


class Job(Model):
    """ Create a plot depending on the properties set. Any property that is a
    traitlet will cause an update when the value is changed.

    """
    #: Material this job will be run on
    material = Instance(Material, ()).tag(config=True)

    #: Path to svg document this job parses
    document = Str().tag(config=True)

    #: Nodes to restrict
    document_kwargs = Dict().tag(config=True)

    #: Meta info a the job
    info = Instance(JobInfo, ()).tag(config=True)

    # Job properties used for generating the plot
    size = ContainerList(Float(), default=[1, 1])
    scale = ContainerList(Float(), default=[1, 1]).tag(config=True)
    auto_scale = Bool(False).tag(
        config=True, help="automatically scale if it's too big for the area")
    lock_scale = Bool(True).tag(
        config=True, help="automatically scale if it's too big for the area")

    mirror = ContainerList(Bool(), default=[False, False]).tag(config=True)
    align_center = ContainerList(Bool(),
                                 default=[False, False]).tag(config=True)

    # Shifting of original file
    auto_shift = Bool(True).tag(config=True, help="shift to start at origin")
    copy_bbox = Instance(QRectF)

    rotation = Float(0).tag(config=True)
    auto_rotate = Bool(False).tag(
        config=True, help="automatically rotate if it saves space")

    copies = Int(1).tag(config=True)
    auto_copies = Bool(False).tag(
        config=True, help="always use a full stack")
    copy_spacing = ContainerList(Float(),
                                 default=[10, 10]).tag(config=True)
    copy_weedline = Bool(False).tag(config=True)
    copy_weedline_padding = ContainerList(
        Float(), default=[10, 10, 10, 10]).tag(config=True)

    plot_weedline = Bool(False).tag(config=True)
    plot_weedline_padding = ContainerList(
        Float(), default=[10, 10, 10, 10]).tag(config=True)

    order = Enum(*sorted(ordering.REGISTRY.keys())).tag(config=True)

    def _default_order(self):
        return 'Normal'

    feed_to_end = Bool(False).tag(config=True)
    feed_after = Float(0).tag(config=True)

    stack_size = ContainerList(Int(), default=[0, 0])

    #: Filters to cut only certain items
    filters = ContainerList(filters.JobFilter)

    #: Original path parsed from the source document
    doc = Instance(QtSvgDoc)
    path = Instance(QtSvgDoc)

    #: Path filtered by layers/colors and ordered according to the order
    optimized_path = Instance(QPainterPath)

    #: Finaly copy using all the applied job properties
    #: This is what is actually cut out
    model = Instance(QPainterPath)

    _blocked = Bool(False)  # block change events
    _desired_copies = Int(1)  # required for auto copies

    def __setstate__(self, *args, **kwargs):
        """ Ensure that when restoring from disk the material and info
        are not set to None. Ideally these would be defined as Typed but
        the material may be made extendable at some point.
        """
        super(Job, self).__setstate__(*args, **kwargs)
        if not self.info:
            self.info = JobInfo()
        if not self.material:
            self.material = Material()

    def _observe_document(self, change):
        """ Read the document from stdin """
        if change['type'] == 'update' and self.document == '-':
            #: Only load from stdin when explicitly changed to it (when doing
            #: open from the cli) otherwise when restoring state this hangs
            #: startup
            self.doc = self.path = QtSvgDoc(sys.stdin, **self.document_kwargs)
        elif self.document and os.path.exists(self.document):
            self.doc = self.path = QtSvgDoc(self.document, **self.document_kwargs)

        # Recreate available filters when the document changes
        self.filters = self._default_filters()

    def _default_filters(self):
        results = []
        if not self.path:
            return results
        for Filter in filters.REGISTRY.values():
            try:
                results.extend(Filter.get_filter_options(self, self.path))
            except Exception as e:
                log.error("Failed loading filters for: %s" % Filter)
                log.exception(e)
        return results

    def _default_optimized_path(self):
        """ Filter parts of the documen based on the selected layers and colors

        """
        doc = self.path
        for f in self.filters:
            # If the color/layer is NOT enabled, then remove that color/layer
            if not f.enabled:
                log.debug("Applying filter {}".format(f))
                doc = f.apply_filter(self, doc)

        # Apply ordering to path
        # this delegates to objects in the ordering module
        OrderingHandler = ordering.REGISTRY.get(self.order)
        if OrderingHandler:
            doc = OrderingHandler().order(self, doc)

        return doc

    @observe('path', 'order', 'filters')
    def _update_optimized_path(self, change):
        """ Whenever the loaded file (and parsed SVG path) changes update
        it based on the filters from the job.

        """
        self.optimized_path = self._default_optimized_path()

    def _create_copy(self):
        """ Creates a copy of the original graphic applying the given
        transforms

        """
        optimized_path = self.optimized_path
        bbox = optimized_path.boundingRect()

        # Create the base copy
        t = QTransform()

        t.scale(
            self.scale[0] * (self.mirror[0] and -1 or 1),
            self.scale[1] * (self.mirror[1] and -1 or 1),
            )

        self.copy_bbox = t.mapRect(bbox)

        # Rotate about center
        if self.rotation != 0:
            c = bbox.center()
            t.translate(-c.x(), -c.y())
            t.rotate(self.rotation)
            t.translate(c.x(), c.y())

        # Apply transform
        path = optimized_path * t

        # Add weedline to copy
        if self.copy_weedline:
            self._add_weedline(path, self.copy_weedline_padding)

        # If it's too big we have to scale it
        w, h = path.boundingRect().width(), path.boundingRect().height()
        available_area = self.material.available_area

        #: This screws stuff up!
        if w > available_area.width() or h > available_area.height():

            # If it's too big an auto scale is enabled, resize it to fit
            if self.auto_scale:
                sx, sy = 1, 1
                if w > available_area.width():
                    sx = available_area.width() / w
                if h > available_area.height():
                    sy = available_area.height() / h
                s = min(sx, sy)  # Fit to the smaller of the two
                path = optimized_path * QTransform.fromScale(s, s)

        # Save original bbox
        bbox = path.boundingRect()

        # Move to bottom left
        br = bbox.bottomRight()
        path = path * QTransform.fromTranslate(-br.x(), -br.y())

        return path

    @contextmanager
    def events_suppressed(self):
        """ Block change events to prevent feedback loops

        """
        self._blocked = True
        try:
            yield
        finally:
            self._blocked = False

    @observe('path', 'scale', 'auto_scale', 'lock_scale', 'mirror',
             'align_center', 'rotation', 'auto_rotate', 'copies', 'order',
             'copy_spacing', 'copy_weedline', 'copy_weedline_padding',
             'plot_weedline', 'plot_weedline_padding', 'feed_to_end',
             'feed_after', 'material', 'material.size', 'material.padding',
             'auto_copies', 'auto_shift')
    def update_document(self, change=None):
        """ Recreate an instance of of the plot using the current settings

        """
        if self._blocked:
            return

        if change:
            name = change['name']
            if name == 'copies':
                self._desired_copies = self.copies
            elif name in ('layer', 'color'):
                self._update_optimized_path(change)

        model = self.create()
        if model:
            self.model = model

    def create(self, swap_xy=False, scale=None):
        """ Create a path model that is rotated and scaled

        """
        model = QPainterPath()

        if not self.path:
            return

        path = self._create_copy()

        # Update size
        bbox = path.boundingRect()
        self.size = [bbox.width(), bbox.height()]

        # Create copies
        c = 0
        points = self._copy_positions_iter(path)

        if self.auto_copies:
            self.stack_size = self._compute_stack_sizes(path)
            if self.stack_size[0]:
                copies_left = self.copies % self.stack_size[0]
                if copies_left:  # not a full stack
                    with self.events_suppressed():
                        self.copies = self._desired_copies
                        self.add_stack()

        while c < self.copies:
            x, y = next(points)
            model.addPath(path * QTransform.fromTranslate(x, -y))
            c += 1

        # Create weedline
        if self.plot_weedline:
            self._add_weedline(model, self.plot_weedline_padding)

        # Determine padding
        bbox = model.boundingRect()
        if self.align_center[0]:
            px = (self.material.width() - bbox.width())/2.0
        else:
            px = self.material.padding_left

        if self.align_center[1]:
            py = -(self.material.height() - bbox.height())/2.0
        else:
            py = -self.material.padding_bottom

        # Scale and rotate
        if scale:
            model *= QTransform.fromScale(*scale)
            px, py = px*abs(scale[0]), py*abs(scale[1])

        if swap_xy:
            t = QTransform()
            t.rotate(90)
            model *= t

        # Move to 0,0
        bbox = model.boundingRect()
        p = bbox.bottomLeft()
        tx, ty = -p.x(), -p.y()

        if not self.auto_shift:
            # Re-add original shift
            bbox = self.copy_bbox
            tx += -bbox.right() if self.mirror[0] else bbox.left()
            ty += bbox.bottom() if self.mirror[1] else -bbox.top()

        # If swapped, make sure padding is still correct
        if swap_xy:
            px, py = -py, -px
        tx += px
        ty += py

        model = model * QTransform.fromTranslate(tx, ty)

        end_point = (QPointF(
            0, -self.feed_after + model.boundingRect().top())
                     if self.feed_to_end else QPointF(0, 0))
        model.moveTo(end_point)

        return model

    def _check_bounds(self, plot, area):
        """ Checks that the width and height of plot are less than the width
        and height of area

        """
        return plot.width() > area.width() or plot.height() > area.height()

    def _copy_positions_iter(self, path, axis=0):
        """ Generator that creates positions of points

        """
        other_axis = axis +1 % 2
        p = [0, 0]

        bbox = path.boundingRect()
        d = (bbox.width(), bbox.height())
        pad = self.copy_spacing
        stack_size = self._compute_stack_sizes(path)

        while True:
            p[axis] = 0
            yield p  # Beginning of each row

            for i in range(stack_size[axis]-1):
                p[axis] += d[axis]+pad[axis]
                yield p

            p[other_axis] += d[other_axis]+pad[other_axis]

    def _compute_stack_sizes(self, path):
        # Usable area
        material = self.material
        a = [material.width(), material.height()]
        a[0] -= material.padding[Padding.LEFT] + material.padding[Padding.RIGHT]
        a[1] -= material.padding[Padding.TOP] + material.padding[Padding.BOTTOM]

        # Clone includes weedline but not spacing
        bbox = path.boundingRect()
        size = [bbox.width(), bbox.height()]

        stack_size = [0, 0]
        p = [0, 0]
        for i in range(2):
            # Compute stack
            while (p[i]+size[i]) < a[i]:  # while another one fits
                stack_size[i] += 1
                p[i] += size[i] + self.copy_spacing[i]  # Add only to end

        self.stack_size = stack_size
        return stack_size

    def _add_weedline(self, path, padding):
        """ Adds a weedline to the path
        by creating a box around the path with the given padding

        """
        bbox = path.boundingRect()
        w, h = bbox.width(), bbox.height()

        tl = bbox.topLeft()
        x = tl.x() - padding[Padding.LEFT]
        y = tl.y() - padding[Padding.TOP]

        w += padding[Padding.LEFT] + padding[Padding.RIGHT]
        h += padding[Padding.TOP] + padding[Padding.BOTTOM]

        path.addRect(x, y, w, h)
        return path

    @property
    def state(self):
        pass

    @property
    def move_path(self):
        """ Returns the path the head moves when not cutting

        """
        # Compute the negative
        path = QPainterPath()
        for i in range(self.model.elementCount()):
            e = self.model.elementAt(i)
            if e.isMoveTo():
                path.lineTo(e.x, e.y)
            else:
                path.moveTo(e.x, e.y)
        return path

    @property
    def cut_path(self):
        """ Returns path where it is cutting

        """
        return self.model

    #     def get_offset_path(self,device):
    #         """ Returns path where it is cutting """
    #         path = QPainterPath()
    #         _p = QPointF(0,0) # previous point
    #         step = 0.1
    #         for subpath in QtSvgDoc.toSubpathList(self.model):#.toSubpathPolygons():
    #             e = subpath.elementAt(0)
    #             path.moveTo(QPointF(e.x,e.y))
    #             length = subpath.length()
    #             distance = 0
    #             while distance<=length:
    #                 t = subpath.percentAtLength(distance)
    #                 p = subpath.pointAtPercent(t)
    #                 a = subpath.angleAtPercent(t)+90
    #                 #path.moveTo(p)#QPointF(x,y))
    #                 # TOOD: Do i need numpy here???
    #                 x = p.x()+np.multiply(self.device.blade_offset,np.sin(np.deg2rad(a)))
    #                 y = p.y()+np.multiply(self.device.blade_offset,np.cos(np.deg2rad(a)))
    #                 path.lineTo(QPointF(x,y))
    #                 distance+=step
    #             #_p = p # update last
    #
    #         return path

    def add_stack(self):
        """ Add a complete stack or fill the row

        """
        stack_size = self.stack_size[0]
        if stack_size == 0:
            self.copies += 1
            return # Don't divide by 0
        copies_left = stack_size - (self.copies % stack_size)
        if copies_left == 0: # Add full stack
            self.copies += stack_size
        else: # Fill stack
            self.copies += copies_left

    def remove_stack(self):
        """ Remove a complete stack or the rest of the row

        """
        stack_size = self.stack_size[0]
        if stack_size == 0 or self.copies <= stack_size:
            self.copies = 1
            return
        copies_left = self.copies % stack_size
        if copies_left == 0: # Add full stack
            self.copies -= stack_size
        else:  # Fill stack
            self.copies -= copies_left

    def clone(self):
        """ Return a cloned instance of this object

        """
        state = self.__getstate__()
        state.update({
            'material': Material(**self.material.__getstate__()),
            'info': JobInfo(**self.info.__getstate__()),
        })
        return Job(**state)
