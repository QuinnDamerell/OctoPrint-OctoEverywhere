import ssl
import time
import threading
import certifi

import websocket
from websocket import WebSocketApp

try:
    import thread
except ImportError:
    import _thread as thread

#
# This class gives a bit of an abstraction over the normal ws
#

class Client:

    def __init__(self, url, onWsOpen = None, onWsMsg = None, onWsData = None, onWsClose = None, onWsError = None, headers = None):

        def OnOpen(ws):
            if onWsOpen:
                onWsOpen(self)

        def OnMsg(ws, msg):
            if onWsMsg:
                onWsMsg(self, msg)

        def onClosed(ws):
            if onWsClose:
                onWsClose(self)

        def OnError(ws, msg):
            if onWsError:
                onWsError(self, msg)

        def OnData(ws, buffer, type, continueFlag):
            if onWsData:
                onWsData(self, buffer, type)

        self.Ws = WebSocketApp(url,        
                                  on_message = OnMsg,
                                  on_close = onClosed,
                                  on_error = OnError,
                                  on_data = OnData,
                                  header = headers
        )
        self.Ws.on_open = OnOpen

    def RunUntilClosed(self):
        # Note we must set the ping_interval and ping_timeout or we won't get a multithread
        # safe socket... python. >.>
        # The client is responsible for sending keep alive pings the server will then pong respond to.
        # If that's not done, the connection will timeout.        
        # We will send a ping every 10 minutes, and expected a pong back within 5 mintues.
        #
        # Important note! This websocket lib won't use certify which a Root CA store that mirrors what firefox uses.
        # Since let's encrypt updated their CA root, we need to use certify's root or the connection will likely fail.
        # The requests lib already does this, so we only need to worry about it for websockets.
        self.Ws.run_forever(skip_utf8_validation=True, ping_interval=600, ping_timeout=300, sslopt={"ca_certs":certifi.where()})

    def RunAsync(self):
        t = threading.Thread(target=self.RunUntilClosed, args=())
        t.daemon = True
        t.start()

    def Close(self):
        if self.Ws:
            self.Ws.close()
            self.Ws = None
    
    def Send(self, msgBytes, isData):
        if isData:
            self.SendWithOptCode(msgBytes, websocket.ABNF.OPCODE_BINARY)
        else:
            self.SendWithOptCode(msgBytes, websocket.ABNF.OPCODE_TEXT)

    def SendWithOptCode(self, msgBytes, opcode):
        if self.Ws:
            self.Ws.send(msgBytes, opcode)

