#
# This logic is a python v3 only async impl of the system I did before realzing OctoPrint only runs on 2.7 ATM.
# I'm keeping it around for future updates.
#

# import requests
# import json 
# import jsonpickle

# import asyncio
# import websockets
# import base64
# import datetime

# # A dictionary of the current proxy sockets setup.
# GlobalProxySockets = {}

# # Respresents the header class we encode in json.
# class Header:
#     Name = ""
#     Value = ""
#     def __init__(self, name, value):
#         self.Name = name
#         self.Value = value

# # Helper to pack ints
# def pack32Int(buffer, bufferOffset, value) :
#     buffer[0 + bufferOffset] = (value & 0xFF000000) >> 24
#     buffer[1 + bufferOffset] = (value & 0x00FF0000) >> 16
#     buffer[2 + bufferOffset] = (value & 0x0000FF00) >> 8
#     buffer[3 + bufferOffset] = (value & 0x000000FF)

# # Helper to unpack ints
# def unpack32Int(buffer, bufferOffset) :
#     return (buffer[0 + bufferOffset] << 24) + (buffer[1 + bufferOffset] << 16) + (buffer[2 + bufferOffset] << 8) + buffer[3 + bufferOffset]

# def decodeOcotoStreamMsg(bytes) :
#     # 4 bytes - json data length
#     # 4 bytes - data length
#     # json bytes
#     # data bytes

#     # Bounds check
#     if len(bytes) < 8 :
#         return None

#     # Get the sizes
#     currentBufferOffset = 0
#     jsonLength = unpack32Int(bytes, currentBufferOffset)
#     currentBufferOffset += 4
#     dataLength = unpack32Int(bytes, currentBufferOffset)
#     currentBufferOffset += 4

#     if 4 + 4 + jsonLength + dataLength != len(bytes) :
#         print("We got an ocotomessage that's not the correct size!")
#         return None

#     # Get and decode the json message
#     ret = {}
#     jsonStr = (bytes[currentBufferOffset:currentBufferOffset+jsonLength]).decode()
#     ret = json.loads(jsonStr)
#     currentBufferOffset += jsonLength

#     # Put the data bytes where they should be
#     ret["Data"] = bytes[currentBufferOffset:]
#     return ret

# def encodeOctoStreamMsg(msg) :
#     # 4 bytes - json data length
#     # 4 bytes - data length
#     # json bytes
#     # data bytes

#     # Remove the data from the message so it doesn't get encoded.
#     data = bytearray(0)
#     if "Data" in msg :
#         data = msg["Data"]
#         msg["Data"] = None

#     # Encode the json to bytes
#     encodedJson = jsonpickle.encode(msg).encode()

#     # Create a buffer to send.
#     msgLength = len(encodedJson) + len(data) + 4 + 4
#     buffer = bytearray(msgLength)
#     currentBufferOffset = 0

#     # Fill the size of the json.
#     pack32Int(buffer, currentBufferOffset, len(encodedJson))
#     currentBufferOffset += 4

#     # Fill the data size.
#     pack32Int(buffer, currentBufferOffset, len(data))
#     currentBufferOffset += 4

#     # Copy the json bytes
#     buffer[currentBufferOffset:] = encodedJson
#     currentBufferOffset += len(encodedJson)

#     # Copy the data
#     buffer[currentBufferOffset:] = data
#     return bytes(buffer)

# # Makes the local http request call
# def MakeRequestCall(path, send_headers, method, data, isStream):
#     try:
#         if method == "POST" :
#             return requests.post(path, headers=send_headers, data=data, timeout=60, stream=isStream)
#         else:
#             return requests.get(path, headers=send_headers, timeout=60, stream=isStream)
#     except Exception as e:
#         print("Failed to make local request. " + str(e))
#     return None

# def GatherRequestHeaders(msg, hostAddress) :
#     send_headers = {}
#     for header in msg["Headers"]:
#         name = header["Name"]
#         value = header["Value"]
#         if name == "Host" :
#             value = hostAddress
#         if name == "Referer" :
#             value = "http://" + hostAddress
#         if name == "Origin" :
#             value = "http://" + hostAddress
#         if name == "Upgrade-Insecure-Requests":
#             continue 
#         send_headers[name] = value
#     return send_headers

