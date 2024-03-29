#!/usr/bin/env python3 -I
''' core traced functionality
'''

import inspect
import itertools
import logging
import time
import weakref

_log = logging.getLogger(__name__)

class GraphException(Exception):
    ''' Base exception for this package.
    '''
    pass

class DefinitionError(GraphException):
    ''' Traceable class improperly defined.
    '''
    pass

class ContextException(GraphException):
    ''' Aabsent or invalid graph context.
    '''
    pass

class DependencyException(GraphException):
    ''' Something went wrong in dependency chain evaluation.
    '''
    pass

class LoopException(DependencyException):
    ''' Loop in dependency chain.
    '''
    pass

class Graph(object):
    active_stack = [] # NB: static usage!
    parent = None
    evaluation_stack = None
    vertices = None # vertex map
    indent = ''

    def __init__(self):
        self.evaluation_stack = []
        self.vertices = {}
        _log.debug('__init__: %s', self)

    def __enter__(self):
        if Graph.active_stack:
            cg = Graph.active_stack[-1]
            assert self.parent is None or cg is self.parent, 'Reparenting of graphs not yet supported'
            self.parent = cg

        Graph.active_stack.append(self)
        _log.info('activated %s, parent %s', self, self.parent)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        assert Graph.active_stack, 'Internal error, no active graph'
        cg = Graph.active_stack.pop()
        assert cg is self, 'Internal error, active graph mismatch'
        _log.info('deactivated %s', self)

    def push_vertex(self, vertex):
        assert vertex, 'Internal error, no vertex'
        if self.evaluation_stack:
            if vertex not in self.evaluation_stack:
                self.evaluation_stack[-1].add_dependency(vertex.key())
            else:
                vertex.undefine()
                raise LoopException(
                    'Loop of length {} detected, attribute {} calls itself',
                    self.evaluation_stack[::-1].index(vertex),
                    vertex,
                )

        self.evaluation_stack.append(vertex)
        self.indent += '  '
        _log.debug('%sstart eval %s', self.indent, vertex)

    def pop_vertex(self, vertex):
        assert vertex, 'Internal error, no vertex'
        assert self.evaluation_stack, 'Internal error, evaluation stack empty'
        v = self.evaluation_stack.pop()
        assert v is vertex, 'Internal error, top vertex mismatch'
        self.indent = self.indent[:-2]
        _log.debug('%sfinish eval %s', self.indent, vertex)

    def traceable_vertex(self, instance, cell, mode, key = None):
        ''' This retrieves a vertex for reading or writing. Read-only vertex
            is the first one on the current graph or up its chain of
            parents. Writeable vertex must be created on the current
            graph if it doesn't exist. This follows Python semantics
            of member access in a class: read will access a class
            attribute if no instance-level one is found, but
            modification will create a new instance attribute if
            absent.

            mode: delete/get/set/trace
        '''
        mode = mode[0]
        assert mode in 'dgst', 'invalid mode ' + mode
        if key is None:
            key = TraceableVertex.graph_key(instance, cell)

        vertex = self.vertices.get(key)
        if vertex is None:
            if 's' == mode:
                # override always creates a new vertex on graph
                self.vertices[key] = vertex = TraceableVertex(instance, cell)
            else:
                # for the other modes (d/g/t) we need to search up
                ancestor = self.parent
                while ancestor is not None and vertex is None:
                    vertex = ancestor.vertices.get(key)
                    ancestor = ancestor.parent

                # new vertex is created if it's computed for the first
                # time or if an override on a higher graph is removed
                if (vertex is None and 'g' == mode) or (vertex is not None and vertex.is_override() and 'd' == mode):
                    self.vertices[key] = vertex = TraceableVertex(instance, cell)

        return vertex

    def remove_override(self, traceable, cell):
        assert isinstance(traceable, Traceable)
        vertex = self.traceable_vertex(traceable, cell, 'd')
        if vertex:
            vertex.remove_override()

    def read(self, traceable, cell):
        assert isinstance(traceable, Traceable)
        return self.traceable_vertex(traceable, cell, 'g')

    def override(self, traceable, cell, value):
        ''' Check that target `cell` is not being overridden inside another
            cell's evaluation function, find/create the vertex and
            override it.
        '''
        assert isinstance(traceable, Traceable)
        if not self.evaluation_stack:
            self.traceable_vertex(traceable, cell, 's').override(value)
        else:
            # currently evaluated cell definitely has a name because it can call other cells (like the target one)
            raise DependencyException('Cell {} cannot override another Cell {}'.format(
                self.evaluation_stack[-1].cell.name(),
                cell.name() or '<anonymous>'
            ))

    def vertex_stale(self, vertex):
        ''' `True` if at least one dependency of specified vertex is newer, `False` otherwise.
        '''
        # TODO this shows TraceableVertex is not an independent entity
        if vertex.dependency_keys:
            for key in vertex.dependency_keys:
                dependency = self.traceable_vertex(None, None, 't', key)
                if dependency is None or dependency.is_newer(vertex):
                    return True

        return False

    @classmethod
    def current(clazz):
        if not clazz.active_stack:
            raise ContextException('Must be computed in the context of a graph, please activate one first')

        return clazz.active_stack[-1]
            
