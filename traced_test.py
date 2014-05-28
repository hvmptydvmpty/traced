#!/usr/bin/env python3 -I
import logging
import unittest

import traced

__doc__ = '''
core traced unit tests
'''

class SingleInstanceDependency(traced.Traceable):
    @traced.Cell
    def Input(self):
        return 1

    @traced.Cell
    def Output(self):
        return self.Input() + 1

class MultipleInstanceDependency(traced.Traceable):
    @traced.Cell
    def Another(self):
        return SingleInstanceDependency()

    @traced.Cell
    def Mul2(self):
        return self.Another().Output() * 2

class Diamond(traced.Traceable):
    count_x = 0

    @traced.Cell
    def X(self):
        self.count_x += 1
        return 6

    @traced.Cell
    def Y1(self):
        return self.X() * 2

    @traced.Cell
    def Y2(self):
        return self.X() // 2

    @traced.Cell
    def Z(self):
        return self.Y1() + self.Y2()

class AssignmentPlus(traced.Traceable):
    AbcByDefault = traced.Cell('abc')

    @traced.Cell
    def Closure(self):
        m = {ch: index for index, ch in enumerate(self.AbcByDefault())}
        return lambda key: m.get(key, None)

    @traced.Cell
    def Generator(self):
        index = 0
        while True:
            yield self.AbcByDefault()[index % len(self.AbcByDefault())]
            index += 1

class SingleGraphTest(unittest.TestCase):
    def test_single_class(self):
        with traced.Graph():
            tr = SingleInstanceDependency()
            self.assertEqual(2, tr.Output())

            tr.Input = -1
            self.assertEqual(0, tr.Output())

            del tr.Input
            self.assertEqual(2, tr.Output())

    def test_two_classes(self):
        with traced.Graph():
            tr1 = MultipleInstanceDependency()
            self.assertEqual(4, tr1.Mul2())

            tr1.Another().Input = -1
            # don't evaluate
            #self.assertEqual(2, tr.Mul2())

            tr2 = SingleInstanceDependency(Input = 7)
            tr1.Another = tr2
            self.assertEqual(16, tr1.Mul2())

            del tr1.Another
            # "original" Another's Input was overridden to -1
            self.assertEqual(0, tr1.Mul2())

    def test_diamond(self):
        with traced.Graph():
            diamond = Diamond()
            self.assertEqual(15, diamond.Z())
            self.assertEqual(1, diamond.count_x)

            diamond.X = 16
            self.assertEqual(40, diamond.Z())
            self.assertEqual(1, diamond.count_x, 'X was overridden so no more calls were expected')

    def test_init(self):
        with traced.Graph():
            tr = SingleInstanceDependency(Input = 5)
            self.assertEqual(6, tr.Output())

            del tr.Input
            self.assertEqual(2, tr.Output())

    def test_assignment_and_closure(self):
        with traced.Graph():
            tr = AssignmentPlus()
            self.assertEqual('abc', tr.AbcByDefault())
            self.assertEqual(1, tr.Closure()('b'))
            self.assertEqual(None, tr.Closure()('d'))

            tr.AbcByDefault = 'something'
            self.assertEqual(2, tr.Closure()('m'))
            self.assertEqual(5, tr.Closure()('h'))

    @unittest.skip('TODO')
    def test_generator(self):
        with traced.Graph():
            tr = AssignmentPlus()
            for index, ch in enumerate(tr.Generator()):
                self.assertEqual('abc'[index], ch)
                if index == 2:
                    break

            tr.AbcByDefault = 'wxyz'
            self.assertEqual('w', next(tr.Generator()))

class Loop(traced.Traceable):
    @traced.Cell
    def First(self):
        return self.Third() * 3

    @traced.Cell
    def Second(self):
        return self.First() - 5

    @traced.Cell
    def Third(self):
        return self.Second() + 10

class RogueError(Exception):
    ''' Custom exception suitable for assertRaises to catch.
    '''
    pass

class Rogue(traced.Traceable):
    SomeValue = traced.Cell('qwerty')

    @traced.Cell
    def AnotherValue(self):
        self.SomeValue = 'asdf'
        return 3

    @traced.Cell
    def Riser(self):
        if 'qwerty' == self.SomeValue():
            raise RogueError('Bad input')

        return self.SomeValue().upper()

    @traced.Cell
    def ReverseRiser(self):
        return self.Riser()[::-1]

