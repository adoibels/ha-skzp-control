"""Testy oczekujących zmian i potwierdzania wartości encji."""

from unittest import TestCase

from helpers import load_function
from skzp_control.pending_change import PendingChangeMixin


class PendingTests(TestCase):
    def test_types_and_confirmation(self):
        for target, old in ((False, True), (True, False), (0, 1), ('stop', 'auto')):
            entity = PendingChangeMixin()
            entity._init_pending_change()
            generation, event = entity._begin_pending_change(target, 1)
            self.assertEqual(generation, 1)
            self.assertFalse(entity._confirm_pending_change(old))
            self.assertTrue(entity._is_stale_update(old))
            self.assertTrue(entity._confirm_pending_change(target))
            self.assertTrue(event.is_set())
            self.assertFalse(entity._is_stale_update(target))
            self.assertEqual(entity._pending_target, target)
            event.clear()
            self.assertTrue(entity._is_stale_update(old))
            self.assertTrue(entity._confirm_pending_change(target))
            self.assertTrue(event.is_set())
            entity._clear_pending_change()
            self.assertIsNone(entity._confirmation_event)
            self.assertIsNone(entity._pending_target)

    def test_supersession(self):
        entity = PendingChangeMixin()
        entity._init_pending_change()
        first, event = entity._begin_pending_change(1, 1)
        second, current = entity._begin_pending_change(2, 1)
        self.assertGreater(second, first)
        self.assertTrue(event.is_set())
        self.assertFalse(current.is_set())
        self.assertTrue(entity._is_stale_update(1))

    def test_expiry_and_active_confirmation(self):
        entity = PendingChangeMixin()
        entity._init_pending_change()
        entity._begin_pending_change(2, -1)
        self.assertTrue(entity._is_stale_update(1))
        entity._confirmation_event = None
        self.assertFalse(entity._is_stale_update(1))
        self.assertIsNone(entity._pending_until)

    def test_number_precision(self):
        method = load_function('number.py', '_normalize_pending_value', {})
        class Number(PendingChangeMixin):
            _normalize_pending_value = method
            def _round(self, value):
                return round(value, 1)
        entity = Number()
        entity._init_pending_change()
        entity._begin_pending_change(1.2, 1)
        self.assertTrue(entity._confirm_pending_change(1.20000001))
        self.assertFalse(entity._is_stale_update(1.20000001))
