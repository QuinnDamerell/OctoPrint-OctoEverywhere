import time
import json
import sys
import struct
import threading
import requests
import jsonpickle
import traceback
import zlib

# 
# This Session file respresents one connection to the service. If anything fails it is destroyed and a new connection will be made.
#

from .octoproxysocketimpl import OctoProxySocket
from .octoheaderimpl import Header
from .octoutils import Utils

# Helper to pack ints
def pack32Int(buffer, bufferOffset, value) :
    buffer[0 + bufferOffset] = (value & 0xFF000000) >> 24
    buffer[1 + bufferOffset] = (value & 0x00FF0000) >> 16
    buffer[2 + bufferOffset] = (value & 0x0000FF00) >> 8
    buffer[3 + bufferOffset] = (value & 0x000000FF)

# Helper to unpack ints
def unpack32Int(buffer, bufferOffset) :
    if sys.version_info[0] < 3:
        return (struct.unpack('1B', buffer[0 + bufferOffset])[0] << 24) + (struct.unpack('1B', buffer[1 + bufferOffset])[0] << 16) + (struct.unpack('1B', buffer[2 + bufferOffset])[0] << 8) + struct.unpack('1B', buffer[3 + bufferOffset])[0]
    else:
        return (buffer[0 + bufferOffset] << 24) + (buffer[1 + bufferOffset] << 16) + (buffer[2 + bufferOffset] << 8) + (buffer[3 + bufferOffset])

# Decodes an OctoStream message.
def decodeOcotoStreamMsg(data) :
    # 4 bytes - json data length
    # 4 bytes - data length
    # json bytes
    # data bytes

    # Bounds check
    if len(data) < 8 :
        raise Exception("The message length is less then 8 bytes!")

    # Get the sizes
    currentBufferOffset = 0
    jsonLength = unpack32Int(data, currentBufferOffset)
    currentBufferOffset += 4
    dataLength = unpack32Int(data, currentBufferOffset)
    currentBufferOffset += 4

    if 4 + 4 + jsonLength + dataLength != len(data) :
        raise Exception("We got an ocotomessage that's not the correct size!")

    # Get and decode the json message
    ret = {}
    jsonStr = (data[currentBufferOffset:currentBufferOffset+jsonLength]).decode()
    ret = json.loads(jsonStr)
    currentBufferOffset += jsonLength

    # Put the data bytes where they should be
    ret["Data"] = data[currentBufferOffset:]
    return ret

# Encodes an octo stream message.
def encodeOctoStreamMsg(msg) :
    # 4 bytes - json data length
    # 4 bytes - data length
    # json bytes
    # data bytes

    # Set our current protocol version.
    msg["Version"] = 1

    # Remove the data from the message so it doesn't get encoded.
    data = bytearray(0)
    if "Data" in msg :
        data = msg["Data"]
        msg["Data"] = None

    # Encode the json to bytes
    encodedJson = jsonpickle.encode(msg).encode()

    # Create a buffer to send.
    msgLength = len(encodedJson) + len(data) + 4 + 4
    buffer = bytearray(msgLength)
    currentBufferOffset = 0

    # Fill the size of the json.
    pack32Int(buffer, currentBufferOffset, len(encodedJson))
    currentBufferOffset += 4

    # Fill the data size.
    pack32Int(buffer, currentBufferOffset, len(data))
    currentBufferOffset += 4

    # Copy the json bytes
    buffer[currentBufferOffset:] = encodedJson
    currentBufferOffset += len(encodedJson)

    # Copy the data
    buffer[currentBufferOffset:] = data
    return bytes(buffer)

