import logging
import asyncio
from libp2p.network.exceptions import SwarmException
from libp2p.network.stream.exceptions import StreamEOF, StreamReset
from libp2p.network.stream.net_stream_interface import INetStream
from libp2p.typing import TProtocol
from libp2p.network.notifee_interface import INotifee
from libp2p.network.stream.exceptions import StreamError
from .pubsub import sub
from aleph.network import incoming_check
# from aleph.services.filestore import get_value
from concurrent import futures
from . import singleton
from . import peers
import orjson as json
import base64
import random

PROTOCOL_ID = TProtocol("/aleph/p2p/0.1.0")
MAX_READ_LEN = 2 ** 32 - 1

LOGGER = logging.getLogger('P2P.protocol')

STREAM_COUNT = 5

HELLO_PACKET = {
    'command': 'hello'
}

CONNECT_LOCK = asyncio.Lock()

class AlephProtocol(INotifee):
    def __init__(self, host, streams_per_host=5):
        self.host = host
        self.streams_per_host = streams_per_host
        print(self.host.get_network())
        self.host.get_network().register_notifee(self)
        self.host.set_stream_handler(PROTOCOL_ID, self.stream_handler)
        self.peers = dict()
        
    async def stream_handler(self, stream: INetStream) -> None:
        asyncio.ensure_future(self.read_data(stream))
    
    async def read_data(self, stream: INetStream) -> None:
        from aleph.storage import get_hash_content
        while True:
            read_bytes = await stream.read(MAX_READ_LEN)
            if read_bytes is not None:
                result = {'status': 'error',
                        'reason': 'unknown'}
                try:
                    read_string = read_bytes.decode('utf-8')
                    message_json = json.loads(read_string)
                    if message_json['command'] == 'hash_content':
                        value = await get_hash_content(message_json['hash'], use_network=False, timeout=.2)
                        if value is not None and value != -1:
                            result = {'status': 'success',
                                    'hash': message_json['hash'],
                                    'content': base64.encodebytes(value).decode('utf-8')}
                        else:
                            result = {'status': 'success',
                                    'content': None}
                    else:
                        result = {'status': 'error',
                                'reason': 'unknown command'}
                    LOGGER.debug(f"received {read_string}")
                except Exception as e:
                    result = {'status': 'error',
                            'reason': repr(e)}
                await stream.write(json.dumps(result))
                
    async def make_request(self, request_structure):
        streams = [(peer, item) for peer, sublist in self.peers.items() for item in sublist]
        random.shuffle(streams)
        while True:
            for i, (peer, (stream, semaphore)) in enumerate(streams):
                if not semaphore.locked():
                    async with semaphore:
                        try:
                            # stream = await asyncio.wait_for(singleton.host.new_stream(peer_id, [PROTOCOL_ID]), connect_timeout)
                            await stream.write(json.dumps(request_structure))
                            value = await stream.read(MAX_READ_LEN)
                            # # await stream.close()
                            return json.loads(value)
                        except (StreamError):
                            # let's delete this stream so it gets recreated next time
                            # await stream.close()
                            await stream.reset()
                            streams.remove((peer, (stream, semaphore)))
                            try:
                                self.peers[peer].remove((stream, semaphore))
                            except ValueError:
                                pass
                            LOGGER.debug("Can't request hash...")
                await asyncio.sleep(0)
                
            if not len(streams):
                return
    
    async def request_hash(self, item_hash):
        # this should be done better, finding best peers to query from.
        query = {
            'command': 'hash_content',
            'hash': item_hash
        }
        item = await self.make_request(query)
        if item is not None and item['status'] == 'success' and item['content'] is not None:
            # TODO: IMPORTANT /!\ verify the hash of received data!
            return base64.decodebytes(item['content'].encode('utf-8'))
        else:
            LOGGER.debug(f"can't get hash {item_hash}")
                
    async def _handle_new_peer(self, peer_id) -> None:
        await self.create_connections(peer_id)
        LOGGER.debug("added new peer %s", peer_id)
        
    async def create_connections(self, peer_id):
        peer_streams = self.peers.get(peer_id, list())
        for i in range(self.streams_per_host - len(peer_streams)):
            try:
                stream: INetStream = await self.host.new_stream(peer_id, [PROTOCOL_ID])
            except SwarmException as error:
                LOGGER.debug("fail to add new peer %s, error %s", peer_id, error)
                return
            
            try:
                await stream.write(json.dumps(HELLO_PACKET))
                await stream.read(MAX_READ_LEN)
            except Exception as error:
                LOGGER.debug("fail to add new peer %s, error %s", peer_id, error)
                return
            
            peer_streams.append((stream, asyncio.Semaphore(1)))
            # await asyncio.sleep(.1)
        
        self.peers[peer_id] = peer_streams
        
        
    async def opened_stream(self, network, stream) -> None:
        pass

    async def closed_stream(self, network, stream) -> None:
        pass

    async def connected(self, network, conn) -> None:
        """
        Add peer_id to initiator_peers_queue, so that this peer_id can be used to
        create a stream and we only want to have one pubsub stream with each peer.
        :param network: network the connection was opened on
        :param conn: connection that was opened
        """
        #await self.initiator_peers_queue.put(conn.muxed_conn.peer_id)
        peer_id = conn.muxed_conn.peer_id
        asyncio.ensure_future(self._handle_new_peer(peer_id))
        

    async def disconnected(self, network, conn) -> None:
        pass

    async def listen(self, network, multiaddr) -> None:
        pass

    async def listen_close(self, network, multiaddr) -> None:
        pass
    
    async def has_active_streams(self, peer_id):
        if peer_id not in self.peers:
            return False
        return bool(len(self.peers[peer_id]))

async def incoming_channel(config, topic):
    from aleph.chains.common import incoming
    loop = asyncio.get_event_loop()
    while True:
        try:
            i = 0
            tasks = []
            async for mvalue in sub(topic):
                try:
                    message = json.loads(mvalue['data'])

                    # we should check the sender here to avoid spam
                    # and such things...
                    message = await incoming_check(mvalue)
                    if message is None:
                        continue
                    
                    LOGGER.debug("New message %r" % message)
                    i += 1
                    tasks.append(
                        loop.create_task(incoming(message)))

                    # await incoming(message, seen_ids=seen_ids)
                    if (i > 1000):
                        # every 1000 message we check that all tasks finished
                        # and we reset the seen_ids list.
                        for task in tasks:
                            await task
                        tasks = []
                        i = 0
                except:
                    LOGGER.exception("Can't handle message")

        except Exception:
            LOGGER.exception("Exception in pubsub, reconnecting.")


async def request_hash(item_hash):
    return await singleton.streamer.request_hash(item_hash)
    

        