# # Respresents a websocket connection we are proxying
# class ProxySocket:
#     OpenMsg = {}
#     HostAddress = ""
#     Id = 0
#     OctoSocket = {}
#     Ws = None
#     HttpResponse = None
#     IsClosed = False

#     def GetBaseOctoMessage(self):
#         msg = {}
#         msg["Path"] = self.OpenMsg["Path"]
#         msg["PairId"] = 0
#         msg["StatusCode"] = 200
#         msg["ProxySocket"] = {}
#         msg["ProxySocket"]["Id"] = self.Id
#         msg["ProxySocket"]["IsCloseMessage"] = False
#         return msg

#     # Handles websocket proxy sockets
#     @asyncio.coroutine
#     def HandleWebSocketConnection(self) :
#         path = self.OpenMsg["Path"]
#         uri = "ws://" + self.HostAddress + path
#         print('Opening proxy socket websocket ' + str(self.Id) + " , " + uri)
#         self.Ws = yield from websockets.connect(uri)
#         while 1:
#             # Wait for a proxy websocket message.
#             msg = await self.Ws.recv()

#             # Proxy the message.
#             send = self.GetBaseOctoMessage()

#             # Detect the correct type.
#             if isinstance(msg, str) :
#                 send["ProxySocket"]["IsBinary"] = False
#                 send["Data"] = msg.encode()
#             else:
#                 send["ProxySocket"]["IsBinary"] = True
#                 send["Data"] = msg

#             # Send 
#             await self.OctoSocket.send(encodeOctoStreamMsg(send))

#     # A sync function that reads data from the response until it has been too long.
#     def ReadResponseChunk(self, httpResponse, byteBuffer, byteBufferMaxSize, chunkReadSize, maxHoldTimeMs):
#         lastSendTime = datetime.datetime.now()
#         byteBufferLength = 0
#         try:            
#             for data in httpResponse.iter_content(chunk_size=chunkReadSize):
#                 # Only return data that's not keep-alive.
#                 if data:
#                     # Add this data to our buffer
#                     byteBuffer[byteBufferLength:] = data
#                     byteBufferLength += len(data)

#                     # To reduce overhead we want to buffer up how much data we send based on time.
#                     # Or if our buffer is about to run out of room.
#                     now = datetime.datetime.now()
#                     diff = now - lastSendTime
#                     deltaMs = diff.seconds * 1000 + diff.microseconds / 1000
#                     if deltaMs < maxHoldTimeMs and chunkReadSize + byteBufferLength < byteBufferMaxSize:
#                         continue

#                     # We are ready to return, return the final buffer length.
#                     return byteBufferLength                   
#         except Exception as e:
#             print("Exception thrown in ReadResponseChunk " + str(e))
#             return 0

#     # Handles websocket proxy sockets
#     @asyncio.coroutine
#     def HandleHttpStreamConnection(self) :
#         # Setup the path.
#         path = self.OpenMsg["Path"]
#         uri = "http://" + self.HostAddress + path
#         print("Opening proxy socket http stream " + str(self.Id) + " , " +uri)

#         # Setup the headers
#         send_headers = GatherRequestHeaders(self.OpenMsg, self.HostAddress)

#         # Try to make the http call.
#         # path, send_headers, method, data, isStream
#         self.HttpResponse = await asyncio.get_event_loop().run_in_executor(None, MakeRequestCall, uri, send_headers, self.OpenMsg["Method"], self.OpenMsg["Data"], True)

#         # Even if we fail, we want to do this to send the failure body.

#         # TODO - This is mostly used for webcam streaming and can def be made better in terms of perf

#         # Since we are sending this data over a websocket, we need to buffer the stream some so we can send it in
#         # bigger chunks for efficiency. The read size is more of a read limit, if it can't fill the read buffer 
#         # it will return more quickly.
#         isFirstResponse = True   
#         readSizeBytes = 5000
#         bufferMaxSize = 500000000
#         maxBufferAccumTimeMs = 100
#         byteBuffer = bytearray(bufferMaxSize)        
#         while 1:
#             lengthBytes = await asyncio.get_event_loop().run_in_executor(None, self.ReadResponseChunk, self.HttpResponse, byteBuffer, bufferMaxSize, readSizeBytes, maxBufferAccumTimeMs)

#             # The read failed if data is None. That means we are done.
#             if lengthBytes == 0:
#                 return

#             # Make sure we are still open, since the close function can be called before we set the HttpResponse
#             if self.IsClosed:
#                 return         

