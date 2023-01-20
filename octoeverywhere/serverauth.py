import random
import string
import rsa

from .sentry import Sentry

# A helper class to handle server validation.
#
# The printer connection to OctoEverywhere is established over a secure websocket using the lastest TLS protocls and policies.
# However, since OctoEverywhere handles very senstive access to phyical 3D printers, we want to make sure the connection is incredibly secure.
# No bad actor should ever be able to generate a valid SSL cert for OctoEverywhere. But it would be possible to add a bad root cert to the
# device and then generate certs based on it.
#
# Thus to add another layer of security, we will validate the secure websocket connection is connected to a valid OctoEverywhere server by also
# doing an RSA challenge. We encrypt a random string the client generates with a public key and send it to the server. The server will use it's private key
# to decrypt it and send the plan text challnege back (over the secure websocket). If the server can successfully decrypt our message, it knows the correct private
# key and thus can be trusted.
class ServerAuthHelper:

    # Defines what key we expect to be using
    c_ServerAuthKeyVersion = 1

    # Version 1 of the RSA public key.
    c_ServerPublicKey = "-----BEGIN RSA PUBLIC KEY-----\nMIICCgKCAgEAwOjuEvc4bnY+MNkzG8ztlUhjPcRVSKGX53fuzmjshuwrhNu9KdNO\nlvEH4ORZI6S3xnXRhzupWYD8M2CVzsNSKJulNPe5hgoxct2bynoEwzzEKXkuypuw\nVtr+/nETdD+quWdS4oEMvmLFI1+7+Qlq4lqddPgIjC5xAvwN3d1NYJMFY3M7jHaq\n2JNK3g6YsEyUYlBFkvrgB8SXjQCrevriANP2UPzZl2uEJh/ibH85CAnfoPPCdGpp\nkfY2KG/fzDVv7nE/7SYW/44RUv4BC6wyJY7PB+ZhTXAcVs67hq6l2/dHOUEek455\n4vJf08sp85JhmeZgEg9COF5j7rAHnnOjENYVVW9FCQam6vscXETrVYX++6QMD/1G\nPdFnZs4KoG2i0LqqC3RoS/Nt3d2CeIl6U+BCueY5icxy5EgsAF4H48yIN7jx1oUd\nJk2TJQsvTnMt7sdIL96v1U/fl7U7kcHxHKXn79Mhtf4yUKnApwEL8JRVmRSL8y8x\nMEqQzTZsBYradQXjPL5QSNwgAGhVEYWgmUGmY8esUVF35/HuzgkJmZjgldU5WJGr\n6pvONbuDIoAwz2EnyVS7r+IL6Eqy2xbA8h5YllJ/qcau5V4YGt2C4JDK4PuX4gTM\n71iVsKozshWsXK8ctySQ0Jbc0O0zVlRTzCw0xH78lWaSHU7H2GitYF0CAwEAAQ==\n-----END RSA PUBLIC KEY-----\n"

    # Defines the length of the challenge we will encrypt.
    c_ServerAuthChallengeLength = 64

    def __init__(self, logger):
        self.Logger = logger

        # Generate our random challenge string.
        self.Challenge =  ''.join(random.SystemRandom().choice(string.ascii_uppercase + string.ascii_lowercase + string.digits) for _ in range(ServerAuthHelper.c_ServerAuthChallengeLength))

    # Returns a string that is our challenge encrypted with the public RSA key.
    def GetEncryptedChallenge(self):
        try:
            publicKey = rsa.PublicKey.load_pkcs1(ServerAuthHelper.c_ServerPublicKey)
            return rsa.encrypt(self.Challenge.encode('utf8'), publicKey)
        except Exception as e:
            Sentry.Exception("GetEncryptedChallenge failed.", e)
        return None

    # Validates the decrypted challenge the server returned is correct.
    def ValidateChallengeResponse(self, response):
        if response is None:
            return False
        if response != self.Challenge:
            return False
        return True
