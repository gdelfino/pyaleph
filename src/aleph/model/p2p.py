"""
"""

from aleph.model.base import BaseClass
from pymongo import ASCENDING, DESCENDING, IndexModel


class Chain(BaseClass):
    """Holds information about the chains state."""
    COLLECTION = "peers"

    INDEXES = [IndexModel([("type", ASCENDING)]),
               IndexModel([("address", ASCENDING)], unique=True),
               IndexModel([("last_seen", DESCENDING)])]
    
async def get_peers(peer_type=None):
    """ Returns current peers.
    TODO: handle the last seen, channel preferences, and better way of avoiding "bad contacts".
    NOTE: Currently used in jobs.
    """
    async for peer in Chain.collection.find({'type': peer_type}):
        yield peer['address']