#             # Proxy the message.
#             send = self.GetBaseOctoMessage()

#             # For the first message, include the response context
#             if isFirstResponse:
#                 isFirstResponse = False
#                 send["Path"] = path
#                 send["StatusCode"] = self.HttpResponse.status_code
#                 # Gather up the headers to return.
#                 returnHeaders = []
#                 for name in self.HttpResponse.headers:
#                     returnHeaders.append(Header(name, self.HttpResponse.headers[name]))
#                 send["Headers"] = returnHeaders

#             # Detect the correct type.   
#             send["ProxySocket"]["IsBinary"] = True
#             send["Data"] = byteBuffer

#             # Send 
#             await self.OctoSocket.send(encodeOctoStreamMsg(send))   
            
#             await asyncio.sleep(0.05)

#     @asyncio.coroutine
#     def ConnectionLoop(self):
#         try:
#             # Handle the connection for the correct type.
#             if self.OpenMsg["ProxySocket"]["OpenDetails"]["IsWebSocket"] :
#                 await self.HandleWebSocketConnection()
#             else:
#                 await self.HandleHttpStreamConnection()
#         except Exception as e:
#             print("Exception in proxy socket connect loop: "+str(e))

#         # Clear out the socket connection.
#         self.Ws = None
#         self.HttpResponse = None
#         self.IsClosed = True

#         # When we exit the run loop, make sure we send a closed message.
#         try:
#             send = self.GetBaseOctoMessage()
#             send["ProxySocket"]["IsCloseMessage"] = True
#             await self.OctoSocket.send(encodeOctoStreamMsg(send))            
#         except Exception as e:
#             print("Failed to send proxy socket close message" + str(e))
#         print("Closed proxy socket id "+ str(self.Id))


#     # Sends data coming from the OctoSocket
#     @asyncio.coroutine
#     def Send(self, msg):
#         ws = self.Ws
#         if ws != None:
#             # Convert the data to the correct send type
#             data = msg["Data"]
#             if msg["ProxySocket"]["IsBinary"] == False :
#                 data = data.decode("utf-8")
#             await ws.send(data)
#             return
#         print("Not supported - Data was attempted to be sent to a proxy socket http")

#     # Closes the proxy socket.
#     @asyncio.coroutine
#     def Close(self):
#         self.IsClosed = True
#         # If we have a ws, close it here.
#         ws = self.Ws
#         if ws != None:
#             await ws.close()
#             return
#         # If we have http stream, close it.
#         httpResponse = self.HttpResponse
#         if httpResponse != None:
#             try:
#                 httpResponse.close()
#             except Exception as e:
#                 print("Failed to close http response." + str(e))


#     def __init__(self, hostAddress, msg, id, octoSocket):
#         self.OpenMsg = msg
#         self.HostAddress = hostAddress
#         self.Id = id
#         self.OctoSocket = octoSocket
#         # Start a async task to handle the connection.
#         asyncio.get_event_loop().create_task(self.ConnectionLoop())

# # Handles sending proxy socket messages.
# @asyncio.coroutine
# def handleProxySocketMessage(hostAddress, msg, octoSocket) :

#     # Get the requested id.
#     socketId = msg["ProxySocket"]["Id"]

#     if "OpenDetails" in msg["ProxySocket"] and msg["ProxySocket"]["OpenDetails"] != None :

#         # Check we don't have a socket already
#         if socketId in GlobalProxySockets :
#             print("Error, tried to open proxy socket id " + str(socketId) + " but it already exists.")
#             # throwing here will terminate this entire OcotoSocket and reset.
#             raise Exception("Tried to open proxy socket that was already open")
        
#         # Sanity check there is no data.5
#         if len(msg["Data"]) != 0:
#             print("Error: A websocket connect message was sent with data!")

#         # Create the proxy socket object
#         GlobalProxySockets[socketId] = ProxySocket(hostAddress, msg, socketId, octoSocket)

#     elif msg["ProxySocket"]["IsCloseMessage"] == True :
#         # Check that it exists.
#         if socketId in GlobalProxySockets :
#             print("Closing proxy socket id "+ str(socketId))
#             try:
#                 await GlobalProxySockets[socketId].Close()
#             except Exception as e:
#                 print("Exception while closing proxy socket "+str(e))

