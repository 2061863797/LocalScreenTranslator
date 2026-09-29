import unittest
from unittest import mock

from app.runtime_resources import RuntimeResources


class RuntimeResourcesTests(unittest.TestCase):
    def test_clients_close_once_in_dependency_order(self):
        events = []
        storage = mock.Mock()
        translator = mock.Mock()
        ocr = mock.Mock()
        translator.close.side_effect = lambda: events.append("translation")
        ocr.close.side_effect = lambda: events.append("ocr")
        storage.close.side_effect = lambda: events.append("storage")
        resources = RuntimeResources(storage, translator, ocr)

        self.assertTrue(resources.close_clients())
        self.assertTrue(resources.close_clients())
        self.assertEqual(events, ["translation", "ocr", "storage"])

    def test_storage_close_can_retry_without_releasing_model_twice(self):
        storage = mock.Mock()
        storage.close.side_effect = [False, True]
        translator = mock.Mock()
        ocr = mock.Mock()
        resources = RuntimeResources(storage, translator, ocr)

        self.assertFalse(resources.close_clients())
        self.assertTrue(resources.close_clients())
        translator.close.assert_called_once_with()
        self.assertEqual(storage.close.call_count, 2)

    def test_interrupt_only_cancels_translation(self):
        storage = mock.Mock()
        translator = mock.Mock()
        ocr = mock.Mock()
        resources = RuntimeResources(storage, translator, ocr)

        resources.interrupt_translation()

        translator.abort_inflight.assert_called_once_with()
        translator.close.assert_not_called()
        ocr.close.assert_not_called()
        storage.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
