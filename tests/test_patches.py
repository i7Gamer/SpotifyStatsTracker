import unittest
from unittest.mock import MagicMock, patch
import threading
import websockets.sync.client
import spotapi.status
import spotapi.websocket


class TestPatches(unittest.TestCase):
    """Verify that monkey-patches are correctly applied to websockets and spotapi."""

    def test_websockets_connect_default_arguments(self):
        """websockets.sync.client.connect should default ping_interval/ping_timeout to None."""
        mock_connect = MagicMock()
        # Temporarily swap the original connect with our mock
        from Database.patches import original_connect
        try:
            with patch("Database.patches.original_connect", mock_connect):
                # When calling websockets.sync.client.connect with some arguments
                websockets.sync.client.connect("wss://example.com", user_agent_header="test-ua")
                
                # Check that original_connect was called with defaults overridden to None
                mock_connect.assert_called_once_with(
                    "wss://example.com",
                    user_agent_header="test-ua",
                    ping_interval=None,
                    ping_timeout=None
                )
        finally:
            pass

    def test_player_status_has_reconnect_method(self):
        """PlayerStatus class must have reconnect method injected."""
        self.assertTrue(hasattr(spotapi.status.PlayerStatus, "reconnect"))
        self.assertTrue(callable(spotapi.status.PlayerStatus.reconnect))

    @patch("websockets.sync.client.connect")
    def test_player_status_reconnect_flow(self, mock_ws_connect):
        """reconnect() must call close on old socket, renew sessions, connect, get init packet, and register."""
        # Create a mock PlayerStatus instance
        self.assertTrue(hasattr(spotapi.status.PlayerStatus, "reconnect"))
        
        # We will mock the required methods/attributes on PlayerStatus
        mock_ws = MagicMock()
        mock_ws_connect.return_value = mock_ws
        
        instance = MagicMock(spec=spotapi.status.PlayerStatus)
        instance.ws = mock_ws
        instance.base = MagicMock()
        
        # When get_init_packet is called, it returns a new connection ID
        instance.get_init_packet.return_value = "new-conn-id"
        
        # Thread status mock
        mock_thread = MagicMock(spec=threading.Thread)
        mock_thread.is_alive.return_value = False
        instance.keep_alive_thread = mock_thread
        
        # Call the reconnect function bound to the instance
        spotapi.status.PlayerStatus.reconnect(instance)
        
        # Verify old websocket is closed
        mock_ws.close.assert_called_once()
        
        # Verify sessions and tokens are renewed
        instance.base.get_session.assert_called_once()
        instance.base.get_client_token.assert_called_once()
        
        # Verify we connect to the new websocket URI
        mock_ws_connect.assert_called_once()
        
        # Verify connection_id was updated
        self.assertEqual(instance.connection_id, "new-conn-id")
        
        # Verify device registration and connection
        instance.register_device.assert_called_once()
        instance.connect_device.assert_called_once()
        
        # Verify keep alive thread was restarted
        mock_thread.is_alive.assert_called_once()


if __name__ == "__main__":
    unittest.main()
