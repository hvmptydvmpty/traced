#!/usr/bin/env python3 -I
import itertools
import logging
import time

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
    active_stack = [] # static usage
    parent = None
    evaluation_stack = None

    def __init__(self):
        self.evaluation_stack = []
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

    @classmethod
    def current(clazz):
        if not clazz.active_stack:
            raise ContextException('Must be computed in the context of a graph, please activate one first')

        return clazz.active_stack[-1]

    @classmethod
    def push_vertex(clazz, vertex):
        assert vertex, 'Internal error, no vertex'
        cg = clazz.current()
        if cg.evaluation_stack:
            if vertex in cg.evaluation_stack:
                vertex.undefine()
                raise LoopException(
                    'Loop of length {} detected, attribute {} calls itself',
                    cg.evaluation_stack[::-1].index(vertex),
                    vertex,
                )

            cg.evaluation_stack[-1].add_dependency(vertex)

        cg.evaluation_stack.append(vertex)
        _log.debug('start eval %s', vertex)

    @classmethod
    def pop_vertex(clazz, vertex):
        assert vertex, 'Internal error, no vertex'
        cg = clazz.current()
        assert cg.evaluation_stack, 'Internal error, evaluation stack empty'
        v = cg.evaluation_stack.pop()
        assert v is vertex, 'Internal error, top vertex mismatch'
        _log.debug('finish eval %s', vertex)

class NotifierMixin(object):
    callbacks = None

    def subscribe(self, callback):
        ''' Register callback.
        '''
        # TODO weakref
        if self.callbacks is None:
            self.callbacks = set()

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

class TraceableVertex(NotifierMixin):
    traceable = None # the instance
    cell = None # a specific attribute on the instance

    dependencies = None
    evaluated = None # time of last calc
    last_known = None # last known evaluation result
    overridden = None # time of override or None if not overridden
    touched = None # time of last change, required to force-update downstream when override is removed
    value = None # current value; None is a valid value so never analyze contents for any tracing logic

    def __init__(self, cell, traceable):
        self.cell = cell
        self.traceable = traceable
        _log.debug('__init__: %s', self)

    def __call__(self):
        assert self.traceable is not None
        Graph.current().push_vertex(self)
        try:
            # overridden or no changes
            if not self.is_dirty():
                return self.value

            self.touched = self.evaluated = time.monotonic()
            self.dependencies = None
            # if evaluation function is not actually a function use it as "default value"
            self.last_known = self.cell.evaluate(self.traceable) if callable(self.cell.evaluate) else self.cell.evaluate
            if self.value != self.last_known:
                NotifierMixin.notify_all([self, self.traceable, self.cell], self.traceable, self.last_known, self.value)

            self.value = self.last_known
            _log.debug('eval %s, dep(s): %d', self, len(self.dependencies) if self.dependencies else 0)
            return self.value
        finally:
            Graph.current().pop_vertex(self)

    def __str__(self):
        return 'vertex {}.{} {}{} = {}'.format(
            self.traceable,
            self.cell.evaluate.__name__ if callable(self.cell.evaluate) else '<anonymous>',
            'over:' if self.overridden is not None else 'eval:' if self.evaluated is not None else '',
            self.defined(),
            self.value,
        )

    def override(self, value):
        self.touched = self.overridden = time.monotonic()
        self.value = value

    def remove_override(self):
        if self.overridden is not None:
            self.touched = time.monotonic()
            self.overridden = None
            self.value = self.last_known

    def undefine(self):
        ''' Wipe out in case of an error. We only need to zero out `evaluated`
            and `overridden` to reset but undefine others to eliminate
            references and allow GC to work its magic.
        '''
        self.touched = time.monotonic()
        self.dependencies = self.evaluated = self.last_known = self.overridden = self.value = None

    def defined(self):
        ''' Time when the current value was set or `None`.
        '''
        return self.overridden or self.evaluated

    def add_dependency(self, vertex):
        assert vertex is not None, 'Internal error, no vertex'
        if self.dependencies is None:
            self.dependencies = []

        self.dependencies.append(vertex)

    def is_newer(self, vertex):
        ''' Is this vertex "newer" than the other one.
            The specified `vertex` depends on this one (`self`).
        '''
        return self.is_dirty() or vertex.defined() < self.touched

    def dirty_dependencies(self):
        ''' Generator of "dirty" dependency vertices. Makes it easy for
            `clean` to return as soon as at least one "dirty"
            dependency is found.
        '''
        return (vertex for vertex in self.dependencies or [] if vertex.is_newer(self))

    def is_dirty(self):
        ''' Returns `True` if the vertex must be evaluated. Overridden vertex
            is clean by definition because it's defined and has no
            dependencies. A vertex that is not overridden is clean if
            it's been evaluated and there are no "dirty" dependencies.
        '''
        result = self.overridden is None and (self.evaluated is None or any(self.dirty_dependencies()))
        _log.debug('%s %s', 'dirty' if result else 'clean', self)
        return result

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
    properties = None

    def __init__(self, evaluate):
        if isinstance(evaluate, Cell):
            raise DefinitionError('Cell cannot decorate another Cell')
        self.evaluate = evaluate

    def __delete__(self, instance):
        assert isinstance(instance, Traceable)
        vertex = self.traceable_vertex(instance, False)
        if vertex:
            vertex.remove_override()

    def __get__(self, instance, typ = None):
        assert typ is None or issubclass(typ, Traceable), 'Invalid type {}'.format(typ)
        # without an instance, return something equivalent to unbound method
        return self if instance is None else self.traceable_vertex(instance)

    def __set__(self, instance, value):
        self.traceable_vertex(instance).override(value)

    def traceable_vertex(self, instance, create = True):
        assert isinstance(instance, Traceable)
        h = hash(instance)
        vertex = self.properties.get(h, None) if self.properties else None
        if create and vertex is None:
            if self.properties is None:
                self.properties = {}

            self.properties[h] = vertex = TraceableVertex(self, instance)

        return vertex

if '__main__' == __name__:
    logging.basicConfig(level = logging.INFO)
