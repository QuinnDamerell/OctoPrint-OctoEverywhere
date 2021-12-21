import threading
import certifi

import websocket
from websocket import WebSocketApp

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

        def OnData(ws, buffer, msgType, continueFlag):
            if onWsData:
                onWsData(self, buffer, msgType)

        self.Ws = WebSocketApp(url,
                                  on_message = OnMsg,
                                  on_close = onClosed,
                                  on_error = OnError,
                                  on_data = OnData,
                                  header = headers
        )
        self.Ws.on_open = OnOpen
        self.onWsError = onWsError

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
        try:
            self.Ws.run_forever(skip_utf8_validation=True, ping_interval=600, ping_timeout=300, sslopt={"ca_certs":certifi.where()})
        except Exception as e:
            # There's a compat issue where  run_forever will try to access "isAlive" when the socket is closing
            # "isAlive" apparently doesn't exist in some PY versions of thread, so this throws. We will ingore that error,
            # But for others we will call OnError.
            #
            # If it is the error message we will just return indication that the socket is closed.
            msg = str(e)
            if "'Thread' object has no attribute 'isAlive'" not in msg:
                if self.onWsError:
                    self.onWsError(self, "run_forever threw and exception: "+msg)

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
