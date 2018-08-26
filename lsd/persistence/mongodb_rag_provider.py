from __future__ import absolute_import
from ..shared_rag_provider import SharedRagProvider, SubRag
from networkx.convert import to_dict_of_dicts
from daisy import Coordinate
from pymongo import MongoClient, IndexModel, ASCENDING
from pymongo.errors import BulkWriteError
import logging

logger = logging.getLogger(__name__)

class MongoDbSubRag(SubRag):

    def __init__(self, db_name, host=None, mode='r+'):

        super(SubRag, self).__init__()

        self.db_name = db_name
        self.host = host
        self.mode = mode

        self.client = MongoClient(self.host)
        self.database = self.client[db_name]
        self.nodes_collection = self.database['nodes']
        self.edges_collection = self.database['edges']

    def _contains(self, roi, edge):

        u, v = edge
        min_node = self.node[u]

        # Some nodes are outside of the originally requested ROI (they have
        # been pulled in by edges leaving the ROI). These nodes have no
        # attributes, so we can't perform an inclusion test. However, we
        # know they are outside of the sub-RAG ROI, and therefore also
        # outside of 'roi', whatever it is.
        if 'center_z' not in min_node:
            return False

        min_node_center = Coordinate((
            min_node['center_z'],
            min_node['center_y'],
            min_node['center_x']))

        return roi.contains(min_node_center)

    def sync_edges(self, roi):

        if self.mode == 'r':
            raise RuntimeError("Trying to write to read-only DB")

        logger.debug("Writing edges in %s", roi)

        edges = []
        for u, v, data in self.edges(data=True):

            u, v = min(u, v), max(u, v)
            if not self._contains(roi, (u, v)):
                continue

            edge = {
                'u': int(u),
                'v': int(v),
            }
            edge.update(data)
            edges.append(edge)

        if len(edges) == 0:
            return

        try:

            self.edges_collection.insert_many(edges)

        except BulkWriteError as e:

            logger.error(e.details)
            raise

    def sync_nodes(self):

        if self.mode == 'r':
            raise RuntimeError("Trying to write to read-only DB")

        logger.debug("Writing all nodes")

        nodes = []
        for node_id, data in self.nodes(data=True):

            node = {
                'id': int(node_id)
            }
            node.update(data)
            nodes.append(node)

        if len(nodes) == 0:
            return

        try:

            self.nodes_collection.insert_many(nodes)

        except BulkWriteError as e:

            logger.error(e.details)
            raise

class MongoDbRagProvider(SharedRagProvider):
    '''A shared region adjacency graph stored in an SQLite file.
    '''

    def __init__(self, db_name, host=None, mode='r+'):

        self.db_name = db_name
        self.host = host
        self.mode = mode
        self.client = None
        self.database = None
        self.nodes = None
        self.edges = None

        try:

            self.__connect()

            if mode == 'w':
                logger.info("dropping database %s", db_name)
                self.client.drop_database(db_name)

            if self.db_name not in self.client.list_database_names():
                self.__setup_db()

        finally:

            self.__disconnect()

    def __connect(self):

        self.client = MongoClient(self.host)

    def __open_db(self):

        self.database = self.client[self.db_name]
        self.nodes = self.database['nodes']
        self.edges = self.database['edges']

    def __disconnect(self):

        self.nodes = None
        self.edges = None
        self.database = None
        self.client.close()
        self.client = None

    def __setup_db(self):

        self.__open_db()

        self.nodes.create_index(
            [
                ('center_z', ASCENDING),
                ('center_y', ASCENDING),
                ('center_x', ASCENDING)
            ],
            name='position')

        self.nodes.create_index(
            [
                ('id', ASCENDING)
            ],
            name='id',
            unique=True)

        self.edges.create_index(
            [
                ('u', ASCENDING),
                ('v', ASCENDING)
            ],
            name='incident',
            unique=True)

    def __read_nodes(self, roi):
        '''Return a list of nodes within roi.
        '''

        logger.debug("Querying nodes in %s", roi)

        bz, by, bx = roi.get_begin()
        ez, ey, ex = roi.get_end()

        nodes = self.nodes.find(
            {
                'center_z': { '$gte': bz, '$lt': ez },
                'center_y': { '$gte': by, '$lt': ey },
                'center_x': { '$gte': bx, '$lt': ex }
            })

        return nodes

    def __getitem__(self, roi):

        assert roi.dims() == 3, "Sorry, MongoDbRagProvider backend does only 3D"

        try:

            self.__connect()
            self.__open_db()

            # get all nodes within roi
            nodes = self.__read_nodes(roi)

            # create a list of nodes and their attributes
            node_list = [
                (n['id'], self.__remove_keys(n, ['id']))
                for n in nodes
            ]
            logger.debug("found %d nodes", len(node_list))
            logger.debug("read nodes: %s", node_list)

            # get all edges that have their u in the selected nodes
            node_ids = list([ node[0] for node in node_list])
            logger.debug("looking for edges with u in %s", node_ids)
            edges = self.edges.find(
                {
                    'u': { '$in': node_ids }
                })

            # create a list of edges and their attributes
            edge_list = [
                (e['u'], e['v'], self.__remove_keys(e, ['u', 'v']))
                for e in edges
            ]
            logger.debug("found %d edges", len(edge_list))
            logger.debug("read edges: %s", edge_list)

        finally:

            self.__disconnect()

        # create the sub-RAG
        graph = MongoDbSubRag(self.db_name, self.host, self.mode)
        graph.add_nodes_from(node_list)
        graph.add_edges_from(edge_list)

        return graph

    def __remove_keys(self, dictionary, keys):

        for key in keys:
            del dictionary[key]
        return dictionary