import threading
import websockets.sync.client
import spotapi.status
import spotapi.websocket

# 1. Monkey patch websockets.sync.client.connect to disable the built-in keepalive ping
# that causes ConnectionClosedError during CPU blockages / imports.
original_connect = websockets.sync.client.connect

def patched_connect(*args, **kwargs):
    # Disable built-in keepalive ping by default
    kwargs.setdefault("ping_interval", None)
    kwargs.setdefault("ping_timeout", None)
    return original_connect(*args, **kwargs)

websockets.sync.client.connect = patched_connect

# Also patch it in spotapi.websocket in case it was already imported
if hasattr(spotapi.websocket, "connect"):
    spotapi.websocket.connect = patched_connect


# 2. Add a robust reconnect method to spotapi.status.PlayerStatus.
# This prevents AttributeError: 'PlayerStatus' object has no attribute 'reconnect'
# when the websocket drops and LastPlayedManger attempts to reconnect.
def player_status_reconnect(self):
    print("[Patches] Reconnecting PlayerStatus websocket...")
    
    # Close old connection if possible
    try:
        if hasattr(self, "ws") and self.ws:
            self.ws.close()
    except Exception:
        pass
    
    # Renew session and client token
    try:
        self.base.get_session()
        self.base.get_client_token()
    except Exception as e:
        print(f"[Patches] Failed to renew session: {e}")
    
    # Establish new websocket connection using the patched connect function
    uri = f"wss://dealer.spotify.com/?access_token={self.base.access_token}"
    self.ws = websockets.sync.client.connect(
        uri,
        user_agent_header="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )
    
    # Update connection ID
    self.connection_id = self.get_init_packet()
    
    # Register and connect device
    self.register_device()
    self.connect_device()
    
    # Restart the keep_alive thread if it is dead
    if hasattr(self, "keep_alive_thread") and not self.keep_alive_thread.is_alive():
        self.keep_alive_thread = threading.Thread(target=self.keep_alive, daemon=True)
        self.keep_alive_thread.start()
        
    print("[Patches] PlayerStatus websocket reconnected successfully.")

# Inject the reconnect method into PlayerStatus class
spotapi.status.PlayerStatus.reconnect = player_status_reconnect
