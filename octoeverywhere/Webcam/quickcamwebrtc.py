import logging
import argparse
import requests
import asyncio
import base64
import json


from aiortc import RTCIceCandidate, RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
from aiortc.contrib.media import MediaStreamTrack
from aiortc.contrib.signaling import BYE, add_signaling_arguments, create_signaling


class MediaBlackhole:
    """
    A media sink that consumes and discards all media.
    """

    def __init__(self) -> None:
        self.__tracks: Dict[MediaStreamTrack, asyncio.Future] = {}

    def addTrack(self, track):
        """
        Add a track whose media should be discarded.

        :param track: A :class:`aiortc.MediaStreamTrack`.
        """
        if track not in self.__tracks:
            self.__tracks[track] = None

    async def start(self) -> None:
        """
        Start discarding media.
        """
        for track, task in self.__tracks.items():
            if task is None:
                self.__tracks[track] = asyncio.ensure_future(blackhole_consume(track))

    async def stop(self) -> None:
        """
        Stop discarding media.
        """
        for task in self.__tracks.values():
            if task is not None:
                task.cancel()
        self.__tracks = {}



# Implements the websocket camera for any jmpeg URL.
class QuickCam_WebRTC:

    def __init__(self, logger:logging.Logger):
        self.Logger = logger



    # ~~ Interface Function ~~
    # Connects to the server.
    # This will throw an exception if it fails.
    def Connect(self, url:str) -> None:

        # Configure the peer connection with a STUN server.
        config = RTCConfiguration(iceServers=[RTCIceServer("stun:stun.l.google.com:19302")])
        #configuration = {"iceServers": [{"urls": "stun:stun.l.google.com:19302"}]}
        pc = RTCPeerConnection(configuration=config)

        # run event loop
        #loop = asyncio.get_event_loop()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                self._runLoop(
                    pc=pc,
                )
            )
        except KeyboardInterrupt:
            pass
        finally:
            # cleanup
            #loop.run_until_complete(recorder.stop())
           # loop.run_until_complete(signaling.close())
            loop.run_until_complete(pc.close())


    async def _runLoop(self, pc:RTCPeerConnection):

        # Add a transceiver for video in "sendrecv" mode.
        pc.addTransceiver("video", direction="sendrecv")

        # Create the offer and set it as the local description.
        offer = await pc.createOffer()
        offer.sdp = offer.sdp.replace("packetization-mode=0", "packetization-mode=1")

        await pc.setLocalDescription(offer)
        print("Local offer created. Waiting for ICE gathering to complete...")

        # Wait until ICE gathering is complete.
        while pc.iceGatheringState != "complete":
            await asyncio.sleep(0.1)

        # Prepare the offer message as a base64 encoded JSON string.
        sdpOffer = pc.localDescription.sdp
        offer_dict = {
            "type": pc.localDescription.type,
            "sdp": sdpOffer
        }
        offer_json = json.dumps(offer_dict)
        offer_b64 = base64.b64encode(offer_json.encode()).decode()
        print("ICE gathering complete. Sending offer to the signaling server.")

        # Use the requests library for the HTTP POST call.
        response = requests.post(
            "http://10.0.0.26:8000/call/webrtc_local",
            data=offer_b64,
            headers={"Content-Type": "plain/text"},
            timeout=10
        )
        if response.status_code != 200:
            print("Error sending offer: HTTP", response.status_code)
            return

        # Decode the response (base64 -> JSON).
        response_text = response.text
        answer_json_str = base64.b64decode(response_text).decode()
        answer_dict = json.loads(answer_json_str)
        print("Received answer from signaling server:", answer_dict)

        # Set the remote description if the answer is valid.
        if answer_dict.get("type") == "answer":
            sdp = answer_dict["sdp"]
            answer = RTCSessionDescription(sdp=answer_dict["sdp"], type=answer_dict["type"])
            await pc.setRemoteDescription(answer)
            print("Remote description set successfully.")
        else:
            print("Unexpected answer type:", answer_dict.get("type"))
            return

        # Handler for incoming tracks.
        @pc.on("track")
        def on_track(track):
            print(f"Received track: {track.kind}")
            if track.kind == "video":
                # In a real application, you might process or display video frames.
                async def receive_video():
                    while True:
                        frame = await track.recv()
                        print("Received a video frame:", frame)
                asyncio.ensure_future(receive_video())

        # Keep the connection alive (adjust sleep duration as needed).
        await asyncio.sleep(60)

        # recorder = MediaBlackhole()

        # @pc.on("track")
        # def on_track(track):
        #     print("Receiving %s" % track.kind)
        #     recorder.addTrack(track)

        # # connect signaling
        # #await signaling.connect()

        # # if role == "offer":
        # #     # send offer
        # #     add_tracks()
        # #     await pc.setLocalDescription(await pc.createOffer())
        # #     await signaling.send(pc.localDescription)

        # # consume signaling
        # while True:
        #     # obj = await signaling.receive()
        #     await pc.setLocalDescription(await pc.createOffer())
        #     test = pc.localDescription()
        #     response = requests.post("http://localhost:8080", json=test)
        #     print(response)

        #     # if isinstance(obj, RTCSessionDescription):
        #     offer = RTCSessionDescription("", "offer")
        #     await pc.setRemoteDescription(offer)
        #     await recorder.start()

        #     # if obj.type == "offer":
        #     #     # send answer
        #     #     add_tracks()
        #     #     await pc.setLocalDescription(await pc.createAnswer())
        #     #         await signaling.send(pc.localDescription)
        #     # elif isinstance(obj, RTCIceCandidate):
        #     #     await pc.addIceCandidate(obj)
        #     # elif obj is BYE:
        #     #     print("Exiting")
        #     #     break


    # ~~ Interface Function ~~
    # Gets an image from the server. This should block until an image is ready.
    # This can return None to indicate there's no image but the connection is still good, this allows the host to check if we should still be running.
    # To indicate connection is closed or needs to be closed, this should throw.
    def GetImage(self) -> bytearray:

        # Ensure we have a valid result
        if self.OctoResult is None:
            raise Exception("QuickCam_Jmpeg failed to make the http request.")
        if self.OctoResult.StatusCode != 200:
            raise Exception(f"QuickCam_Jmpeg failed to get a valid OctoHttpRequest result. Status code: {self.OctoResult.StatusCode}")

        # Try to get an image from the stream using the common logic.
        result = WebcamUtil.GetSnapshotFromStream(self.Logger, self.OctoResult, validateMultiStreamHeader=self.IsFirstImagePull)
        self.IsFirstImagePull = False
        if result is None:
            raise Exception("QuickCam_Jmpeg failed to get an image from the stream.")

        # We must use the ensure jpeg header info function to ensure the image is a valid jpeg.
        # We know, for example, the Elegoo OS webcam server doesn't send the jpeg header info properly.
        return WebcamUtil.EnsureJpegHeaderInfo(self.Logger, result.ImageBuffer)


    # Allows us to using the with: scope.
    def __enter__(self):
        return self


    # Allows us to using the with: scope.
    # Must not throw!
    def __exit__(self, t, v, tb):
        try:
            if self.OctoResult is not None:
                self.OctoResult.__exit__(t, v, tb)
        except Exception:
            pass
