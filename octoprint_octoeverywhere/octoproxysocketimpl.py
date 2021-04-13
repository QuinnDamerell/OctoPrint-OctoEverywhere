import threading
import requests
import datetime
import time
import traceback
import sys

from .websocketimpl import Client
from .octoheaderimpl import HeaderHelper
from .octohttprequest import OctoHttpRequest

#
# Respresents a websocket connection we are proxying or a http stream we are sending.
#
class OctoProxySocket(threading.Thread):

    def __init__(self, group=None, target=None, name=None, args=(), kwargs=None, verbose=None):
        threading.Thread.__init__(self, group=group, target=target, name=name)
        self.Logger = args[0]    
        self.Id = args[1] 
        self.OctoSession = args[2] 
        self.OpenMsg = args[3] 
        self.Ws = None
        self.IsClosed = False
        self.IsOpened = False
        self.HttpResponse = None

    def run(self):
        try:
            # Handle the connection for the correct type.
            if self.OpenMsg["ProxySocket"]["OpenDetails"]["IsWebSocket"] :
                self.HandleWebSocketConnection()
            else:
                self.HandleHttpStreamConnection()
        except Exception as e:
            self.Logger.error("Exception in proxy socket connect loop: "+str(e))
            traceback.print_exc()

        # Clear out the socket connection.
        self.Ws = None
        self.HttpResponse = None
        self.IsClosed = True

        # When we exit the run loop, make sure we send a closed message.
        try:
            send = self.GetBaseOctoMessage()
            send["ProxySocket"]["IsCloseMessage"] = True
            self.OctoSession.Send(send)            
        except Exception as e:
            self.Logger.error("Failed to send proxy socket close message" + str(e))
            self.OctoSession.OnSessionError(0)
        self.Logger.info("Closed proxy socket id "+ str(self.Id))

    def GetBaseOctoMessage(self):
        msg = {}
        msg["PairId"] = 0
        msg["StatusCode"] = 200
        msg["ProxySocket"] = {}
        msg["ProxySocket"]["Id"] = self.Id
        msg["ProxySocket"]["IsCloseMessage"] = False
        return msg

    def OnWsOpened(self, ws):
        # When we get the opened callback, set the flag so we know it's ok
        # to send messages.
        self.IsOpened = True
        self.Logger.info('Proxy socket websocket ' + str(self.Id) + " opened")

    def OnWsClosed(self, ws):
        self.Logger.info('Proxy socket websocket ' + str(self.Id) + " closed")

    def OnWsError(self, ws, err):
        self.Logger.info('Proxy socket websocket ' + str(self.Id) + " error " + str(err))

    def OnWsData(self, ws, buffer, isData):
        try:
            # Proxy the message.
            send = self.GetBaseOctoMessage()

            if isData:
                send["ProxySocket"]["IsBinary"] = True
                send["Data"] = buffer
            else:
                send["ProxySocket"]["IsBinary"] = False
                send["Data"] = buffer.encode()           

            # Send 
            self.OctoSession.Send(send)            

        except Exception as e:
            # If we fail, shutdown the session.
            self.Logger.error("Failed to send proxy socket message to OctoEverywhere" + str(e))
            self.Ws.Close()
            self.OctoSession.OnSessionError(0)

    # Sends data coming from the OctoSocket
    def Send(self, msg):
        ws = self.Ws
        if ws != None:
            # Convert the data to the correct send type
            data = msg["Data"]
            isBinary = msg["ProxySocket"]["IsBinary"]
            if isBinary == False :
                data = data.decode("utf-8")

            # Some applications send data super quickly after opening the socket. 
            # In this case, we need to wait to make sure the socket is opened fully before we try to send or it will fail.
            sleepCount = 0
            while self.IsOpened != True:
                # Sleep for one second.
                time.sleep(1)
                
                # Sanity check that we don't loop forever.
                sleepCount += 1 
                if sleepCount > 30:
                    raise Exception('We had data to send to OctoPrint on a proxy websocket but it didnt open after 30 seconds.') 

            # If we have been closed return now.
            if self.IsClosed:
                return

            # Now send.            
            ws.Send(data, isBinary)
            return
        self.Logger.error("Not supported - Data was attempted to be sent to a proxy socket http")

    # Closes the proxy socket.
    def Close(self):
        self.IsClosed = True

        # If we have a ws, close it here.
        ws = self.Ws
        if ws != None:
            try:
                ws.Close()
            except Exception as e: 
                self.Logger.error("Failed to close proxy socket "+ str(self.Id))
            return

        # If we have http stream, close it.
        httpResponse = self.HttpResponse
        if httpResponse != None:
            try:
                httpResponse.close()
            except Exception as e:
                self.Logger.error("Failed to close http response." + str(e))

    # Handles websocket proxy sockets
    def HandleWebSocketConnection(self) :
        # Open the ws and run it until it closed.
        # TODO - Handle ABL Urls
        path = self.OpenMsg["Path"]

        # For the websocket use the correct OctoPrint port number
        uri = "ws://" + OctoHttpRequest.GetLocalhostAddress() + ":" + str(OctoHttpRequest.GetLocalOctoPrintPort()) + path
        self.Logger.info('Opening proxy socket websocket ' + str(self.Id) + " , " + uri)
        self.Ws = Client(uri, self.OnWsOpened, None, self.OnWsData, self.OnWsClosed, self.OnWsError)
        if self.IsClosed:
            return
        self.Ws.RunUntilClosed()

        # TODO - We should tell the service the websocket is closed when this exits.

    # Reads a single chunk from the http response.
    def ReadStreamChunk(self, byteBuffer, boundaryStr):
        frameSize = 0
        headerSize = 0
        gotData = False

        # Read in an initial chunk of data to get the headers.
        try: 
            for data in self.HttpResponse.iter_content(chunk_size=200):
                # Skip keepalives
                if data:        
                    # Add this initial buffer to our output 
                    gotData = True   
                    byteBuffer[0:] = data
                    headerStr = str(data)

                    # Find out how long the headers are. The \r\n\r\n sequence ends the headers.
                    # For python2, we need to change the format of the strings.
                    endOfAllHeadersMatch = "\\r\\n\\r\\n"
                    endOfHeaderMatch = "\\r\\n"
                    if sys.version_info[0] < 3:
                        endOfAllHeadersMatch =  "\r\n\r\n"
                        endOfHeaderMatch = "\r\n"

                    headerSize = headerStr.find(endOfAllHeadersMatch)
                    if headerSize == -1:
                        # We failed.
                        self.Logger.error("Failed to find header size in http stream." +str(data))
                        return 0

                    # Add 4 bytes for the \r\n\r\n sequence and two bytes for the \r\n at the end of the chunk.
                    headerSize += 4 + 2

                    # Split out the headers
                    headers = headerStr.split(endOfHeaderMatch)
                    foundLen = False
                    for header in headers:
                        if header.lower().startswith("content-length"):
                            p = header.split(':')
                            if len(p) != 2:
                                self.Logger.error("Failed to parse content-length in http stream header")
                                return 0

                            frameSize = int(p[1].strip())
                            foundLen = True
                            break
                    if foundLen:
                        break
        except Exception as e:
            # Only report an error if we got data and not if there wasn't any.
            # If we didn't get any data the http connection is closed.
            if gotData:
                self.Logger.error("Exception thrown in http stream chunk reader "+str(e))
            return 0

        # If we didn't get a frame size we failed        
        if frameSize == 0:
            # Only report an error if we got data and not if there wasn't any.
            # If we didn't get any data the http connection is closed.
            if gotData:
                self.Logger.error("Failed to find frame size in http stream.")
            return 0

        # Compute how much more we need to read.
        toRead = (frameSize + headerSize) - len(byteBuffer)
        if toRead < 0:
            self.Logger.error("http stream to read size is less than 0")
            return 0

        if toRead > 0:
            # Read the rest of the frame
            for data in self.HttpResponse.iter_content(chunk_size=toRead):
                if data:
                    byteBuffer[len(byteBuffer):] = data
                    break      
        return len(byteBuffer)

    # Reads a single chunk from the http response.
    def ReadEvent(self, byteBuffer):
        # Not exactly sure how to do this, so for now we will just read each chunk.
        try:
            for data in self.HttpResponse.iter_content(chunk_size=200):
                # Skip keepalives
                if data:         
                    byteBuffer[0:] = data
                    return len(byteBuffer)
        except requests.exceptions.StreamConsumedError as _:
            return 0

    # Handles websocket proxy sockets
    def HandleHttpStreamConnection(self) :

        # Setup the headers
        sendHeaders = HeaderHelper.GatherRequestHeaders(self.OpenMsg)

        # Find the method
        method = self.OpenMsg["Method"]
        if method != "POST" and method != "GET" :
            self.Logger.error(method+" method is not supported for stream sockets.")
            return

        # Make the http request.
        httpResult = OctoHttpRequest.MakeHttpCall(self.Logger, self.OpenMsg, method, sendHeaders, self.OpenMsg["Data"], True)
        if httpResult == None:
            self.Logger.error("Failed to make http request for http stream " + str(self.Id))
            return

        # Set the results.
        self.HttpResponse = httpResult.Result
        uri = httpResult.Url
        self.Logger.info("Opening proxy socket http stream " + str(self.Id) + ", " +uri)

        # The response should indicate the boundary or that it's an event stream.
        # Otherwise this code won't work.
        boundaryStr = None
        isEventStream = False
        for name in self.HttpResponse.headers:
            if name.lower().startswith("content-type"):
                value = self.HttpResponse.headers[name]

                # Check for event streams.
                if value.lower().find("text/event-stream;") != -1:
                    isEventStream = True
                    break

                # Check for a boundary
                if value.find('=') == -1:
                    self.Logger.error("Failed to setup http stream, expected boundary not found. "+ value)
                    return                
                parts = value.split("=")
                if len(parts[1]) == 0:
                    self.Logger.error("Failed to setup http stream, expected boundary not found. "+ value)
                    return
                boundaryStr = parts[1]
                if boundaryStr.find(';') != -1:
                    self.Logger.error("Failed to setup http stream, the boundary value was not expected. "+ boundaryStr)
                    return
                break
        
        # Make sure we found something.
        if boundaryStr == None and not isEventStream:
            self.Logger.error("Failed to setup http stream, expected boundary or event stream not found in headers.")
            return

        byteBuffer = bytearray(10)   
        isFirstResponse = True 
        while 1:      
            lengthBytes = 0      
            if isEventStream:
                # Read anything that comes in.
                lengthBytes = self.ReadEvent(byteBuffer)
            else:
                # Read in a chunk.
                lengthBytes = self.ReadStreamChunk(byteBuffer, boundaryStr)

            # The read failed if data is None. That means we are done.
            if lengthBytes == 0:
                return

            # Make sure we are still open, since the close function can be called before we set the HttpResponse
            if self.IsClosed:
                return

            # Proxy the message.
            send = self.GetBaseOctoMessage()

            # For the first message, include the response context
            if isFirstResponse:
                isFirstResponse = False
                send["StatusCode"] = self.HttpResponse.status_code
                # Gather up the headers to return.
                returnHeaders = []
                for name in self.HttpResponse.headers:
                    returnHeaders.append({"Name":name, "Value":self.HttpResponse.headers[name]})
                send["Headers"] = returnHeaders

            # Detect the correct type.   
            send["ProxySocket"]["IsBinary"] = True
            send["Data"] = byteBuffer

            # Send 
            self.OctoSession.Send(send)