class NotifierMixin(object):
    callbacks = None

    def subscribe(self, callback):
        ''' Register callback.
        '''
        # TODO weakref
        if self.callbacks is None:
            self.callbacks = weakref.WeakSet()

        self.callbacks.add(callback)

    def unsubscribe(self, callback):
        ''' Unregister callback. Does nothing if there was no prior subscription.
        '''
        if self.callbacks:
            self.callbacks.discard(callback)

    @classmethod
    def notify_all(clazz, notifiers, *arg, **kw):
        callbacks = set(itertools.chain(*(n.callbacks for n in notifiers if n.callbacks)))
        if callbacks:
            _log.debug('notifying %d callback(s)', len(callbacks))
            for cb in callbacks:
                cb(*arg, **kw)

class MetaTraceable(type):
    def __new__(cls, name, bases, memberdict):
        if '__init__' in memberdict and name != 'Traceable':
            raise DefinitionError('__init__  not allowed in Traceable subclass ' + name)

        return type.__new__(cls, name, bases, memberdict)

class Traceable(NotifierMixin, metaclass = MetaTraceable):
    parent_graph = None

    def __init__(self, **kw):
        self.parent_graph = Graph.current()
        missing = None
        for k, v in kw.items():
            cell = getattr(type(self), k, None)
            if isinstance(cell, Cell):
                setattr(self, k, v)
            else:
                if missing is None:
                    missing = []

                missing.append(k)

        if missing:
            raise DefinitionError('Attribute(s) {} not found'.format(', '.join(sorted(missing))))

class TraceableWrapper(object):
    ''' Base class for dependency tracking on callable/generator values.
    '''
    graph = None
    vertex = None
    valid = True

    def __init__(self, graph, vertex):
        self.graph = graph
        self.vertex = vertex

    def wrap(self, f, *args, **kwargs):
        try:
            if self.valid:
                self.graph.push_vertex(self.vertex)

            return f(*args, **kwargs)
        finally:
            if self.valid:
                self.graph.pop_vertex(self.vertex)

class TraceableClosure(TraceableWrapper):
    ''' Function/closure wrapper that records dependencies.
    '''
    func = None

    def __init__(self, graph, vertex, func):
        super().__init__(graph, vertex)
        self.func = func
        _log.debug('__init__: closure %s, %s', graph, vertex)

    def __call__(self, *args, **kwargs):
        return self.wrap(self.func, *args, **kwargs)

class TraceableGenerator(TraceableWrapper):
    ''' Generator wrapper that records dependencies.
    '''
    gen = None

    def __init__(self, graph, vertex, gen):
        super().__init__(graph, vertex)
        self.gen = gen
        _log.debug('__init__: generator %s, %s', graph, vertex)
    
    def __iter__(self):
        return self

    def __next__(self):
        return self.wrap(self.gen.__next__)

    def send(self, value):
        return self.wrap(self.gen.send, value)

    def throw(self, *args):
        # TODO working example?
        return self.wrap(self.gen.throw, *args)

    def close(self):
        self.valid = False
        self.gen.close()