class FailureTest(unittest.TestCase):
    def test_contextless(self):
        with self.assertRaises(traced.ContextException):
            SingleInstanceDependency()

        with traced.Graph():
            tr = SingleInstanceDependency()
            self.assertEqual(2, tr.Output())

        with self.assertRaises(traced.ContextException):
            tr.Output()

    def test_double_cell(self):
        with self.assertRaises(traced.DefinitionError):
            class DoubleCell(traced.Traceable):
                @traced.Cell
                @traced.Cell
                def Calc(self):
                    pass

    def test_eval_exception(self):
        with traced.Graph():
            rogue = Rogue(SomeValue = 'xyz')
            self.assertEqual('XYZ', rogue.Riser())

            del rogue.SomeValue
            # should fail no matter how many times we call it
            for i in range(2):
                with self.assertRaises(RogueError):
                    rogue.Riser()

                with self.assertRaises(RogueError):
                    rogue.ReverseRiser()

            rogue.SomeValue = 'asdf'
            self.assertEqual('FDSA', rogue.ReverseRiser())

    def test_forbidden_init(self):
        with self.assertRaisesRegex(traced.DefinitionError, '__init__ .* WithInit'):
            class WithInit(traced.Traceable):
                def __init__(self):
                    pass

    def test_init_bad_attribute(self):
        with traced.Graph():
            with self.assertRaisesRegex(traced.DefinitionError, 'Y3, Z1'):
                Diamond(Y3 = 10, X = 30, Z1 = 50)

    def test_loop(self):
        with traced.Graph():
            with self.assertRaises(traced.LoopException):
                Loop().First()

        with traced.Graph():
            with self.assertRaises(traced.LoopException):
                Loop().Third()

        # break the loop
        with traced.Graph():
            loop3 = Loop(First = 17)
            self.assertEqual(22, loop3.Third())

            del loop3.First
            loop3.Third = 10
            self.assertEqual(25, loop3.Second())

    def test_override_in_eval(self):
        with self.assertRaisesRegex(traced.DependencyException, 'AnotherValue.*override.*<anonymous>'):
            with traced.Graph():
                Rogue().AnotherValue()

class NotificationSink(object):
    count = 0

    def __call__(self, instance, attr, new, old):
        self.count += 1

class SubscriptionTest(unittest.TestCase):
    def test_single_vertex_sub_unsub(self):
        with traced.Graph():
            tr = SingleInstanceDependency()

            cb_count = 0
            def on_change(instance, attr, new, old):
                nonlocal cb_count
                cb_count += 1

            tr.Output.subscribe(on_change)
            self.assertEqual(2, tr.Output())
            self.assertEqual(1, cb_count)

            tr.Input = 7
            self.assertEqual(8, tr.Output())
            self.assertEqual(2, cb_count)

            # override without an actual change, no notification
            self.Output = 8
            self.assertEqual(2, cb_count)

            # override with a different value, will notify
            tr.Output = 15
            self.assertEqual(3, cb_count)

            # un-override doesn't result in a notification...
            del tr.Input
            del tr.Output
            self.assertEqual(3, cb_count)

            # ...but subsequent calc does
            self.assertEqual(2, tr.Output())
            self.assertEqual(4, cb_count)

            tr.Output.unsubscribe(on_change)
            tr.Output = -3
            self.assertEqual(4, cb_count)

    def test_subscribe_variety(self):
        with traced.Graph():
            tr1, tr2 = SingleInstanceDependency(), SingleInstanceDependency()

            tr1_sink, tr2_sink, cell_sink, tr2_vertex_sink = [NotificationSink() for i in range(4)]
            tr1.subscribe(tr1_sink)
            tr2.subscribe(tr2_sink)
            tr2.Output.subscribe(tr2_vertex_sink)
            SingleInstanceDependency.Output.subscribe(cell_sink)

            # override then eval downstream

            tr1.Input = 4
            self.assertEqual((1, 0), (tr1_sink.count, cell_sink.count))

            self.assertEqual(5, tr1.Output())
            self.assertEqual((2, 1), (tr1_sink.count, cell_sink.count))

            # induce evaluation and change from None to something

            self.assertEqual(2, tr2.Output())
            self.assertEqual((2, 1, 2), (tr2_sink.count, tr2_vertex_sink.count, cell_sink.count))

    def test_same_cb_one_call_per_vertex(self):
        with traced.Graph():
            tr = SingleInstanceDependency()

            sink = NotificationSink()
            SingleInstanceDependency.Input.subscribe(sink)
            tr.subscribe(sink)
            tr.Input.subscribe(sink)

            self.assertEqual(1, tr.Input())
            self.assertEqual(1, sink.count)

    def test_assignment_subscription(self):
        with traced.Graph():
            tr = AssignmentPlus()
            
            called = False
            def on_change(instance, name, new, old):
                nonlocal called
                called = True
                self.assertIs(tr, instance)
                self.assertIsNone(name)
                self.assertEqual('abc', new)
                self.assertIsNone(old)

            tr.subscribe(on_change)
            self.assertEqual('abc', tr.AbcByDefault())
            self.assertTrue(called)

class MultiGraphTest(unittest.TestCase):
    def test_subgraph(self):
        with traced.Graph() as g1:
            diamond = Diamond(X = 20)

            with traced.Graph() as g2:
                diamond.X = -8
                self.assertEqual(-20, diamond.Z())

            self.assertEqual(50, diamond.Z())

    def test_passthrough(self):
        with traced.Graph() as g1:
            tr = SingleInstanceDependency(Input = 100)

            with traced.Graph() as g2:
                self.assertEqual(101, tr.Output())

    def test_override(self):
        with traced.Graph() as g1:
            tr = SingleInstanceDependency(Input = 4)
            self.assertEqual(5, tr.Output())

            with traced.Graph() as g2:
                del tr.Input
                self.assertEqual(2, tr.Output())

if '__main__' == __name__:
    logging.basicConfig(level = logging.DEBUG)
    unittest.main()