# Used to handle each incoming message from the service. We run a thread for each so they can be handled in parallel.
# TODO, we might want to use some kind of pool in the future to have a limit of concurrent threads.
class OctoMessageThread(threading.Thread):
    Logger = None
    OctoSession = None
    IncomingData = None

    def __init__(self, group=None, target=None, name=None, args=(), kwargs=None):
        threading.Thread.__init__(self, group=group, target=target, name=name)
        self.Logger = args[0]
        self.OctoSession = args[1]
        self.IncomingData = args[2]
        return

    def run(self):
        msg = None
        try:
            msg = decodeOcotoStreamMsg(self.IncomingData)
        except Exception as e:
            self.Logger.error("Failed to decode message local request. " + str(e))
            self.OctoSession.OnSessionError(0)
            return
    
        try:
            # If this is a handshake ack, handle it.
            if "HandshakeAck" in msg and msg["HandshakeAck"] != None :
                self.OctoSession.HandleHandshakeAck(msg)
                return

            # If this is a proxy socket, handle it
            if "ProxySocket" in msg and msg["ProxySocket"] != None :
                self.OctoSession.HandleProxySocketMessage(msg)
                return

            # If this is a client notification, handle it.
            if "Notification" in msg and msg["Notification"] != None :
                self.OctoSession.HandleClientNotification(msg)
                return

            # Handle the webrequest.
            if "IsHttpRequest" in msg and msg["IsHttpRequest"] != None and msg["IsHttpRequest"]:
                self.OctoSession.HandleWebRequest(msg)
                return

            # Handle the summon request.
            if "Summon" in msg and msg["Summon"] != None :
                self.OctoSession.HandleSummonRequest(msg)
                return

            # We don't know what this is, probally a new message we don't understand.
            self.Logger.info("Unknown message type received, ignoring.")
            return

        except Exception as e:
            # If anything throws, we consider it a protocol failure.
            traceback.print_exc()
            self.Logger.error("Failed to handle octo message. " + str(e))
            self.OctoSession.OnSessionError(0)
            return

