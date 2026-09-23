import unittest
from unittest import mock

from app.runtime_resources import RuntimeResources


class RuntimeResourcesTests(unittest.TestCase):
    def test_clients_close_once_before_server_is_stopped(self):
        events = []
        storage = mock.Mock()
        translator = mock.Mock()
        server = mock.Mock()
        storage.close.side_effect = lambda: events.append("storage")
        translator.close.side_effect = lambda: events.append("http")
        server.stop.side_effect = lambda: events.append("llama")
        resources = RuntimeResources(storage, server, translator, mock.Mock())

        resources.close_clients()
        resources.close_clients()
        resources.stop_server()

        self.assertEqual(events, ["http", "storage", "llama"])

    def test_close_retries_storage_without_closing_translator_twice(self):
        storage = mock.Mock()
        storage.close.side_effect = [False, True]
        translator = mock.Mock()
        resources = RuntimeResources(storage, mock.Mock(), translator, mock.Mock())

        self.assertFalse(resources.close_clients())
        self.assertTrue(resources.close_clients())
        self.assertTrue(resources.close_clients())
        translator.close.assert_called_once_with()
        self.assertEqual(storage.close.call_count, 2)

    def test_emergency_interrupt_only_stops_the_owned_server(self):
        storage = mock.Mock()
        translator = mock.Mock()
        server = mock.Mock()
        resources = RuntimeResources(storage, server, translator, mock.Mock())

        resources.interrupt_server()

        server.stop.assert_called_once_with()
        translator.close.assert_not_called()
        storage.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
