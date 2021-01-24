import ssl
import time
import threading

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
    Ws = None
    IsOpen = False

    def __init__(self, url, onWsOpen = None, onWsMsg = None, onWsData = None, onWsClose = None, onWsError = None, headerArray = None):

        def OnOpen(ws):
            self.IsOpen = True
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
                onWsData(self, buffer, type == websocket.ABNF.OPCODE_BINARY)

        self.Ws = WebSocketApp(url,        
                                  on_message = OnMsg,
                                  on_close = onClosed,
                                  on_error = OnError,
                                  on_data = OnData,
                                  header = headerArray
        )
        self.Ws.on_open = OnOpen

    def RunUntilClosed(self):
        # Note we must set the ping_interval and ping_timeout or we won't get a multithread
        # safe socket... python. >.>
        self.Ws.run_forever(skip_utf8_validation=True, ping_interval=600, ping_timeout=300)

    def RunAsync(self):
        t = threading.Thread(target=self.RunUntilClosed, args=())
        t.daemon = True
        t.start()

    def Close(self):
        self.IsOpen = False
        if self.Ws:
            self.Ws.close()
            self.Ws = None
    
    def Send(self, msgBytes, isData):
        if self.Ws:
            if isData:
                self.Ws.send(msgBytes, opcode=websocket.ABNF.OPCODE_BINARY)
            else:
                self.Ws.send(msgBytes, opcode=websocket.ABNF.OPCODE_TEXT)