#             # Remove it from the map
#             GlobalProxySockets.pop(socketId)  

#         else :
#             print("Error, tried to close proxy socket id " + str(socketId) + " but it already closed.")
#             # throwing here will terminate this entire OcotoSocket and reset.
#             raise Exception("Tried to close proxy socket that was already closed.")
#     else :
#         if socketId in GlobalProxySockets :
#             # If the id exists, send the data through the socket.
#             await GlobalProxySockets[socketId].Send(msg)
#         else:
#             print("Error, tried to send data to a proxy socket id that doesn't exist. " + str(socketId))
#             # throwing here will terminate this entire OcotoSocket and reset.
#             raise Exception("Tried to send data to a proxy socket id that doesn't exist")    

# # Closes and removes all of the current proxy sockets
# @asyncio.coroutine
# def closeAllProxySockets():
#     # Close them all.
#     for id in GlobalProxySockets:
#         try:
#             await GlobalProxySockets[id].Close()
#         except Exception as e:
#             print("Exception thrown while closing proxy socket " + str(e))
#     # Clear them all.
#     GlobalProxySockets.clear()

# @asyncio.coroutine
# def handleIncomingMessage(bytes, octoSocket, hostAddress) :
#     try:
#         # Parse the message
#         msg = decodeOcotoStreamMsg(bytes)
#         if msg == None :
#             return

#         # If this is a proxy socket, handle it
#         if msg["ProxySocket"] != None :
#             return await handleProxySocketMessage(hostAddress, msg, octoSocket)

#         # Create the path.
#         path = 'http://' + hostAddress + msg["Path"]

#         # Setup the headers
#         send_headers = GatherRequestHeaders(msg, hostAddress)
        
#         # Make the local request.
#         # Using this run in executor we can wait on the synchronous function with an await.
#         print("Making local request for " + path)
#         response = await asyncio.get_event_loop().run_in_executor(None, MakeRequestCall, path, send_headers, msg["Method"], msg["Data"], False)

#         outMsg = {}
#         outMsg["Path"] = path
#         outMsg["PairId"] = msg["PairId"]

#         if response != None:
#             # Prepare to send back the response.
#             outMsg["StatusCode"] = response.status_code
#             outMsg["Data"] = response.content

#             # Gather up the headers to return.
#             returnHeaders = []
#             for name in response.headers:
#                 returnHeaders.append(Header(name, response.headers[name]))
#             outMsg["Headers"] = returnHeaders
#         else:
#             outMsg["StatusCode"] = 408

#         # Send the response back.
#         result = encodeOctoStreamMsg(outMsg)
#         await octoSocket.send(result)

#     except Exception as e:
#         # If anything in here fails, we want to abort the entire connection. We can do so by closing the main socket.
#         print("Exception thrown in incoming octo socket handing." + str(e))
#         try:
#             await octoSocket.close()
#         except Exception as e:
#             pass

# @asyncio.coroutine
# def mainLoop():
#     octoStreamWsUri = "wss://octoeverywhere.com/octoclientws"
#     octoPrinterHost = "localhost"
#     #octoPrinterHost = "192.168.86.43"
#     #octoStreamWsUri = "ws://localhost:5000/octoclientws"

#     backOffValue = 0

#     # Loop forever.
#     while 1:
#         try:
#             # Try to open a new websocket to the service.
#             print("Connecting to websocket.")
#             async with websockets.connect(octoStreamWsUri) as octoSocket:

#                 # While the socket hasn't thrown, keep using it.
#                 while 1:
#                     # Wait for a new message.
#                     rawMsg = await octoSocket.recv()

#                     # Reset out backoff counter
#                     backOffValue = 0

#                     # Process the message on an async task so we can keep reading new requests.
#                     asyncio.get_event_loop().create_task(handleIncomingMessage(rawMsg, octoSocket, octoPrinterHost))

#         except Exception as e:
#             print("Exception in main loop: "+str(e))

#         # When we exit this socket loop, we want to clean up any proxy sockets.
#         print("Closing all proxy sockets")
#         await closeAllProxySockets()

#         # When we fail, sleep for a back off value.
#         await asyncio.sleep(backOffValue * 0.5)
#         if backOffValue < 10:
#             backOffValue += 1

# # Start the main loop. This should never exit.
# if __name__ == "__main__":
#     asyncio.get_event_loop().run_until_complete(mainLoop())
