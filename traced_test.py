#!/usr/bin/env python3 -I
import logging
import unittest

import traced

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

    @unittest.skip
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
        pass

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
        pass

class SubscriptionTest(unittest.TestCase):
    pass

class MultiGraphTest(unittest.TestCase):
    pass

if '__main__' == __name__:
    logging.basicConfig(level = logging.DEBUG)