class TraceableVertex(NotifierMixin):
    ''' A descriptor accessed via traceable node attribute.

        :member:`touched` is the time of last change. Guaranteed to be
        the same or later than either `evaluated` or
        `overridden`. Required to (a) force-update downstream when
        override is removed or (b) reflect addition of dynamic
        dependencies for generators or closures.
    '''
    traceable = None # the instance
    cell = None # a specific attribute on the instance

    dependency_keys = None
    evaluated = None # time of last calc
    last_known = None # last known evaluation result TODO optionally make it weak-referenced
    overridden = None # time of override or None if not overridden

    touched = None

    value = None # current value; None is a valid value so never analyze contents for any tracing logic

    def __init__(self, traceable, cell):
        self.cell = cell
        self.traceable = traceable
        _log.debug('__init__: %s', self)

    def __call__(self):
        ''' Call on the traceable node ends up here.
        '''
        assert self.traceable is not None
        cg = Graph.current()
        cg.push_vertex(self)
        try:
            # overridden or no changes
            if not self.is_dirty():
                return self.value

            self.touched = time.monotonic()
            self.dependency_keys = None
            # if evaluation function is not actually a function use it as "default value"
            self.last_known = self.cell.evaluate(self.traceable) if callable(self.cell.evaluate) else self.cell.evaluate

            # generator must be wrapped to populate dependencies as its __next__ or other methods are called
            if inspect.isgenerator(self.last_known):
                # assume previous value, if present, was a generator too
                if self.value is not None: self.value.close()
                self.last_known = TraceableGenerator(cg, self, self.last_known)
            elif callable(self.last_known):
                self.last_known = TraceableClosure(cg, self, self.last_known)

            # the code below would not execute on exception during evaluation
            self.evaluated = self.touched
            self.__assign(self.last_known)
            _log.debug('%seval %s, dep(s): %d', cg.indent, self, len(self.dependency_keys) if self.dependency_keys else 0)
            return self.value
        finally:
            # TODO should _log.debug be here and report sys.exc_info()?
            #import sys
            #if sys.exc_info()[1]:
            #    _log.exception('__call__')

            cg.pop_vertex(self)

    def __str__(self):
        return 'vertex {}.{} {}{} = {}'.format(
            self.traceable,
            self.cell.name() or '<anonymous>',
            'over:' if self.overridden is not None else 'eval:' if self.evaluated is not None else '',
            self.defined(),
            self.value,
        )

    def __assign(self, new_value):
        ''' Do not call this directly!

            Assigns the new value and if it's different from the old,
            notifies all subscribers. The new value is assigned prior
            to notification being sent so that if subscriber reads
            this vertex it's already up to date.
        '''
        old_value = self.value
        self.value = new_value
        if new_value != old_value:
            NotifierMixin.notify_all(
                (self, self.traceable, self.cell),
                self.traceable,
                self.cell.name(),
                new_value,
                old_value,
            )

    def override(self, value):
        # not checking for validity here, see Cell.__set__
        self.touched = self.overridden = time.monotonic()
        self.__assign(value)

    def remove_override(self):
        if self.overridden is not None:
            self.touched = time.monotonic()
            self.overridden = None
            # don't issue a notification; "last known" may not be valid if dependencies have changed
            self.value = self.last_known

    def is_override(self):
        ''' `True` if vertex is overridden, otherwise `False`. API for outside consumption.
        '''
        return self.overridden is not None

    def undefine(self):
        ''' Wipe out in case of an error. We only need to zero out `evaluated`
            and `overridden` to reset but undefine others to eliminate
            references and allow GC to work its magic.

            Does not generate a notification.
        '''
        self.touched = time.monotonic()
        self.dependency_keys = self.evaluated = self.last_known = self.overridden = self.value = None

    def defined(self):
        ''' Time when the current value was set or `None`.
        '''
        return self.overridden or self.evaluated

    def add_dependency(self, vertex_key):
        assert vertex_key is not None, 'Internal error, no vertex'
        if self.dependency_keys is None:
            self.dependency_keys = set()
        elif vertex_key in self.dependency_keys:
            return

        self.touched = time.monotonic()
        self.dependency_keys.add(vertex_key)

    def is_newer(self, vertex):
        ''' Is this vertex "newer" than the other one.
            The specified `vertex` depends on this one (`self`).
        '''
        return self.is_dirty() or vertex.defined() < self.touched

    def is_dirty(self):
        ''' Returns `True` if the vertex must be evaluated. Overridden vertex
            is clean by definition because it's defined and has no
            dependencies. A vertex that is not overridden is clean if
            it's been evaluated and there are no "dirty" dependencies.
        '''
        result = self.overridden is None and (self.evaluated is None or Graph.current().vertex_stale(self))
        _log.debug('%s%s %s', Graph.current().indent, 'dirty' if result else 'clean', self)
        return result

    def key(self):
        '''Identifies vertex on graph. Must match `graph_key`.
        '''
        return hash(self.traceable), hash(self.cell)

    @classmethod
    def graph_key(clazz, traceable, cell):
        '''Derives vertex key from traceable object and a cell. Must match `key`.
        '''
        return hash(traceable), hash(cell)

class Cell(NotifierMixin):
    ''' This is a descriptor for traceable object's cells. Value of a
        `Cell` is calculated lazily.  The descriptor changes standard
        _getter_ semantics of invocation to reinforce that the
        underlying property is evaluated, by requiring that the
        property be called. In the following definition:

    :code:
        class MyClass(Traceable):
            @Cell
            def MyProperty(self):
                return 0

        my = MyClass()

    The following applies:

    * `expr = my.MyProperty` -- provides access to the descriptor
      itself, `__get__` is called behind the scenes

    * `my.MyProperty = expr` -- overrides/sets current value by
      calling `__set__`

    * `del my.MyProperty` -- clears an overridden value, allowing for
      recalculation; does nothing without an override

    * `expr = my.MyProperty()` -- returns calculated value or an
      override if one is set by calling `__get__` then `__call__`

    * my.MyProperty.subscribe/unsubscribe -- notifies about changing
      value, for GUI etc.

    '''

    evaluate = None

    def __init__(self, evaluate):
        if isinstance(evaluate, Cell):
            raise DefinitionError('Cell cannot decorate another Cell')

        self.evaluate = evaluate

    def __delete__(self, instance):
        Graph.current().remove_override(instance, self)

    def __get__(self, instance, typ = None):
        assert typ is None or issubclass(typ, Traceable), 'Invalid type {}'.format(typ)
        # without an instance, return something equivalent to unbound method
        return self if instance is None else Graph.current().read(instance, self)

    def __set__(self, instance, value):
        Graph.current().override(instance, self, value)

    def name(self):
        return getattr(self.evaluate, '__name__', None)

if '__main__' == __name__:
    logging.basicConfig(level = logging.INFO)