class OctoSession:
    Logger = None
    SessionId = 0
    UiPopupInvoker = None
    OctoStream = None
    OctoPrintLocalPort = 80
    MjpgStreamerLocalPort = 8080
    PrinterId = ""
    LocalHostAddress = "127.0.0.1"
    PluginVersion = ""
    ActiveProxySockets = {}
    ActiveProxySocketsLock = threading.Lock()

    def __init__(self, octoStream, logger, printerId, sessionId, octoPrintLocalPort, mjpgStreamerLocalPort, uiPopupInvoker, pluginVersion):
        self.Logger = logger
        self.SessionId = sessionId
        self.OctoStream = octoStream
        self.PrinterId = printerId
        self.OctoPrintLocalPort = octoPrintLocalPort
        self.MjpgStreamerLocalPort = mjpgStreamerLocalPort
        self.UiPopupInvoker = uiPopupInvoker
        self.PluginVersion = pluginVersion

    def OnSessionError(self, backoffModifierSec):
        # Just forward
        self.OctoStream.OnSessionError(self.SessionId, backoffModifierSec)

    def Send(self, msg):
        # Encode and send the message.
        encodedMsg = encodeOctoStreamMsg(msg)
        self.OctoStream.SendMsg(encodedMsg)

    def HandleSummonRequest(self, msg):
        try:
            summonConnectUrl = msg["Summon"]["ServerConnectUrl"]
            self.OctoStream.OnSummonRequest(self.SessionId, summonConnectUrl)
        except Exception as e:
            self.Logger.error("Failed to handle summon request " + str(e))

    def HandleClientNotification(self, msg): 
        try:
            title = msg["Notification"]["Title"]
            text = msg["Notification"]["Text"]
            type = msg["Notification"]["Type"].lower()
            autoHide = msg["Notification"]["AutoHide"]
            self.UiPopupInvoker.ShowUiPopup(title, text, type, autoHide)
        except Exception as e:
            self.Logger.error("Failed to handle octo notification message. " + str(e))

    def HandleHandshakeAck(self, msg):
        # Handles a handshake ack message.
        if msg["HandshakeAck"]["Accepted"]:
            self.OctoStream.OnHandshakeComplete(self.SessionId)
        else:
            self.Logger.error("Handshake failed, reason '" + str(msg["HandshakeAck"]["Error"] + "'"))
            # The server can send back a backoff time we should respect.
            backoffModifierSec = 0
            if msg["HandshakeAck"]["BackoffSeconds"]:
                backoffModifierSec = int(msg["HandshakeAck"]["BackoffSeconds"])
            self.OnSessionError(backoffModifierSec)

    def HandleProxySocketMessage(self, msg) :
        # Get the requested id.
        socketId = msg["ProxySocket"]["Id"]

        if "OpenDetails" in msg["ProxySocket"] and msg["ProxySocket"]["OpenDetails"] != None :
            # Grab the lock before messing with the map.
            self.ActiveProxySocketsLock.acquire()
            try:
                # Check we don't have a socket already
                if socketId in self.ActiveProxySockets :
                    self.Logger.error("Tried to open proxy socket id " + str(socketId) + " but it already exists.")
                    # throwing here will terminate this entire OcotoSocket and reset.
                    raise Exception("Tried to open proxy socket that was already open")
                    
                # Sanity check there is no data.
                if len(msg["Data"]) != 0:
                    self.Logger.error("A websocket connect message was sent with data!")

                # Create the proxy socket object
                s = OctoProxySocket(args=(self.Logger, socketId, self, msg, self.LocalHostAddress, self.OctoPrintLocalPort, self.MjpgStreamerLocalPort,))
                self.ActiveProxySockets[socketId] = s
                s.start()

            except Exception as _:
                # rethrow any exceptions in the code
                raise                
            finally:
                # Always unlock                
                self.ActiveProxySocketsLock.release()

        elif msg["ProxySocket"]["IsCloseMessage"] == True :
            # Grab the lock before messing with the map.
            localSocket = None
            self.ActiveProxySocketsLock.acquire()
            try:
                # Try to copy the socket locally.
                if socketId in self.ActiveProxySockets :
                    # If we find it, take it and remove it.
                    localSocket = self.ActiveProxySockets[socketId]
                    self.ActiveProxySockets.pop(socketId)  
                else:
                    self.Logger.error("tried to close proxy socket id " + str(socketId) + " but it already closed.")
                    # throwing here will terminate this entire OcotoSocket and reset.
                    raise Exception("Tried to close proxy socket that was already closed.")
            except Exception as _:
                # rethrow any exceptions in the code
                raise                
            finally:
                # Always unlock                
                self.ActiveProxySocketsLock.release()

            # Check we got it. We always should, but it never hurts.
            if localSocket == None:
                self.Logger.error("tried to close proxy socket id " + str(socketId) + " but it returned a null socket?")
                # throwing here will terminate this entire OcotoSocket and reset.
                raise Exception("Tried to close proxy socket but got a null socket obj.")

            # Now actually try to close the socket
            try:
                localSocket.Close()
            except Exception as e:
                # If we get an error report it and throw again, so the connection will
                # reset.
                self.Logger.error("Exception while closing proxy socket "+str(e))
                raise Exception("Exception while closing proxy socket")

        else :
            # Grab the lock before messing with the map.
            localSocket = None
            self.ActiveProxySocketsLock.acquire()
            try:
                # Try to copy the socket locally.
                if socketId in self.ActiveProxySockets :
                    localSocket = self.ActiveProxySockets[socketId]
                else:
                    self.Logger.error("Tried to send data to a proxy socket id that doesn't exist. " + str(socketId))
                    # throwing here will terminate this entire OcotoSocket and reset.
                    raise Exception("Tried to send data to a proxy socket id that doesn't exist")
            except Exception as _:
                # rethrow any exceptions in the code
                raise                
            finally:
                # Always unlock                
                self.ActiveProxySocketsLock.release()

            # Check we got it. We always should, but it never hurts.
            if localSocket == None:
                self.Logger.error("tried to send a message on a proxy socket id " + str(socketId) + " but it returned a null socket?")
                # throwing here will terminate this entire OcotoSocket and reset.
                raise Exception("Tried to send a message on a proxy socket but got a null socket obj.")

            # Send the message!
            localSocket.Send(msg)

            
    def CloseAllProxySockets(self):
        # To be thread safe, lock the map, copy all of the sockets out, then close them all.
        localSocketList = {}
        self.ActiveProxySocketsLock.acquire()
        try:
            # Close them all.
            self.Logger.info("Closing all open proxy sockets ("+str(len(self.ActiveProxySockets))+")")

            # Copy all of the sockets locally
            for id in self.ActiveProxySockets:
                localSocketList[id] = self.ActiveProxySockets[id]

            # Clear them all from the global map
            self.ActiveProxySockets.clear()
            
        except Exception as _:
            # rethrow any exceptions in the code
            raise                
        finally:
            # Always unlock                
            self.ActiveProxySocketsLock.release()

        # Try catch all of this so we don't leak exceptions.
        # Use our local socket list to tell them all to close.
        try:
            for id in localSocketList:
                try:
                    localSocketList[id].Close()
                except Exception as e:
                    self.Logger.error("Exception thrown while closing proxy socket " +str(id)+ " " + str(e))
        except Exception as ex:
            self.Logger.error("Exception thrown while closing all proxy sockets" + str(ex))

    def HandleWebRequest(self, msg):                         
            # Create the path.
            addressAndPort = self.LocalHostAddress + ':' + str(self.OctoPrintLocalPort)
            path = 'http://' + addressAndPort + msg["Path"]

            # Any path that is directed to /webcam/ needs to go to mjpg-streamer instead of
            # the OctoPrint instance. If we detect it, we need to use a different path.
            if Utils.IsWebcamRequest(msg["Path"]) :
                path = Utils.GetWebcamRequestPath(msg["Path"], self.LocalHostAddress, self.MjpgStreamerLocalPort)

            # Setup the headers
            send_headers = Header.GatherRequestHeaders(msg, addressAndPort)

            # Make the local request.
            # Note we use a long timeout because some api calls can hang for a while.
            # For example when plugins are installed, some have to compile which can take some time.
            # Also note we want to disable redirects. Since we are proxying the http calls, we want to send
            # the redirect back to the client so it can handle it. Otherwise we will return the redirected content
            # for this url, which is incorrect. The X-Forwarded-Host header will tell the OctoPrint server the correct
            # place to set the location redirect header.
            reqStart = time.time()
            response = None
            try:
                response = requests.request(msg['Method'], path, headers=send_headers, data= msg["Data"], timeout=1800, allow_redirects=False)
            except Exception as e:
                # If we fail to make the call then kill the connection.
                self.Logger.error("Failed to make local request. " + str(e) + " for " + path)
                self.OnSessionError(0)
                return

            reqEnd = time.time()         

            # Prepare to return the response.
            outMsg = {}
            outMsg["Path"] = path
            outMsg["PairId"] = msg["PairId"]
            outMsg["IsHttpRequest"] = True

            ogDataSize = 0
            compressedSize = 0
            if response != None:
                # Prepare to send back the response.
                outMsg["StatusCode"] = response.status_code
                outMsg["Data"] = response.content
                ogDataSize = len(outMsg["Data"])

                # Gather up the headers to return.
                compressData = False
                returnHeaders = []
                for name in response.headers:
                    nameLower = name.lower()

                    # Since we send the entire result as one non-encoded
                    # payload we want to drop this header. Otherwise the server might emit it to 
                    # the client, when it actually doesn't match what the server sends to the client.
                    # Note: Typically, if the OctoPrint web server sent something chunk encoded, 
                    # our web server will also send it to the client via chunk encoding. But it will handle
                    # that on it's own and set the header accordingly.
                    if nameLower == "transfer-encoding":
                        continue

                    # Add the output header
                    returnHeaders.append(Header(name, response.headers[name]))

                    # Look for the content type header. Anything that is text or
                    # javascript we will compress before sending.
                    if nameLower == "content-type":
                        valueLower = response.headers[name].lower()
                        if valueLower.find("text/") == 0 or valueLower.find("javascript") != -1 or valueLower.find("json") != -1:
                            compressData = True

                # Set the headers
                outMsg["Headers"] = returnHeaders

                # Compress the data if needed.
                if compressData:
                    ogDataSize = len(outMsg["Data"])
                    outMsg["DataCompression"] = "zlib"
                    outMsg["OriginalDataSize"] = ogDataSize
                    outMsg["Data"] = zlib.compress(outMsg["Data"])
                    compressedSize = len(outMsg["Data"])                    
            else:
                outMsg["StatusCode"] = 408

            processTime = time.time() 

            # Send the response
            self.Send(outMsg) 

            # Log about it.
            sentTime = time.time() 
            self.Logger.info("Web Request [call:"+str(format(reqEnd - reqStart, '.3f'))+"s; process:"+str(format(processTime - reqEnd, '.3f'))+"s; send:"+str(format(sentTime - processTime, '.3f'))+"s] size ("+str(ogDataSize)+"->"+str(compressedSize)+") for " + path)

    def StartHandshake(self):
        # Setup the message
        outMsg = {}
        handshakeSyn = {}
        outMsg["HandshakeSyn"] = handshakeSyn
        handshakeSyn["Id"] = self.PrinterId
        handshakeSyn["PluginVersion"] = self.PluginVersion

        # Send the handshakesyn
        try:
            res = encodeOctoStreamMsg(outMsg)
            self.OctoStream.SendMsg(res)
        except Exception as e:
            self.Logger.error("Failed to send handshake syn. " + str(e))
            self.OnSessionError(0)
            return

    def HandleMessage(self, msgBytes):
        # Start a worker thread to handle the request.
        t = OctoMessageThread(args=(self.Logger, self, msgBytes,))
        t.start()  