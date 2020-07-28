import time
import json
import sys
import struct
import threading
import requests
import jsonpickle
import traceback

# 
# This Session file respresents one connection to the service. If anything fails it is destroyed and a new connection will be made.
#

from .octoproxysocketimpl import OctoProxySocket
from .octoheaderimpl import Header

# Helper to pack ints
def pack32Int(buffer, bufferOffset, value) :
    buffer[0 + bufferOffset] = (value & 0xFF000000) >> 24
    buffer[1 + bufferOffset] = (value & 0x00FF0000) >> 16
    buffer[2 + bufferOffset] = (value & 0x0000FF00) >> 8
    buffer[3 + bufferOffset] = (value & 0x000000FF)

# Helper to unpack ints
def unpack32Int(buffer, bufferOffset) :
    return (struct.unpack('1B', buffer[0 + bufferOffset])[0] << 24) + (struct.unpack('1B', buffer[1 + bufferOffset])[0] << 16) + (struct.unpack('1B', buffer[2 + bufferOffset])[0] << 8) + struct.unpack('1B', buffer[3 + bufferOffset])[0]

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

    def __init__(self, group=None, target=None, name=None, args=(), kwargs=None, verbose=None):
        threading.Thread.__init__(self, group=group, target=target, name=name, verbose=verbose)
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
            # If this is a proxy socket, handle it
            if msg["ProxySocket"] != None :
                self.OctoSession.HandleProxySocketMessage(msg)
                return

            # If this is a handshake ack, handle it.
            if msg["HandshakeAck"] != None :
                self.OctoSession.HandleHandshakeAck(msg)
                return

            # Handle the webrequest.
            if msg["IsHttpRequest"] != None and msg["IsHttpRequest"]:
                self.OctoSession.HandleWebRequest(msg)
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
    OctoStream = None
    PrinterId = ""
    LocalHostAddress = "127.0.0.1"
    ActiveProxySockets = {}

    def __init__(self, octoStream, logger, printerId):
        self.Logger = logger
        self.OctoStream = octoStream
        self.PrinterId = printerId

    def OnSessionError(self, backoffModifierSec):
        # Just forward
        self.OctoStream.OnSessionError(backoffModifierSec)

    def Send(self, msg):
        # Encode and send the message.
        encodedMsg = encodeOctoStreamMsg(msg)
        self.OctoStream.SendMsg(encodedMsg)

    def HandleHandshakeAck(self, msg):
        # Handles a handshake ack message.
        if msg["HandshakeAck"]["Accepted"]:
            self.OctoStream.OnHandshakeComplete()
        else:
            self.Logger.error("Handshake failed, reason '" + str(msg["HandshakeAck"]["Error"] + "'"))
            backoffModifierSec = 0
            if msg["HandshakeAck"]["BackoffSeconds"]:
                backoffModifierSec = int(msg["HandshakeAck"]["BackoffSeconds"])
            self.OctoStream.OnSessionError(backoffModifierSec)

    def HandleProxySocketMessage(self, msg) :
        # Get the requested id.
        socketId = msg["ProxySocket"]["Id"]

        #
        # TODO does this map need to be thread safe?
        if "OpenDetails" in msg["ProxySocket"] and msg["ProxySocket"]["OpenDetails"] != None :

            # Check we don't have a socket already
            if socketId in self.ActiveProxySockets :
                self.Logger.error("Tried to open proxy socket id " + str(socketId) + " but it already exists.")
                # throwing here will terminate this entire OcotoSocket and reset.
                raise Exception("Tried to open proxy socket that was already open")
            
            # Sanity check there is no data.
            if len(msg["Data"]) != 0:
                self.Logger.error("A websocket connect message was sent with data!")

            # Create the proxy socket object
            s = OctoProxySocket(args=(self.Logger, socketId, self, msg, self.LocalHostAddress,))
            self.ActiveProxySockets[socketId] = s
            s.start()

        elif msg["ProxySocket"]["IsCloseMessage"] == True :
            # Check that it exists.
            if socketId in self.ActiveProxySockets :
                try:
                    self.ActiveProxySockets[socketId].Close()
                except Exception as e:
                    self.Logger.error("Exception while closing proxy socket "+str(e))

                # Remove it from the map
                self.ActiveProxySockets.pop(socketId)  

            else :
                self.Logger.error("tried to close proxy socket id " + str(socketId) + " but it already closed.")
                # throwing here will terminate this entire OcotoSocket and reset.
                raise Exception("Tried to close proxy socket that was already closed.")
        else :
            if socketId in self.ActiveProxySockets :
                # If the id exists, send the data through the socket.
                self.ActiveProxySockets[socketId].Send(msg)
            else:
                self.Logger.error("Tried to send data to a proxy socket id that doesn't exist. " + str(socketId))
                # throwing here will terminate this entire OcotoSocket and reset.
                raise Exception("Tried to send data to a proxy socket id that doesn't exist")

    def CloseAllProxySockets(self):
        # Close them all.
        self.Logger.info("Closing all open proxy sockets ("+str(len(self.ActiveProxySockets))+")")
        for id in self.ActiveProxySockets:
            try:
                self.ActiveProxySockets[id].Close()
            except Exception as e:
                self.Logger.error("Exception thrown while closing proxy socket " +str(id)+ " " + str(e))
        # Clear them all.
        self.ActiveProxySockets.clear()

    def HandleWebRequest(self, msg):                         
            # Create the path.
            path = 'http://' + self.LocalHostAddress + msg["Path"]

            # Setup the headers
            send_headers = Header.GatherRequestHeaders(msg, self.LocalHostAddress)

            # Make the local request.
            reqStart = time.time()
            response = None
            try:
                if msg["Method"] == "POST" :
                    response = requests.post(path, headers=send_headers, data= msg["Data"], timeout=60)
                else:
                    response = requests.get(path, headers=send_headers, timeout=60)
            except Exception as e:
                # If we fail to make the call then kill the connection.
                self.Logger.error("Failed to make local request. " + str(e) + " for " + path)
                self.OctoStream.OnSessionError(0)
                return

            reqEnd = time.time()
            self.Logger.info("Local Web Request took ["+str(reqEnd - reqStart)+"] for " + path)           

            # Prepare to return the response.
            outMsg = {}
            outMsg["Path"] = path
            outMsg["PairId"] = msg["PairId"]
            outMsg["IsHttpRequest"] = True

            if response != None:
                # Prepare to send back the response.
                outMsg["StatusCode"] = response.status_code
                outMsg["Data"] = response.content

                # Gather up the headers to return.
                returnHeaders = []
                for name in response.headers:
                    returnHeaders.append(Header(name, response.headers[name]))
                outMsg["Headers"] = returnHeaders
            else:
                outMsg["StatusCode"] = 408

            # Send the response
            self.Send(outMsg) 

    def StartHandshake(self):
        # Setup the message
        outMsg = {}
        handshakeSyn = {}
        outMsg["HandshakeSyn"] = handshakeSyn
        handshakeSyn["Id"] = self.PrinterId

        # Send the handshakesyn
        try:
            res = encodeOctoStreamMsg(outMsg)
            self.OctoStream.SendMsg(res)
        except Exception as e:
            self.Logger.error("Failed to send handshake syn. " + str(e))
            self.OctoStream.OnSessionError(0)
            return

    def HandleMessage(self, msgBytes):
        # Start a worker thread to handle the request.
        t = OctoMessageThread(args=(self.Logger, self, msgBytes,))
        t.start()  